import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from backend.core.finguard_engine import check_payments, make_dashboard, run_forecast
from backend.core.store import Store, ImportErrorCSV, demo_csv, parse_csv_bytes, sample_case
from backend.core.selfcheck import check_all


class CoreTests(unittest.TestCase):
    def test_base_suite(self):
        results = check_all()
        self.assertEqual(len(results), 9)
        self.assertTrue(all(passed for _, passed, _ in results), results)

    def test_russian_date(self):
        csv = 'date;counterparty;amount\n25.10.2026;ООО Альфа;-150000\n'
        rows = parse_csv_bytes(csv.encode('utf-8'))
        self.assertEqual(rows[0].payment_date, date(2026, 10, 25))

    def test_invalid_date_human_friendly(self):
        csv = 'date;counterparty;amount\n2026-99-99;ООО Альфа;-150000\n'
        with self.assertRaises(ImportErrorCSV) as caught:
            parse_csv_bytes(csv.encode('utf-8'))
        msg = str(caught.exception)
        self.assertIn('Строка 2', msg)
        self.assertIn('неверная дата', msg)
        self.assertIn('ГГГГ-ММ-ДД', msg)
        self.assertNotIn('Invalid isoformat', msg)

    def test_invalid_csv_preserves_previous_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'qa.db')
            before = store.payments()
            with self.assertRaises(ImportErrorCSV):
                parse_csv_bytes(b'date;counterparty;amount\n2026-99-99;X;20\n')
            self.assertEqual(before, store.payments())
            store.close()

    def test_forecast_exact_duplicate_delta(self):
        rows, balance = sample_case('duplicates')
        issue = next(i for i in check_payments(rows) if i['code'] == 'duplicate')
        base = run_forecast(rows, balance, date.today())
        future = run_forecast(rows, balance, date.today(), excluded_ids={issue['payment_id']})
        self.assertEqual(future['final_rub']-base['final_rub'], 150000)

    def test_gap_date_and_balance(self):
        rows, opening = sample_case('cash-gap')
        forecast = run_forecast(rows, opening, date.today())
        self.assertEqual(forecast['min_rub'], -80000)
        self.assertEqual(forecast['first_cash_gap_date'], (date.today()+timedelta(days=1)).isoformat())

    def test_demo_import_succeeds(self):
        for case in ('duplicates', 'clean', 'cash-gap', 'bad-details'):
            parsed = parse_csv_bytes(demo_csv(case))
            self.assertEqual(len(parsed), len(sample_case(case)[0]))

    def test_payment_error_and_clean(self):
        self.assertEqual(check_payments(sample_case('clean')[0]), [])
        self.assertTrue(check_payments(sample_case('bad-details')[0]))

    def test_balance_and_report_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'db.sqlite3'
            db = Store(path)
            db.set_balance(74321000)
            db.close()
            db = Store(path)
            self.assertEqual(db.opening_balance(), 74321000)
            dash = make_dashboard(db.payments(), db.opening_balance(), date.today())
            report = Path(tmp)/'report.json'
            db.export_json(report, {'forecast': dash['forecast']})
            self.assertTrue(report.exists())
            db.close()


if __name__ == '__main__':
    unittest.main()
