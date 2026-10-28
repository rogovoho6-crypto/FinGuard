"""In-app smoke checks; never modifies the user's database."""
from datetime import date, timedelta
from pathlib import Path
import json
import tempfile

from .finguard_engine import check_payments, make_dashboard, run_forecast
from .store import Store, demo_csv, parse_csv_bytes, sample_case, ImportErrorCSV
from .payment_workflow import visible_issues, match_payment


def check_all() -> list[tuple[str, bool, str]]:
    checks = []
    def test(name, action):
        try:
            action()
            checks.append((name, True, 'OK'))
        except Exception as exc:
            checks.append((name, False, str(exc)))

    def duplicate_check():
        rows, opening = sample_case('duplicates')
        dash = make_dashboard(rows, opening, date.today())
        assert dash['possible_duplicate_exposure_rub'] == 150000
        issue = next(i for i in dash['issues'] if i['code'] == 'duplicate')
        after = run_forecast(rows, opening, date.today(), excluded_ids={issue['payment_id']})
        assert after['final_rub'] - dash['forecast']['final_rub'] == 150000
    test('Дубль 150 000 ₽ и исключение', duplicate_check)

    def gap_check():
        rows, opening = sample_case('cash-gap')
        result = run_forecast(rows, opening, date.today())
        assert result['min_rub'] == -80_000
        assert result['first_cash_gap_date'] == (date.today()+timedelta(days=1)).isoformat()
    test('Кассовый разрыв −80 000 ₽ и дата', gap_check)

    def clean_check():
        rows, opening = sample_case('clean')
        assert not check_payments(rows)
    test('Чистый набор без замечаний', clean_check)

    def bad_details():
        rows, _ = sample_case('bad-details')
        assert {'inn', 'account_key', 'incomplete'} <= {x['code'] for x in check_payments(rows)}
    test('ИНН, счёт, неполные реквизиты', bad_details)

    def csv_roundtrip():
        for case in ['duplicates','cash-gap','clean','bad-details']:
            rows = parse_csv_bytes(demo_csv(case))
            assert len(rows) == len(sample_case(case)[0])
    test('CSV: четыре набора без потерь', csv_roundtrip)

    def invalid_file():
        try: parse_csv_bytes(b'date;counterparty;amount\n2026-99-99;X;100\n')
        except ImportErrorCSV: return
        assert False, 'Неверная дата прошла проверку'
    test('Некорректный CSV отклоняется', invalid_file)

    def persistence():
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'data.sqlite3'
            s = Store(path)
            s.load_case('cash-gap')
            s.close()
            s = Store(path)
            assert len(s.payments()) == 2
            s.close()
    test('SQLite: сохранение/повторное открытие', persistence)

    def review_flow():
        with tempfile.TemporaryDirectory() as d:
            db = Store(Path(d) / 'review.sqlite3')
            rows = db.payments()
            issue = next(x for x in check_payments(rows) if x['code'] == 'duplicate')
            baseline = run_forecast(rows, db.opening_balance(), date.today())
            db.set_duplicate_review(issue['payment_id'], 'dismissed')
            assert db.review_statuses()[issue['payment_id']] == 'dismissed'
            active = visible_issues(check_payments(db.payments()), db.review_statuses())
            assert not any(x['code'] == 'duplicate' for x in active)
            assert run_forecast(db.payments(), db.opening_balance(), date.today()) == baseline
            db.close()
            db = Store(Path(d) / 'review.sqlite3')
            assert db.review_statuses()[issue['payment_id']] == 'dismissed'
            db.close()
    test('Ручная проверка дубля и SQLite', review_flow)

    def filter_flow():
        rows, _ = sample_case('duplicates')
        issues = check_payments(rows)
        per_id = {}
        for flag in issues:
            per_id.setdefault(flag['payment_id'], []).append(flag)
        count = lambda mode: sum(match_payment(p, per_id, {}, mode, '') for p in rows)
        assert [count(x) for x in ('all', 'flagged', 'duplicate', 'clean', 'reviewed')] == [9, 3, 1, 6, 0]
    test('Фильтры платежей', filter_flow)
    return checks
