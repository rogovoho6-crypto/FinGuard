"""FastAPI transport, request validation, access control, and static frontend."""
from __future__ import annotations

import base64
from datetime import date
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core.store import demo_csv
from .core.selfcheck import check_all
from .service import Conflict, FinGuardService, VERSION


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Mutation(Input):
    revision: StrictStr


class Balance(Mutation):
    amount_kopecks: StrictInt
    as_of: date


class Review(Mutation):
    payment_id: StrictInt
    verdict: StrictStr


class PaymentEdit(Mutation):
    payment_id: StrictInt
    status: StrictStr
    payment_date: date


class Demo(Mutation):
    case: StrictStr


class Calculation(Input):
    parameters: dict


class ScenarioSave(Mutation):
    name: StrictStr
    parameters: dict


class ScenarioDelete(Mutation):
    scenario_id: StrictInt


class Preview(Input):
    content: StrictStr


class ImportApply(Mutation):
    token: StrictStr


class Restore(Mutation):
    name: StrictStr | None = None
    content: StrictStr | None = None


def default_data_dir():
    root = Path(os.environ.get('APPDATA', Path.home() / '.local/share'))
    return Path(os.environ.get('FINGUARD_DATA_DIR', root / 'FinGuard' / 'web'))


def create_app(data_dir=None, password=None, allowed_hosts=None, frontend_dir=None):
    service = FinGuardService(Path(data_dir) if data_dir else default_data_dir())
    password = password if password is not None else os.environ.get('FINGUARD_PASSWORD', '')
    csrf = secrets.token_urlsafe(32)
    app = FastAPI(title='FinGuard Web API', version=VERSION, docs_url=None, openapi_url='/api/openapi.json', redoc_url=None)
    app.state.service = service
    hosts = allowed_hosts or os.environ.get('FINGUARD_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1]').split(',')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    @app.middleware('http')
    async def access(request: Request, call_next):
        if password:
            header = request.headers.get('authorization', '')
            try:
                credentials = base64.b64decode(header.split(' ', 1)[1], validate=True).decode('utf-8')
                valid = header.startswith('Basic ') and hmac.compare_digest(credentials.encode(), ('finguard:' + password).encode())
            except (ValueError, IndexError, UnicodeDecodeError):
                valid = False
            if not valid:
                return Response(status_code=401, headers={'WWW-Authenticate': 'Basic realm="FinGuard", charset="UTF-8"', 'Cache-Control': 'no-store'})
        if request.method not in ('GET', 'HEAD'):
            if not hmac.compare_digest(request.headers.get('x-finguard-token', ''), csrf):
                return JSONResponse({'detail': 'Обновите страницу перед выполнением действия.'}, 403)
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Запрос с другого сайта запрещён.'}, 403)
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 12 * 1024 * 1024:
                    return JSONResponse({'detail': 'Файл слишком большой. CSV: до 2 МБ; копия: до 8 МБ.'}, 413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409 if isinstance(exc, Conflict) else 400)

    @app.get('/api/health')
    def health():
        return dict(status='ok', version=VERSION)

    @app.get('/api/docs', include_in_schema=False, response_class=HTMLResponse)
    def docs():
        schema = app.openapi()
        rows = []
        for path, methods in schema['paths'].items():
            for method, operation in methods.items():
                rows.append('<article><h2>' + html.escape(method.upper()+' '+path) + '</h2><p>' +
                    html.escape(operation.get('summary','')) + '</p><details><summary>Формат запроса и ответа</summary><pre>' +
                    html.escape(json.dumps(operation,ensure_ascii=False,indent=2)) + '</pre></details></article>')
        models = html.escape(json.dumps(schema.get('components',{}),ensure_ascii=False,indent=2))
        return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FinGuard API</title><style>body{font:14px/1.7 system-ui;margin:32px auto;max-width:980px;padding:0 22px;color:#243c5d;background:#f5f7fb}article{background:white;border:1px solid #e0e7f2;padding:18px;margin:15px 0;border-radius:10px}h2{font-size:16px}pre{overflow:auto;font-size:12px}a{color:#2463e9}</style><h1>FinGuard Web API</h1><p>Справочник работает без интернета. <a href="/api/openapi.json">OpenAPI JSON</a> · <a href="/">Вернуться в приложение</a></p><p>Для POST-запросов передавайте X-FinGuard-Token из /api/state. При изменении данных также передавайте revision из того же состояния. Конфликт версий возвращает HTTP 409.</p>' + ''.join(rows) + '<article><h2>Модели данных</h2><pre>'+models+'</pre></article></html>'

    @app.get('/api/state')
    def state():
        return dict(**service.state(), csrf_token=csrf)

    @app.post('/api/balance')
    def balance(body: Balance):
        if body.as_of > date.today():
            raise ValueError('Дата фактического остатка не может быть в будущем.')
        with service.store(body.revision) as store:
            store.set_balance(body.amount_kopecks, body.as_of)
        return dict(ok=True)

    @app.post('/api/review')
    def review(body: Review):
        with service.store(body.revision) as store:
            store.set_duplicate_review(body.payment_id, body.verdict)
        return dict(ok=True)

    @app.post('/api/payment')
    def payment(body: PaymentEdit):
        with service.store(body.revision) as store:
            store.set_payment_status(body.payment_id, body.status, body.payment_date)
        return dict(ok=True)

    @app.post('/api/demo')
    def demo(body: Demo):
        with service.store(body.revision) as store:
            before = store.create_backup()
            store.load_case(body.case)
        return dict(backup_name=before.name)

    @app.post('/api/scenario/calculate')
    def calculate(body: Calculation):
        return service.calculate(body.parameters)

    @app.post('/api/scenario/save')
    def save(body: ScenarioSave):
        return dict(id=service.save_scenario(body.name, body.parameters, body.revision))

    @app.get('/api/scenario/compare')
    def compare():
        return service.compare()

    @app.get('/api/scenario/{scenario_id}')
    def load(scenario_id: int):
        return service.load_scenario(scenario_id)

    @app.post('/api/scenario/delete')
    def delete(body: ScenarioDelete):
        with service.store(body.revision) as store:
            store.delete_scenario(body.scenario_id)
        return dict(ok=True)

    @app.post('/api/import/preview')
    def preview(body: Preview):
        return service.preview_csv(body.content)

    @app.post('/api/import/apply')
    def apply(body: ImportApply):
        return service.apply_import(body.token, body.revision)

    @app.get('/api/sample')
    def sample(case: str = 'duplicates'):
        return Response(demo_csv(case), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="FinGuard_sample.csv"'})

    @app.get('/api/export')
    def export():
        content = json.dumps(service.state(), ensure_ascii=False, indent=2).encode('utf-8')
        return Response(content, media_type='application/json', headers={'Content-Disposition': 'attachment; filename="FinGuard_report.json"'})

    @app.post('/api/backup/create')
    def backup(body: Mutation):
        with service.store(body.revision) as store:
            return dict(name=store.create_backup().name)

    @app.get('/api/backup/download')
    def download(name: str):
        return FileResponse(service.backup_file(name), media_type='application/vnd.sqlite3', filename=name)

    @app.post('/api/backup/restore')
    def restore(body: Restore):
        return service.restore(body.revision, body.name, body.content)

    @app.post('/api/selfcheck')
    def selfcheck():
        checks = check_all()
        return dict(passed=all(ok for _, ok, _ in checks), checks=[dict(name=name, passed=ok, detail=detail) for name, ok, detail in checks])

    static = Path(frontend_dir) if frontend_dir else Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'frontend' / 'dist'
    if static.is_dir():
        app.mount('/', StaticFiles(directory=static, html=True), name='frontend')
    return app
