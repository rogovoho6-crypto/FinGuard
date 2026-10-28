import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from backend.core.finguard_engine import run_forecast
from backend.core.scenario_actions import normalize_scenario, payment_identity
from backend.core.store import Payment, Store

START = date(2026, 10, 27)


def rows():
    return [Payment(i, START + timedelta(days=d), f'Поставщик {i}', '', '', '', amount, f'INV-{i}', state)
            for i, d, amount, state in [(1, 1, -10000, 'planned'), (2, 2, -20000, 'planned'),
                (3, 4, -30000, 'planned'), (4, 3, 50000, 'planned'), (5, 1, -99900, 'completed')]]


def action(payment, kind, days=7):
    return dict(payment_id=payment.id, payment_identity=payment_identity(payment), action=kind, defer_days=days)


class CascadeTests(unittest.TestCase):
    def test_mixed_actions_keep_independent_dates_and_remaining_obligations(self):
        payments = rows()
        result = run_forecast(payments, 10000, START, excluded_ids={1}, deferrals={2: 3, 3: 40})
        self.assertEqual(result['final_rub'], 400)
        self.assertEqual(result['min_rub'], 100)
        self.assertEqual(result['points'][5]['change_rub'], -200)
        self.assertEqual(result['deferred_beyond_horizon_rub'], 300)
        self.assertEqual(result['excluded_outgoing_rub'], 100)
        extended = run_forecast(payments, 10000, START, horizon=60,
                                excluded_ids={1}, deferrals={2: 3, 3: 40})
        self.assertEqual(extended['final_rub'], 100)
        self.assertEqual(extended['beyond_horizon_outgoing_rub'], 0)

    def test_global_shocks_apply_once_for_the_whole_cascade(self):
        result = run_forecast(rows(), 10000, START, excluded_ids={1}, deferrals={2: 3, 3: 40},
                              revenue_drop_pct=20, extra_expense_rub=10)
        self.assertEqual(result['final_rub'], 290)
        self.assertEqual(result['points'][10]['change_rub'], -10)

    def test_two_deferrals_can_have_different_delays(self):
        result = run_forecast(rows(), 10000, START, deferrals={1: 1, 2: 5})
        self.assertEqual(result['points'][1]['change_rub'], 0)
        self.assertEqual(result['points'][2]['change_rub'], -100)
        self.assertEqual(result['points'][7]['change_rub'], -200)

    def test_legacy_single_payment_deferral_remains_compatible(self):
        payments = rows()
        legacy = action(payments[0], 'defer', 7)
        normalized = normalize_scenario(legacy, payments)
        self.assertEqual(len(normalized['payment_actions']), 1)
        self.assertEqual(run_forecast(payments, 10000, START, deferred_ids={1}, defer_days=7),
                         run_forecast(payments, 10000, START, deferrals={1: 7}))

    def test_conflicting_actions_never_silently_win(self):
        with self.assertRaisesRegex(ValueError, 'одновременно'):
            run_forecast(rows(), 10000, START, excluded_ids={1}, deferrals={1: 3})
        with self.assertRaisesRegex(ValueError, 'разные сроки'):
            run_forecast(rows(), 10000, START, deferred_ids={1}, defer_days=2, deferrals={1: 3})

    def test_duplicate_actions_for_one_payment_are_rejected(self):
        payments = rows()
        with self.assertRaisesRegex(ValueError, 'один раз'):
            normalize_scenario({'payment_actions': [action(payments[0], 'exclude'),
                                                   action(payments[0], 'defer', 5)]}, payments)

    def test_a_changed_member_invalidates_the_entire_saved_cascade(self):
        payments = rows()
        params = normalize_scenario({'payment_actions': [action(payments[0], 'exclude'),
                                                          action(payments[1], 'defer', 5)]}, payments)
        changed = [replace(p, amount_kopecks=-20100) if p.id == 2 else p for p in payments]
        with self.assertRaisesRegex(ValueError, '№2 изменился'):
            normalize_scenario(params, changed)
        self.assertEqual(params['payment_actions'][1]['payment_identity']['amount_kopecks'], -20000)

    def test_missing_completed_and_incoming_operations_are_rejected(self):
        payments = rows()
        for p in [payments[3], payments[4], replace(payments[0], id=999)]:
            with self.subTest(payment=p.id), self.assertRaisesRegex(ValueError, 'планового расхода'):
                normalize_scenario({'payment_actions': [action(p, 'exclude')]}, payments)

    def test_exclusion_does_not_use_an_inactive_delay(self):
        payments = rows()
        params = normalize_scenario({'payment_actions': [action(payments[0], 'exclude', 999)]}, payments)
        self.assertEqual(params['payment_actions'][0]['defer_days'], 0)

    def test_invalid_deferral_types_and_bounds_are_rejected(self):
        for mapping in [{1: -1}, {1: 366}, {1: True}, {1: 2.5}, {True: 1}, {'1': 1}]:
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                run_forecast(rows(), 10000, START, deferrals=mapping)
        with self.assertRaises(ValueError):
            run_forecast(rows(), 10000, START, deferrals=[(1, 3)])

    def test_multi_action_parameters_roundtrip_through_sqlite(self):
        payments = rows()
        params = normalize_scenario({'payment_actions': [action(payments[0], 'exclude'),
                                                        action(payments[1], 'defer', 4)],
                                     'revenue_drop_pct': 20, 'extra_expense_rub': 10}, payments)
        self.assertEqual(params['action'], 'multiple')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scenario.sqlite3'
            db = Store(path)
            db.replace(payments, 10000)
            sid = db.save_scenario('Составной', params)
            db.close()
            db = Store(path)
            self.assertEqual(normalize_scenario(db.get_scenario(sid)['parameters'], db.payments()), params)
            db.close()

    def test_new_empty_plan_overrides_legacy_single_payment_fields(self):
        payments = rows()
        params = action(payments[0], 'exclude')
        params['payment_actions'] = []
        normalized = normalize_scenario(params, payments)
        self.assertEqual(normalized['payment_actions'], [])
        self.assertEqual(normalized['action'], 'none')

    def test_malformed_saved_parameters_do_not_get_clamped_by_widgets(self):
        for params in [{'payment_actions': None}, {'horizon': 40}, {'revenue_drop_pct': 101},
                       {'income_delay_days': -1}, {'extra_expense_rub': True}]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                normalize_scenario(params, rows())


if __name__ == '__main__':
    unittest.main()
