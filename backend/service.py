"""Application use cases. No HTTP, HTML, or browser dependencies."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from pathlib import Path
import secrets
import tempfile
import threading
import time

from .core.finguard_engine import check_payments, make_dashboard, run_forecast
from .core.payment_workflow import visible_issues
from .core.scenario_actions import normalize_scenario
from .core.store import Store, parse_csv_bytes

VERSION = '6.2-web.1'


class Conflict(ValueError):
    pass


def serialize_payment(payment):
    result = asdict(payment)
    result['payment_date'] = payment.payment_date.isoformat()
    return result


class FinGuardService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'finguard-web.sqlite3'
        self.lock = threading.RLock()
        self.previews = {}
        with self.store():
            pass

    @contextmanager
    def store(self, revision=None):
        # One connection per use case; SQLite objects never cross worker threads.
        with self.lock:
            store = Store(self.db_path)
            try:
                if revision is not None and revision != store._db_signature():
                    raise Conflict('Данные изменились в другой вкладке. Обновите страницу и повторите действие.')
                yield store
            finally:
                store.close()

    @staticmethod
    def tag_forecast(store, forecast):
        when = store.opening_balance_date()
        forecast['balance_date'] = when.isoformat() if when else None
        forecast['balance_needs_reconciliation'] = store.balance_needs_reconciliation()
        forecast['balance_is_current'] = when == date.today() and not store.balance_needs_reconciliation()
        if when is None:
            forecast['warnings'].insert(0, 'Дата остатка неизвестна. Сверьте и сохраните фактический остаток.')
        if store.balance_needs_reconciliation():
            forecast['warnings'].insert(0, 'Изменились исполненные операции. Повторно сверьте и сохраните фактический остаток.')
        return forecast

    def state(self):
        with self.store() as store:
            rows = store.payments()
            reviews = store.review_statuses()
            dashboard = make_dashboard(rows, store.opening_balance(), date.today(), balance_date=store.opening_balance_date())
            raw_issues = dashboard['issues']
            dashboard['issues'] = visible_issues(raw_issues, reviews)
            dashboard['flagged_payment_count'] = len({i['payment_id'] for i in dashboard['issues']})
            dashboard['possible_duplicate_exposure_rub'] = sum(i['exposure_rub'] for i in dashboard['issues'] if i['code'] == 'duplicate')
            self.tag_forecast(store, dashboard['forecast'])
            return dict(version=VERSION, today=date.today().isoformat(), revision=store._db_signature(),
                        payments=[serialize_payment(p) for p in rows], reviews=reviews, raw_issues=raw_issues,
                        dashboard=dashboard, opening_kopecks=store.opening_balance(),
                        balance_date=str(store.opening_balance_date() or ''),
                        balance_needs_reconciliation=store.balance_needs_reconciliation(),
                        last_import_at=store.last_import_at(), scenarios=store.scenarios(),
                        backups=[dict(name=p.name, bytes=p.stat().st_size) for p in store.list_backups()])

    def calculate_with(self, store, parameters):
        rows = store.payments()
        params = normalize_scenario(parameters, rows)
        common = dict(horizon=params['horizon'], balance_date=store.opening_balance_date())
        base = run_forecast(rows, store.opening_balance(), date.today(), **common)
        actions = params['payment_actions']
        alternative = run_forecast(rows, store.opening_balance(), date.today(), **common,
            excluded_ids={x['payment_id'] for x in actions if x['action'] == 'exclude'},
            deferrals={x['payment_id']: x['defer_days'] for x in actions if x['action'] == 'defer'},
            revenue_drop_pct=params['revenue_drop_pct'], income_delay_days=params['income_delay_days'],
            extra_expense_rub=params['extra_expense_rub'])
        return dict(parameters=params, base=self.tag_forecast(store, base),
                    scenario=self.tag_forecast(store, alternative), revision=store._db_signature())

    def calculate(self, parameters):
        with self.store() as store:
            return self.calculate_with(store, parameters)

    def save_scenario(self, name, parameters, revision):
        with self.store(revision) as store:
            result = self.calculate_with(store, parameters)
            return store.save_scenario(name, result['parameters'])

    def load_scenario(self, scenario_id):
        with self.store() as store:
            saved = store.get_scenario(scenario_id)
            return dict(saved=saved, **self.calculate_with(store, saved['parameters']))

    def compare(self):
        with self.store() as store:
            output = []
            for saved in store.scenarios():
                try:
                    result = self.calculate_with(store, saved['parameters'])
                    output.append(dict(id=saved['id'], name=saved['name'], stale=saved['stale'], **result))
                except ValueError as exc:
                    output.append(dict(id=saved['id'], name=saved['name'], error=str(exc)))
            return output

    def preview_csv(self, content):
        try:
            raw = base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('Не удалось прочитать CSV-файл.') from exc
        rows = parse_csv_bytes(raw)
        with self.store() as store:
            preview = store._import_preview(rows)
            now = time.monotonic()
            self.previews = {k: v for k, v in self.previews.items() if now - v[0] < 900}
            if len(self.previews) >= 16:
                self.previews.pop(next(iter(self.previews)))
            token = secrets.token_urlsafe(24)
            self.previews[token] = (now, preview)
            public = {k: v for k, v in preview.items() if k not in ('rows', '_preview_signature', 'db_signature')}
            return dict(token=token, **public, rows=[serialize_payment(p) for p in preview['rows'][:100]])

    def apply_import(self, token, revision):
        with self.store(revision) as store:
            cached = self.previews.get(token)
            if cached is None or time.monotonic() - cached[0] >= 900:
                raise ValueError('Предпросмотр истёк. Выберите CSV-файл заново.')
            result = store.apply_import(cached[1])
            del self.previews[token]
            result['backup_name'] = Path(result.pop('backup_path')).name
            return result

    def backup_file(self, name):
        with self.store() as store:
            if name != Path(name).name:
                raise ValueError('Некорректное имя резервной копии.')
            found = next((p for p in store.list_backups() if p.name == name), None)
            if found is None:
                raise ValueError('Резервная копия не найдена.')
            return found

    def restore(self, revision, name=None, content=None):
        with self.store(revision) as store:
            if bool(name) == bool(content):
                raise ValueError('Выберите одну резервную копию.')
            if name:
                before = store.restore_backup(self.backup_file(name))
            else:
                try:
                    raw = base64.b64decode(content, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError('Не удалось прочитать резервную копию.') from exc
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('Резервная копия превышает 8 МБ.')
                with tempfile.TemporaryDirectory() as folder:
                    upload = Path(folder) / 'upload.sqlite3'
                    upload.write_bytes(raw)
                    before = store.restore_backup(upload)
            self.previews.clear()
            return dict(backup_name=before.name)
