"""Explainable deterministic checks and scenario arithmetic. No ML, no bank access."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def valid_inn(value: str) -> bool:
    if not value.isdigit() or len(value) not in (10, 12):
        return False
    digits = [int(c) for c in value]

    def checksum(weights):
        return sum(a * b for a, b in zip(digits, weights)) % 11 % 10

    if len(digits) == 10:
        return checksum([2, 4, 10, 3, 5, 9, 4, 6, 8]) == digits[9]
    return checksum([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == digits[10] and checksum(
        [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    ) == digits[11]



def valid_account_bik(account: str, bik: str) -> bool:
    """Mathematical control-key check for a standard Russian settlement account.

    Does not verify that the account exists, is open, or belongs to the recipient.
    Special treasury and correspondent accounts require different logic and are
    intentionally out of scope for this function.
    """
    if not (account.isdigit() and len(account) == 20 and bik.isdigit() and len(bik) == 9):
        return False
    combined = bik[-3:] + account
    weights = (7, 1, 3)
    return sum(int(digit) * weights[i % 3] for i, digit in enumerate(combined)) % 10 == 0


def settlement_account(account: str) -> bool:
    """Conservative eligibility test; don't mislabel special accounts."""
    return account.startswith(("407", "408", "405", "406"))


def money(kopecks: int) -> float:
    return float(Decimal(kopecks) / 100)


def check_payments(rows: list[Any]) -> list[dict]:
    """Flag issues by payment ID. Duplicate = candidate, not proof of erroneous transfer."""
    issues = []
    outbound = sorted(
        (p for p in rows if p.amount_kopecks < 0),
        key=lambda p: (p.payment_date, p.id),
    )
    for index, p in enumerate(outbound):
        def flag(code, title, detail, severity, exposure=0, related_id=None):
            issues.append({
                "id": f"{p.id}:{code}", "payment_id": p.id,
                "code": code, "title": title, "detail": detail,
                "severity": severity, "exposure_rub": money(exposure),
                "related_payment_id": related_id,
            })

        if p.inn and not valid_inn(p.inn):
            flag("inn", "Некорректный ИНН", "ИНН не проходит проверку формата или контрольных цифр. Проверьте его по первоисточнику.", "high")
        if p.account and (len(p.account) != 20 or not p.account.isdigit()):
            flag("account", "Ошибка формата счёта", "Номер расчётного счёта должен содержать 20 цифр. Проверка не подтверждает владельца счёта.", "high")
        if p.bik and (len(p.bik) != 9 or not p.bik.isdigit()):
            flag("bik", "Ошибка формата БИК", "БИК должен содержать 9 цифр. Наличие банка по БИК не проверялось.", "medium")
        if p.account and p.bik and settlement_account(p.account) and (len(p.account) == 20 and p.account.isdigit()) and (len(p.bik) == 9 and p.bik.isdigit()) and not valid_account_bik(p.account, p.bik):
            flag("account_key", "Не сошёлся контрольный ключ счёта", "Расчётный счёт математически не согласуется с БИК. Проверьте оба реквизита в платёжном документе.", "high")
        if not p.inn or not p.account:
            flag("incomplete", "Неполные реквизиты", "Не указан ИНН или счёт. Автоматическая проверка ограничена.", "medium")

        # Only a planned outgoing transaction can be prevented before execution.
        # Previously completed transactions may still serve as comparison candidates.
        if p.status != "planned":
            continue
        for other in reversed(outbound[:index]):
            if (p.payment_date - other.payment_date).days > 7:
                break
            same_payee = bool(p.account and other.account and p.account == other.account)
            same_amount = p.amount_kopecks == other.amount_kopecks
            same_ref = p.reference and other.reference and p.reference.strip().casefold() == other.reference.strip().casefold()
            if same_payee and same_amount and (same_ref or (p.payment_date - other.payment_date).days <= 2 and p.inn and p.inn == other.inn):
                rationale = "Совпадают счёт, сумма и " + ("назначение" if same_ref else "ИНН в пределах двух дней")
                flag("duplicate", "Возможный повторный платёж", f"{rationale}. Сравните с операцией №{other.id} перед отправкой.", "critical", abs(p.amount_kopecks), other.id)
                break
    severity_order = {"critical": 0, "high": 1, "medium": 2}
    return sorted(issues, key=lambda f: (severity_order[f["severity"]], f["payment_id"]))


