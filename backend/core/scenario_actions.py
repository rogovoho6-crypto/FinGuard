"""Validate saved single-payment and cascading scenarios before any calculation."""
from __future__ import annotations


def payment_identity(payment):
    if payment is None:
        return None
    return dict(date=payment.payment_date.isoformat(), counterparty=payment.counterparty,
                inn=payment.inn, account=payment.account, bik=payment.bik,
                amount_kopecks=payment.amount_kopecks, reference=payment.reference,
                status=payment.status)


def normalize_scenario(parameters, rows):
    if not isinstance(parameters, dict):
        raise ValueError('Параметры сценария должны быть словарём.')
    if 'payment_actions' in parameters:
        actions = parameters['payment_actions']
        if not isinstance(actions, list):
            raise ValueError('Действия по платежам должны быть списком.')
    else:
        chosen = parameters.get('action', 'none')
        if chosen not in ('exclude', 'defer', 'none'):
            raise ValueError('Неизвестное действие сценария.')
        actions = [] if chosen == 'none' else [dict(
            payment_id=parameters.get('payment_id'), action=chosen,
            payment_identity=parameters.get('payment_identity'),
            defer_days=parameters.get('defer_days', 7))]
    by_id = {p.id: p for p in rows}
    seen, normalized = set(), []
    for item in actions:
        if not isinstance(item, dict):
            raise ValueError('Каждое действие должно содержать платёж и тип изменения.')
        pid, action = item.get('payment_id'), item.get('action')
        if type(pid) is not int or pid in seen:
            raise ValueError('Один платёж можно добавить в сценарий только один раз.')
        seen.add(pid)
        if action not in ('exclude', 'defer'):
            raise ValueError(f'Платёж №{pid}: выберите исключение или перенос.')
        payment = by_id.get(pid)
        if payment is None or payment.status != 'planned' or payment.amount_kopecks >= 0:
            raise ValueError(f'Выбранного планового расхода №{pid} больше нет. Выберите платёж заново и сохраните сценарий.')
        identity = item.get('payment_identity')
        if identity is None:
            raise ValueError(f'В сценарии не сохранены реквизиты операции №{pid}. Выберите платёж и сохраните сценарий заново.')
        if identity != payment_identity(payment):
            raise ValueError(f'Выбранный платёж №{pid} изменился после сохранения сценария. Проверьте операцию и сохраните параметры заново.')
        days = item.get('defer_days', 7) if action == 'defer' else 0
        if action == 'defer' and (type(days) is not int or not 1 <= days <= 90):
            raise ValueError(f'Платёж №{pid}: перенос должен составлять от 1 до 90 дней.')
        normalized.append(dict(payment_id=pid, payment_identity=identity,
                               action=action, defer_days=days))
    horizon = parameters.get('horizon', 30)
    if type(horizon) is not int or horizon not in (30, 60, 90):
        raise ValueError('Выберите горизонт сценария: 30, 60 или 90 дней.')
    for title, key, maximum in [('Снижение поступлений', 'revenue_drop_pct', 100),
                                ('Задержка поступлений', 'income_delay_days', 60),
                                ('Дополнительный расход', 'extra_expense_rub', 1_000_000_000)]:
        value = parameters.get(key, 0)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f'{title}: укажите целое число от 0 до {maximum}.')
    result = dict(schema_version=2, payment_actions=normalized,
                  horizon=horizon,
                  revenue_drop_pct=parameters.get('revenue_drop_pct', 0),
                  income_delay_days=parameters.get('income_delay_days', 0),
                  extra_expense_rub=parameters.get('extra_expense_rub', 0))
    # v6.1 can read a single action; it rejects a multi-action scenario explicitly.
    if len(normalized) == 1:
        result.update(normalized[0])
    else:
        result.update(action='multiple' if normalized else 'none', payment_id=None,
                      payment_identity=None, defer_days=7)
    return result
