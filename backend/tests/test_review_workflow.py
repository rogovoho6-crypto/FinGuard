"""Full non-GUI release tests: database migration, manual review, risk filtering."""
import sys
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.core.finguard_engine import check_payments, run_forecast, make_dashboard
from backend.core.payment_workflow import match_payment, visible_issues
from backend.core.store import Store, sample_case, parse_csv_bytes, ImportErrorCSV


class ReviewWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.db = Store(self.path)
        self.db.load_case('duplicates')
        self.duplicates = [x for x in check_payments(self.db.payments()) if x['code'] == 'duplicate']
        self.assertEqual(len(self.duplicates), 1)
        self.duplicate_id = self.duplicates[0]['payment_id']

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_manual_review_migrates_existing_database(self):
        self.db.close()
        self.path.unlink()
        conn = sqlite3.connect(self.path)
        conn.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY, payment_date TEXT NOT NULL, counterparty TEXT NOT NULL,
                inn TEXT NOT NULL, account TEXT NOT NULL, bik TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL, reference TEXT NOT NULL, status TEXT NOT NULL);
            INSERT INTO settings(key,value) VALUES ('balance','30000000');
            INSERT INTO payments VALUES (1, '2026-11-03', 'Test', '', '', '', -900, 'A', 'planned');
        ''')
        conn.commit()
        conn.close()
        db = Store(self.path)
        self.assertEqual(db.opening_balance(), 30000000)
        self.assertEqual(len(db.payments()), 1)
        self.assertEqual(db.review_statuses(), {})
        db.close()
        self.db = Store(self.path)

    def test_mark_dismiss_and_confirm_persist_without_changing_money(self):
        before = run_forecast(self.db.payments(), self.db.opening_balance(), date.today())
        self.db.set_duplicate_review(self.duplicate_id, 'dismissed')
        self.assertEqual(self.db.review_statuses()[self.duplicate_id], 'dismissed')
        active = visible_issues(check_payments(self.db.payments()), self.db.review_statuses())
        self.assertFalse(any(x['code'] == 'duplicate' for x in active))
        self.assertEqual(before, run_forecast(self.db.payments(), self.db.opening_balance(), date.today()))
        self.db.close()
        self.db = Store(self.path)
        self.assertEqual(self.db.review_statuses()[self.duplicate_id], 'dismissed')
        self.db.set_duplicate_review(self.duplicate_id, 'confirmed')
        self.assertEqual(self.db.review_statuses()[self.duplicate_id], 'confirmed')
        self.db.set_duplicate_review(self.duplicate_id, 'pending')
        self.assertEqual(self.db.review_statuses(), {})

    def test_invalid_reviews_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_duplicate_review(self.duplicate_id, 'other')
        with self.assertRaises(ValueError):
            self.db.set_duplicate_review(999, 'dismissed')
        self.assertEqual(self.db.review_statuses(), {})

    def test_review_lifecycle_removed_on_new_case_or_import(self):
        self.db.set_duplicate_review(self.duplicate_id, 'confirmed')
        self.db.load_case('clean')
        self.assertEqual(self.db.review_statuses(), {})
        self.db.load_case('duplicates')
        self.db.set_duplicate_review(self.duplicate_id, 'dismissed')
        good = 'date;counterparty;amount\n25.10.2026;ООО Новый;-2000\n'.encode('utf-8')
        p = parse_csv_bytes(good)
        self.db.replace(p)
        self.assertEqual(self.db.review_statuses(), {})

    def test_csv_invalid_never_affects_reviews_or_payments(self):
        self.db.set_duplicate_review(self.duplicate_id, 'confirmed')
        before = self.db.payments()
        with self.assertRaises(ImportErrorCSV):
            parse_csv_bytes(b'date;counterparty;amount\n2026-99-99;Company;-20\n')
        self.assertEqual(self.db.payments(), before)
        self.assertEqual(self.db.review_statuses()[self.duplicate_id], 'confirmed')

    def test_filter_modes_and_combined_search(self):
        rows = self.db.payments()
        issues = check_payments(rows)
        issues_by_id = {}
        for i in issues: issues_by_id.setdefault(i['payment_id'], []).append(i)
        rev = {}
        result = lambda mode, text='': [p for p in rows if match_payment(p, issues_by_id, rev, mode, text)]
        self.assertEqual(len(result('all')), 9)
        self.assertEqual(len(result('duplicate')), 1)
        self.assertEqual(len(result('flagged')), 3)
        self.assertEqual(len(result('clean')), 6)
        self.assertEqual(len(result('reviewed')), 0)
        self.assertEqual(len(result('duplicate', 'альфа')), 1)
        self.db.set_duplicate_review(self.duplicate_id, 'dismissed')
        rev = self.db.review_statuses()
        active = visible_issues(issues, rev)
        issues_by_id = {}
        for i in active: issues_by_id.setdefault(i['payment_id'], []).append(i)
        self.assertEqual(len(result('duplicate')), 0)
        self.assertEqual(len(result('reviewed')), 1)
        self.assertEqual(len(result('flagged')), 2)

    def test_simulation_duplicate_impact_is_150k(self):
        rows = self.db.payments()
        base = run_forecast(rows, self.db.opening_balance(), date.today())
        new = run_forecast(rows, self.db.opening_balance(), date.today(),
                           excluded_ids={self.duplicate_id})
        self.assertEqual(new['final_rub'] - base['final_rub'], 150000)
        self.assertEqual(new['min_rub'] - base['min_rub'], 150000)


if __name__ == '__main__':
    unittest.main()
