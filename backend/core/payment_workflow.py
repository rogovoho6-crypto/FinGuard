"""UI-independent payment review and filtering helpers."""
from __future__ import annotations
from datetime import date

REVIEW_LABELS = {
    'pending': 'Ожидает ручной проверки',
    'confirmed': 'Риск подтверждён пользователем',
    'dismissed': 'Не дубль по результатам проверки',
}

def visible_issues(issues: list[dict], reviews: dict[int, str]) -> list[dict]:
    """Keep calculated signals separate from the user's current duplicate verdict."""
    return [x for x in issues
            if not (x['code'] == 'duplicate' and reviews.get(x['payment_id']) == 'dismissed')]

def match_payment(payment, issues_by_id: dict[int, list[dict]],
                  reviews: dict[int, str], mode: str, text: str) -> bool:
    raw = ' '.join(str(v) for v in (payment.id, payment.counterparty,
                payment.reference, payment.inn, payment.bik, payment.account,
                payment.amount_kopecks, payment.status)).casefold()
    if text.strip().casefold() not in raw:
        return False
    issues = issues_by_id.get(payment.id, [])
    duplicate = any(x['code'] == 'duplicate' for x in issues)
    if mode == 'all': return True
    if mode == 'flagged': return bool(issues)
    if mode == 'duplicate': return duplicate
    if mode == 'clean': return not issues
    if mode == 'reviewed': return reviews.get(payment.id) in ('confirmed', 'dismissed')
    if mode == 'overdue': return payment.status == 'planned' and payment.payment_date < date.today()
    raise ValueError('Неизвестный фильтр')
