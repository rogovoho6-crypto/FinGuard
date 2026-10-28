"""Forecast regression cases use fixed dates and exact kopeck arithmetic."""
import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from backend.core.finguard_engine import make_dashboard, run_forecast
from backend.core.store import Payment


START = date(2026, 10, 24)


def payment(identifier, offset, kopecks, status='planned'):
    return Payment(identifier, START + timedelta(days=offset), f'Контрагент {identifier}',
                   '7707083893', '40702810200000000001', '044525225',
                   kopecks, f'Документ {identifier}', status)


class ForecastV61Tests(unittest.TestCase):
    def test_fixed_start_preserves_inclusive_horizon_and_old_keys(self):
        rows = [payment(1, 0, -125), payment(2, 2, 310), payment(3, 3, -500)]
        result = run_forecast(rows, 1000, START, horizon=2)
        self.assertEqual([p['date'] for p in result['points']],
                         ['2026-10-24', '2026-10-25', '2026-10-26'])
        self.assertEqual(result['final_rub'], 11.85)
        self.assertEqual(result['min_rub'], 8.75)
        self.assertEqual(result['min_date'], START.isoformat())
        self.assertIsNone(result['first_cash_gap_date'])
        self.assertEqual(result['balance_date'], START.isoformat())
        self.assertTrue(result['balance_is_current'])
        self.assertEqual(result['negative_days'], 0)
        self.assertEqual(result['max_shortfall_rub'], 0)

    def test_deferral_keeps_obligation_and_distinguishes_existing_later_expense(self):
        rows = [payment(1, 2, -12500), payment(2, 5, -3300)]
        baseline = run_forecast(rows, 20000, START, horizon=3)
        result = run_forecast(rows, 20000, START, horizon=3,
                              deferred_ids={1, 2}, defer_days=2)
        self.assertEqual(baseline['final_rub'], 75)
        self.assertEqual(result['final_rub'], 200)
        self.assertEqual(result['beyond_horizon_outgoing_rub'], 158)
        self.assertEqual(result['deferred_beyond_horizon_rub'], 125)
        self.assertEqual(result['obligations_beyond_horizon'], [
            {'id': 1, 'counterparty': 'Контрагент 1', 'original_date': '2026-10-26',
             'effective_date': '2026-10-28', 'amount_rub': -125.0},
            {'id': 2, 'counterparty': 'Контрагент 2', 'original_date': '2026-10-29',
             'effective_date': '2026-10-31', 'amount_rub': -33.0},
        ])
        self.assertTrue(any('За горизонтом' in w for w in result['warnings']))

    def test_expense_on_last_day_is_included_and_not_beyond(self):
        result = run_forecast([payment(1, 1, -100)], 1000, START,
                              horizon=3, deferred_ids={1}, defer_days=2)
        self.assertEqual(result['final_rub'], 9)
        self.assertEqual(result['obligations_beyond_horizon'], [])
        self.assertEqual(result['deferred_beyond_horizon_rub'], 0)

    def test_negative_day_count_shortfall_and_recovery(self):
        rows = [payment(1, 0, -1250), payment(2, 2, 200), payment(3, 3, 100)]
        result = run_forecast(rows, 1000, START, horizon=4)
        self.assertEqual(result['negative_days'], 3)
        self.assertEqual(result['max_shortfall_rub'], 2.50)
        self.assertEqual(result['min_rub'], -2.50)
        self.assertEqual(result['first_cash_gap_date'], START.isoformat())
        self.assertEqual(result['final_rub'], 0.50)

    def test_negative_opening_balance_counts_even_without_operations(self):
        result = run_forecast([], -1, START, horizon=1)
        self.assertEqual(result['negative_days'], 2)
        self.assertEqual(result['max_shortfall_rub'], 0.01)
        self.assertEqual(result['min_date'], START.isoformat())

    def test_overdue_outgoing_is_due_today_and_overdue_income_cannot_fund_it(self):
        rows = [payment(1, -3, -15000), payment(2, -1, 20000), payment(3, 1, 1000)]
        result = run_forecast(rows, 10000, START, horizon=2)
        self.assertEqual(result['points'][0]['change_rub'], -150)
        self.assertEqual(result['points'][0]['balance_rub'], -50)
        self.assertEqual(result['final_rub'], -40)
        self.assertEqual(result['overdue_outgoing_rub'], 150)
        self.assertEqual(result['overdue_incoming_rub'], 200)
        self.assertEqual(result['overdue_payments'][0]['effective_date'], START.isoformat())
        self.assertIsNone(result['overdue_payments'][1]['effective_date'])
        self.assertTrue(any('включены на 2026-10-24' in w for w in result['warnings']))
        self.assertTrue(any('поступления' in w and 'не включены' in w for w in result['warnings']))
        delayed = run_forecast(rows, 10000, START, horizon=3, income_delay_days=2)
        self.assertEqual(delayed['final_rub'], -40)
        self.assertEqual(delayed['overdue_incoming_rub'], 200)

    def test_overdue_deferral_moves_original_date_with_floor_today(self):
        rows = [payment(1, -3, -100)]
        still_due = run_forecast(rows, 1000, START, horizon=2,
                                 deferred_ids={1}, defer_days=2)
        self.assertEqual(still_due['points'][0]['change_rub'], -1)
        moved = run_forecast(rows, 1000, START, horizon=2,
                            deferred_ids={1}, defer_days=6)
        self.assertEqual(moved['points'][0]['change_rub'], 0)
        self.assertEqual(moved['deferred_beyond_horizon_rub'], 1)
        self.assertEqual(moved['overdue_payments'][0]['effective_date'], '2026-10-27')

    def test_completed_operations_are_already_in_the_opening_balance(self):
        rows = [payment(1, -1, -5000, 'completed'), payment(2, 0, 9000, 'completed'),
                payment(3, 40, -20000, 'completed')]
        result = run_forecast(rows, 12345, START)
        self.assertTrue(all(p['balance_rub'] == 123.45 for p in result['points']))
        self.assertEqual(result['overdue_payments'], [])
        self.assertEqual(result['obligations_beyond_horizon'], [])
        self.assertEqual(result['overdue_outgoing_rub'], 0)

    def test_balance_date_warning_never_replays_transactions_or_changes_balance(self):
        rows = [payment(1, -1, -5000, 'completed')]
        for offset in (-1, 1):
            with self.subTest(offset=offset):
                when = START + timedelta(days=offset)
                result = run_forecast(rows, 12345, START, balance_date=when)
                self.assertFalse(result['balance_is_current'])
                self.assertEqual(result['balance_date'], when.isoformat())
                self.assertEqual(result['final_rub'], 123.45)
                self.assertTrue(any('Обновите остаток' in w for w in result['warnings']))
        self.assertEqual(run_forecast([], 0, START, balance_date=START)['warnings'], [])

    def test_dashboard_uses_the_same_participating_income_and_expenses(self):
        rows = [payment(1, -1, -100), payment(2, -1, 500), payment(3, 30, -200),
                payment(4, 30, 300), payment(5, 31, -400), payment(6, 0, -600, 'completed')]
        result = make_dashboard(rows, 1000, START, balance_date=START - timedelta(days=1))
        self.assertEqual(result['income_30d_rub'], 3)
        self.assertEqual(result['expense_30d_rub'], 3)
        self.assertEqual(result['forecast']['final_rub'], 10)
        self.assertFalse(result['forecast']['balance_is_current'])

    def test_revenue_and_extra_expense_round_half_up_to_kopecks(self):
        rows = [payment(1, 0, 1), payment(2, 1, 3), payment(3, 2, -1)]
        result = run_forecast(rows, 0, START, horizon=2, revenue_drop_pct=50,
                              extra_expense_rub=Decimal('0.015'), extra_expense_day=2)
        self.assertEqual([p['change_rub'] for p in result['points']], [0.01, 0.02, -0.03])
        self.assertEqual(result['final_rub'], 0)
        self.assertEqual(result['min_rub'], 0)
        self.assertEqual(result['min_date'], '2026-10-26')

    def test_minimum_date_compares_kopecks_before_float_conversion(self):
        result = run_forecast([payment(1, 1, -1)], 10 ** 20, START, horizon=2)
        self.assertEqual(result['points'][0]['balance_rub'], result['points'][1]['balance_rub'])
        self.assertEqual(result['min_date'], '2026-10-25')

    def test_excluded_outgoing_is_reported_and_not_an_obligation(self):
        rows = [payment(1, -2, -100), payment(2, 40, -200), payment(3, 1, -300)]
        result = run_forecast(rows, 1000, START, excluded_ids={1, 2})
        self.assertEqual(result['excluded_outgoing_rub'], 3)
        self.assertEqual(result['overdue_outgoing_rub'], 0)
        self.assertEqual(result['obligations_beyond_horizon'], [])
        self.assertEqual(result['final_rub'], 7)

    def test_unknown_and_unavailable_selected_operations_are_not_silently_ignored(self):
        rows = [payment(1, 0, -100), payment(2, 0, 200), payment(3, 0, -100, 'completed')]
        for key in ('excluded_ids', 'deferred_ids'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'отсутствуют.*999'):
                    run_forecast(rows, 0, START, **{key: {999}})
                for identifier in (2, 3):
                    with self.assertRaisesRegex(ValueError, 'плановый расход'):
                        run_forecast(rows, 0, START, **{key: {identifier}})

    def test_public_scenario_parameter_bounds(self):
        invalid = [
            {'horizon': 0}, {'horizon': 366}, {'horizon': 1.5}, {'horizon': True},
            {'defer_days': -1}, {'defer_days': 366}, {'income_delay_days': -1},
            {'income_delay_days': 366}, {'revenue_drop_pct': -1}, {'revenue_drop_pct': 101},
            {'extra_expense_rub': -1}, {'extra_expense_rub': float('nan')},
            {'extra_expense_rub': float('inf')}, {'extra_expense_day': -1},
            {'extra_expense_day': 366}, {'extra_expense_day': 1.5},
            {'extra_expense_rub': 1, 'extra_expense_day': 31},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    run_forecast([], 0, START, **kwargs)
        result = run_forecast([], 0, START, horizon=365, defer_days=365,
                              income_delay_days=365, revenue_drop_pct=100,
                              extra_expense_rub=1, extra_expense_day=365)
        self.assertEqual(result['final_rub'], -1)
        self.assertEqual(result['negative_days'], 1)

    def test_extreme_date_deferral_reports_a_readable_error(self):
        row = payment(1, 0, -100)
        row.payment_date = date.max
        with self.assertRaisesRegex(ValueError, 'Дата операции.*диапазон'):
            run_forecast([row], 0, START, deferred_ids={1}, defer_days=1)


if __name__ == '__main__':
    unittest.main()