def run_forecast(
    rows: list[Any], opening_kopecks: int, start: date, *,
    excluded_ids: set[int] | None = None, deferred_ids: set[int] | None = None,
    defer_days: int = 0, revenue_drop_pct: int = 0,
    income_delay_days: int = 0, extra_expense_rub: int = 0,
    extra_expense_day: int = 10, horizon: int = 30,
    balance_date: date | None = None,
    deferrals: dict[int, int] | None = None,
) -> dict:
    """Calculate closing daily balances, retaining obligations outside the chart.

    ``horizon`` keeps the original inclusive day 0..horizon convention. The
    opening balance is an actual snapshot on ``start`` including all completed
    payments, which must never be replayed. An older balance is reported, not adjusted.
    Overdue outflows are due now, whereas overdue income needs a new expected
    date before it can fund the forecast. Scenario deferrals move the original
    due date, with any remaining overdue outflow clamped to today.
    """
    def bounded_integer(name, value, low, high):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name}: требуется целое число от {low} до {high}.')

    bounded_integer('Горизонт прогноза', horizon, 1, 365)
    bounded_integer('Перенос расходов, дней', defer_days, 0, 365)
    bounded_integer('Задержка поступлений, дней', income_delay_days, 0, 365)
    bounded_integer('Снижение поступлений, %', revenue_drop_pct, 0, 100)
    bounded_integer('День дополнительного расхода', extra_expense_day, 0, 365)
    try:
        extra_rub = Decimal(str(extra_expense_rub))
        if not extra_rub.is_finite() or extra_rub < 0:
            raise ValueError()
        extra_kopecks = int((extra_rub * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Дополнительный расход должен быть неотрицательной конечной суммой.') from exc
    if extra_rub and extra_expense_day > horizon:
        raise ValueError('День дополнительного расхода должен находиться в горизонте прогноза.')
    if type(start) is not date or (balance_date is not None and type(balance_date) is not date):
        raise ValueError('Начало прогноза и дата остатка должны быть датами.')
    if isinstance(opening_kopecks, bool) or not isinstance(opening_kopecks, int):
        raise ValueError('Начальный остаток должен быть целым числом копеек.')

    balance_date = start if balance_date is None else balance_date
    excluded_ids = set(excluded_ids or ())
    deferred_ids = set(deferred_ids or ())
    if deferrals is not None and not isinstance(deferrals, dict):
        raise ValueError('Переносы платежей должны быть словарём ID и числа дней.')
    defer_by_id = {pid: defer_days for pid in deferred_ids}
    for pid, days in (deferrals or {}).items():
        bounded_integer('Перенос отдельного платежа, дней', days, 0, 365)
        if pid in defer_by_id and defer_by_id[pid] != days:
            raise ValueError('Для одного платежа указаны разные сроки переноса.')
        defer_by_id[pid] = days
    if excluded_ids & defer_by_id.keys():
        raise ValueError('Платёж нельзя одновременно исключить и перенести.')
    selected_ids = excluded_ids | defer_by_id.keys()
    if any(isinstance(i, bool) or not isinstance(i, int) for i in selected_ids):
        raise ValueError('Идентификаторы операций сценария должны быть целыми числами.')
    known_ids = {p.id for p in rows}
    missing_ids = selected_ids - known_ids
    if missing_ids:
        missing = ', '.join(str(i) for i in sorted(missing_ids))
        raise ValueError(f'Выбранные операции отсутствуют в текущих данных: {missing}. Выберите операции заново.')
    selectable_ids = {p.id for p in rows if p.status == 'planned' and p.amount_kopecks < 0}
    unavailable_ids = selected_ids - selectable_ids
    if unavailable_ids:
        unavailable = ', '.join(str(i) for i in sorted(unavailable_ids))
        raise ValueError(f'Для сценария нужен плановый расход. Операции недоступны: {unavailable}.')

    warnings = []
    if balance_date != start:
        warnings.append(
            f'Остаток указан на {balance_date.isoformat()}. Обновите остаток на '
            f'{start.isoformat()} с учётом исполненных операций: расчёт использует введённую сумму без корректировки.'
        )
    overdue_payments = []
    obligations_beyond_horizon = []
    overdue_outgoing = overdue_incoming = overdue_due_today = 0
    beyond_outgoing = deferred_beyond = excluded_outgoing = 0
    events: dict[int, int] = defaultdict(int)

    def operation(p, effective_day, amount):
        try:
            effective_date = (start + timedelta(days=effective_day)).isoformat() if effective_day is not None else None
        except OverflowError as exc:
            raise ValueError(f'Дата операции №{p.id} после переноса выходит за допустимый диапазон. Уточните дату.') from exc
        return {
            'id': p.id, 'counterparty': p.counterparty,
            'original_date': p.payment_date.isoformat(),
            'effective_date': effective_date,
            'amount_rub': money(amount),
        }

    for p in rows:
        if p.status != "planned":
            continue
        original_day = (p.payment_date - start).days
        day = original_day
        amount = p.amount_kopecks
        if amount < 0:
            if p.id in excluded_ids:
                excluded_outgoing -= amount
                continue
            if p.id in defer_by_id:
                day += defer_by_id[p.id]
            day = max(0, day)
            if original_day < 0:
                overdue_outgoing -= amount
                overdue_payments.append(operation(p, day, amount))
                if day == 0:
                    overdue_due_today -= amount
            if day > horizon:
                beyond_outgoing -= amount
                obligations_beyond_horizon.append(operation(p, day, amount))
                if p.id in defer_by_id and max(0, original_day) <= horizon:
                    deferred_beyond -= amount
        elif amount > 0:
            if original_day < 0:
                overdue_incoming += amount
                overdue_payments.append(operation(p, None, amount))
                continue
            day += income_delay_days
            amount = int((Decimal(amount) * Decimal(100 - revenue_drop_pct) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if 0 <= day <= horizon:
            events[day] += amount
    if extra_kopecks:
        events[extra_expense_day] -= extra_kopecks

    if overdue_due_today:
        warnings.append(
            f'Просроченные плановые расходы {money(overdue_due_today):.2f} ₽ '
            f'включены на {start.isoformat()}. Подтвердите фактические даты оплаты.'
        )
    if overdue_outgoing > overdue_due_today:
        warnings.append('Часть просроченных расходов перенесена сценарием; обязательства сохранены в расчёте или списке за горизонтом.')
    if overdue_incoming:
        warnings.append(
            f'Просроченные поступления {money(overdue_incoming):.2f} ₽ не включены в прогноз. '
            'Уточните ожидаемые даты получения денег.'
        )
    if beyond_outgoing:
        warnings.append(
            f'За горизонтом прогноза остаются расходы {money(beyond_outgoing):.2f} ₽. '
            'Они не уменьшают показанный конечный остаток, но обязательства сохраняются.'
        )

    balance = opening_kopecks
    points = []
    first_negative = None
    negative_days = 0
    minimum = None
    min_date = None
    for d in range(horizon + 1):
        balance += events[d]
        day = start + timedelta(days=d)
        if balance < 0:
            negative_days += 1
            if first_negative is None:
                first_negative = day.isoformat()
        if minimum is None or balance < minimum:
            minimum = balance
            min_date = day.isoformat()
        points.append({"date": day.isoformat(), "balance_rub": money(balance), "change_rub": money(events[d])})
    return {
        'points': points, 'final_rub': money(balance), 'min_rub': money(minimum),
        'min_date': min_date, 'first_cash_gap_date': first_negative,
        'balance_date': balance_date.isoformat(), 'balance_is_current': balance_date == start,
        'warnings': warnings, 'negative_days': negative_days,
        'max_shortfall_rub': money(max(0, -minimum)),
        'overdue_outgoing_rub': money(overdue_outgoing),
        'overdue_incoming_rub': money(overdue_incoming),
        'overdue_payments': sorted(overdue_payments, key=lambda p: (p['original_date'], p['id'])),
        'obligations_beyond_horizon': sorted(obligations_beyond_horizon, key=lambda p: (p['effective_date'], p['id'])),
        'beyond_horizon_outgoing_rub': money(beyond_outgoing),
        'deferred_beyond_horizon_rub': money(deferred_beyond),
        'excluded_outgoing_rub': money(excluded_outgoing),
    }


def make_dashboard(rows: list[Any], opening: int, start: date, *, balance_date: date | None = None) -> dict:
    issues = check_payments(rows)
    forecast = run_forecast(rows, opening, start, balance_date=balance_date)
    planned = [p for p in rows if p.status == "planned" and p.payment_date <= start + timedelta(days=30)]
    unique_flagged_ids = {f["payment_id"] for f in issues}
    return {
        "today": start.isoformat(),
        "opening_balance_rub": money(opening),
        "issues": issues, "flagged_payment_count": len(unique_flagged_ids),
        "possible_duplicate_exposure_rub": sum(f["exposure_rub"] for f in issues if f["code"] == "duplicate"),
        "income_30d_rub": money(sum(p.amount_kopecks for p in planned if p.amount_kopecks > 0 and p.payment_date >= start)),
        "expense_30d_rub": money(-sum(p.amount_kopecks for p in planned if p.amount_kopecks < 0)),
        "forecast": forecast,
    }
