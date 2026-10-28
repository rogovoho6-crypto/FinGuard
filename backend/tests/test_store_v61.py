"""Temporary-database regressions for safe imports and recoverable v6.1 data."""
import csv
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.core.finguard_engine import check_payments
from backend.core.store import FIELDS, ImportErrorCSV, Store, sample_case


class StoreV61Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'test.sqlite3')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def csv_file(self, rows, name='payments.csv'):
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(FIELDS)
        for p in rows:
            writer.writerow([p.payment_date.isoformat(), p.counterparty,
                str(Decimal(p.amount_kopecks) / 100), p.inn, p.account, p.bik,
                p.reference, p.status])
        path = self.root / name
        path.write_text(output.getvalue(), encoding='utf-8-sig')
        return path

    def duplicate_id(self):
        return next(x['payment_id'] for x in check_payments(self.store.payments())
                    if x['code'] == 'duplicate')

    def test_snapshot_reorder_keeps_unique_ids_and_reviews(self):
        original = self.store.payments()
        pid = self.duplicate_id()
        self.store.set_duplicate_review(pid, 'dismissed')
        before = self.store._db_signature()
        preview = self.store.preview_import_file(self.csv_file(list(reversed(original))))
        self.assertEqual(before, self.store._db_signature(), 'Preview must be read-only')
        self.assertEqual((preview['added'], preview['removed'], preview['unchanged']), (0, 0, 9))
        self.assertEqual((preview['reviews_preserved'], preview['reviews_reset']), (1, 0))
        self.assertEqual(preview['ambiguous'], 0)
        applied = self.store.apply_import(preview)
        self.assertTrue(Path(applied['backup_path']).is_file())
        self.assertEqual(original, self.store.payments())
        self.assertEqual(self.store.review_statuses(), {pid: 'dismissed'})
        self.assertIsNotNone(self.store.last_import_at())
        self.assertEqual(self.store.import_file(self.csv_file(original)), 9)
        self.assertEqual(original, self.store.payments())

    def test_changed_source_resets_review_even_if_candidate_is_unchanged(self):
        old = self.store.payments()
        pid = self.duplicate_id()
        self.store.set_duplicate_review(pid, 'confirmed')
        incoming = [replace(p, reference='OTHER-INVOICE') if p.id == 1 else p for p in old]
        preview = self.store.preview_import_file(self.csv_file(incoming))
        self.assertEqual(preview['reviews_preserved'], 0)
        self.assertEqual(preview['reviews_reset'], 1)
        self.assertIn(pid, {p.id for p in preview['rows']})
        self.store.apply_import(preview)
        self.assertEqual(self.store.review_statuses(), {})

    def test_new_closer_related_payment_resets_old_pair_review(self):
        old = self.store.payments()
        pid = self.duplicate_id()
        self.store.set_duplicate_review(pid, 'dismissed')
        source = next(p for p in old if p.id == 1)
        # Same date as the older source; a larger ID wins the deterministic tie.
        nearer = replace(source, id=100, counterparty='Другая запись того же счёта')
        preview = self.store.preview_import_file(self.csv_file(old + [nearer]))
        self.assertEqual(preview['unchanged'], 9)
        self.assertEqual(preview['reviews_reset'], 1)
        new_pairs = self.store._candidate_pairs(preview['rows'])
        self.assertNotEqual(new_pairs[pid], 1)
        self.store.apply_import(preview)
        self.assertEqual(self.store.review_statuses(), {})

    def test_identical_fingerprints_are_ambiguous_and_never_accumulate(self):
        source = self.store.payments()[0]
        duplicate = replace(source, id=2)
        self.store.replace([source, duplicate])
        pid = self.duplicate_id()
        self.store.set_duplicate_review(pid, 'dismissed')
        path = self.csv_file([source, duplicate])
        preview = self.store.preview_import_file(path)
        self.assertEqual((preview['ambiguous'], preview['unchanged'], preview['reviews_reset']), (2, 0, 1))
        self.assertTrue(all(p.id > 2 for p in preview['rows']))
        self.store.apply_import(preview)
        self.assertEqual(self.store.review_statuses(), {})
        self.store.import_file(path)
        self.assertEqual(len(self.store.payments()), 2)

    def test_snapshot_adds_and_removes_exactly_the_csv_contents(self):
        old = self.store.payments()
        changed = replace(old[0], id=50, counterparty='Новый контрагент')
        preview = self.store.preview_import_file(self.csv_file([old[-1], changed]))
        self.assertEqual((preview['total'], preview['unchanged'], preview['added'], preview['removed']), (2, 1, 1, 8))
        self.assertEqual({c['kind'] for c in preview['changes']}, {'added', 'removed'})
        self.store.apply_import(preview)
        self.assertEqual(len(self.store.payments()), 2)
        self.assertIn(old[-1].id, {p.id for p in self.store.payments()})

    def test_stale_or_tampered_preview_never_changes_data(self):
        preview = self.store.preview_import_file(self.csv_file(self.store.payments()))
        self.store.set_balance(-12300)
        before, backups = self.store._db_signature(), self.store.list_backups()
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.store.apply_import(preview)
        self.assertEqual(before, self.store._db_signature())
        self.assertEqual(backups, self.store.list_backups())
        preview = self.store.preview_import_file(self.csv_file(self.store.payments()))
        preview['rows'][0] = replace(preview['rows'][0], counterparty='Подмена')
        with self.assertRaisesRegex(ValueError, 'изменён'):
            self.store.apply_import(preview)
        self.assertEqual(before, self.store._db_signature())

    def test_import_failure_rolls_back_every_write(self):
        preview = self.store.preview_import_file(self.csv_file(sample_case('clean')[0]))
        before = self.store._db_signature()
        with patch.object(self.store, '_write_payments', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self.store.apply_import(preview)
        self.assertEqual(before, self.store._db_signature())
        self.assertFalse(self.store.db.in_transaction)

    def test_bad_csv_preserves_payments_reviews_and_backups(self):
        self.store.set_duplicate_review(self.duplicate_id(), 'confirmed')
        before, backups = self.store._db_signature(), self.store.list_backups()
        bad = self.root / 'bad.csv'
        bad.write_text('date;counterparty;amount\n2026-99-99;X;-20\n', encoding='utf-8')
        with self.assertRaises(ImportErrorCSV):
            self.store.preview_import_file(bad)
        self.assertEqual(before, self.store._db_signature())
        self.assertEqual(backups, self.store.list_backups())

    def test_backup_restore_preserves_all_data_and_can_be_undone(self):
        pid = self.duplicate_id()
        self.store.set_duplicate_review(pid, 'confirmed')
        self.store.set_balance(-50000, date(2026, 10, 20))
        sid = self.store.save_scenario('Задержка', {'income_delay_days': 7})
        signature = self.store._db_signature()
        backup = self.store.create_backup()
        self.store.set_balance(90000)
        changed = self.store._db_signature()
        undo = self.store.restore_backup(backup)
        self.assertEqual(signature, self.store._db_signature())
        self.assertEqual(self.store.review_statuses(), {pid: 'confirmed'})
        self.assertEqual(self.store.get_scenario(sid)['parameters'], {'income_delay_days': 7})
        self.assertIn(backup, self.store.list_backups())
        self.store.restore_backup(undo)
        self.assertEqual(changed, self.store._db_signature())

    def test_invalid_restore_is_atomic_for_corruption_and_bad_values(self):
        before, backups = self.store._db_signature(), self.store.list_backups()
        garbage = self.root / 'garbage.sqlite3'
        garbage.write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            self.store.restore_backup(garbage)
        self.assertEqual(before, self.store._db_signature())
        self.assertEqual(backups, self.store.list_backups())
        for sql in ["UPDATE settings SET value='2026-99-99' WHERE key='balance_date'",
                    "UPDATE settings SET value='100000000001' WHERE key='balance'",
                    "UPDATE settings SET value='2' WHERE key='balance_needs_reconciliation'",
                    'UPDATE payments SET amount_kopecks=0 WHERE id=1',
                    "UPDATE payments SET status='sent' WHERE id=1"]:
            bad = self.store.create_backup()
            with closing(sqlite3.connect(bad)) as db, db:
                db.execute(sql)
            count = len(self.store.list_backups())
            with self.assertRaises(ValueError):
                self.store.restore_backup(bad)
            self.assertEqual(before, self.store._db_signature())
            self.assertEqual(count, len(self.store.list_backups()))

    def test_restore_copies_expected_data_without_executing_backup_triggers(self):
        backup = self.store.create_backup()
        with closing(sqlite3.connect(backup)) as db, db:
            db.execute("""CREATE TRIGGER suspicious AFTER INSERT ON payments
                          BEGIN DELETE FROM settings; END""")
        self.store.set_balance(12300)
        self.store.restore_backup(backup)
        self.assertEqual(self.store.opening_balance(), 30000000)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone())

    def test_legacy_balance_has_unknown_date_and_new_balances_allow_overdraft(self):
        legacy = self.root / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE payments(id INTEGER PRIMARY KEY,payment_date TEXT NOT NULL,
                counterparty TEXT NOT NULL,inn TEXT NOT NULL,account TEXT NOT NULL,bik TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL,reference TEXT NOT NULL,status TEXT NOT NULL);
                INSERT INTO settings VALUES('balance','-100');
                INSERT INTO payments VALUES(1,'2026-10-24','X','','','',-100,'A','planned');''')
        with_legacy = Store(legacy)
        try:
            self.assertIsNone(with_legacy.opening_balance_date())
            self.assertEqual(with_legacy.opening_balance(), -100)
            with_legacy.set_balance(-100_000_000_000, date(2026, 10, 23))
            self.assertEqual(with_legacy.opening_balance_date(), date(2026, 10, 23))
            with self.assertRaises(ValueError):
                with_legacy.set_balance(-100_000_000_001)
            with_legacy.load_case('clean')
            self.assertEqual(with_legacy.opening_balance_date(), date.today())
            self.assertTrue(with_legacy.list_backups())
        finally:
            with_legacy.close()

    def test_status_and_date_change_reset_affected_reviews_without_changing_balance(self):
        self.store.set_duplicate_review(self.duplicate_id(), 'dismissed')
        balance = (self.store.opening_balance(), self.store.opening_balance_date())
        when = date.today() + timedelta(days=20)
        self.store.set_payment_status(1, 'completed', when)
        self.assertEqual(self.store.review_statuses(), {})
        updated = next(p for p in self.store.payments() if p.id == 1)
        self.assertEqual((updated.status, updated.payment_date), ('completed', when))
        self.assertEqual(balance, (self.store.opening_balance(), self.store.opening_balance_date()))
        before = self.store._db_signature()
        with self.assertRaises(ValueError):
            self.store.set_payment_status(9999, 'completed')
        self.assertEqual(before, self.store._db_signature())

    def test_scenarios_are_definitions_and_report_changed_input_data(self):
        parameters = {'excluded_ids': [2], 'defer_days': 5, 'revenue_drop_pct': 10}
        sid = self.store.save_scenario('Проверка', parameters)
        self.assertFalse(self.store.get_scenario(sid)['stale'])
        self.store.set_balance(-100)
        self.assertTrue(self.store.get_scenario(sid)['stale'])
        self.assertEqual(self.store.get_scenario(sid)['parameters'], parameters)
        self.assertEqual(self.store.save_scenario('Проверка', {'income_delay_days': 2}), sid)
        self.assertFalse(self.store.get_scenario(sid)['stale'])
        self.assertEqual(len(self.store.scenarios()), 1)
        with self.assertRaises(ValueError):
            self.store.save_scenario('Invalid', {'amount': float('nan')})
        self.store.delete_scenario(sid)
        self.assertEqual(self.store.scenarios(), [])
        with self.assertRaises(ValueError):
            self.store.get_scenario(sid)

    def test_status_execution_always_requires_new_balance_reconciliation(self):
        self.store.set_balance(12300, date.today())
        self.assertFalse(self.store.balance_needs_reconciliation())
        self.store.set_payment_status(1, 'completed')
        self.assertEqual(self.store.opening_balance_date(), date.today())
        self.assertEqual(self.store.opening_balance(), 12300)
        self.assertTrue(self.store.balance_needs_reconciliation())
        self.store.set_payment_status(1, 'completed')
        self.assertTrue(self.store.balance_needs_reconciliation(), 'An unchanged status cannot clear the flag')
        self.store.set_balance(12300, date.today())
        self.assertFalse(self.store.balance_needs_reconciliation())
        self.store.set_payment_status(1, 'planned')
        self.assertTrue(self.store.balance_needs_reconciliation())
        self.store.load_case('clean')
        self.assertFalse(self.store.balance_needs_reconciliation())

    def test_completed_csv_changes_invalidate_balance_but_snapshot_reorder_does_not(self):
        original = self.store.payments()
        self.store.set_balance(30000000)
        changed = [replace(p, status='completed') if p.id == 1 else p for p in original]
        self.store.import_file(self.csv_file(changed))
        self.assertTrue(self.store.balance_needs_reconciliation())
        self.assertEqual(self.store.opening_balance_date(), date.today())
        self.assertEqual(self.store.opening_balance(), 30000000)
        self.store.set_balance(15000000)
        self.store.import_file(self.csv_file(list(reversed(changed))))
        self.assertFalse(self.store.balance_needs_reconciliation())
        # A planned payment update does not change what the actual balance includes.
        planned_changed = [replace(p, reference='New reference') if p.id == 3 else p for p in changed]
        self.store.import_file(self.csv_file(planned_changed))
        self.assertFalse(self.store.balance_needs_reconciliation())
        completed_changed = [replace(p, amount_kopecks=-99900) if p.status == 'completed' else p
                             for p in planned_changed]
        self.store.import_file(self.csv_file(completed_changed))
        self.assertTrue(self.store.balance_needs_reconciliation())

    def test_backup_restore_preserves_balance_reconciliation_requirement(self):
        self.store.set_payment_status(1, 'completed')
        backup = self.store.create_backup()
        self.store.set_balance(15000000)
        self.assertFalse(self.store.balance_needs_reconciliation())
        self.store.restore_backup(backup)
        self.assertTrue(self.store.balance_needs_reconciliation())


if __name__ == '__main__':
    unittest.main()
