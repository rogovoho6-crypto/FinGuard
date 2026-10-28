"""Local-only SQLite persistence and validated CSV import for FinGuard desktop."""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, replace as copy_payment
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


@dataclass
class Payment:
    id: int
    payment_date: date
    counterparty: str
    inn: str
    account: str
    bik: str
    amount_kopecks: int
    reference: str
    status: str = 'planned'


class ImportErrorCSV(ValueError):
    pass


FIELDS = ['date', 'counterparty', 'amount', 'inn', 'account', 'bik', 'reference', 'status']
MAX_ROWS = 2000
MAX_BYTES = 2 * 1024 * 1024


def to_kopecks(raw: str) -> int:
    try:
        value = Decimal(raw.strip().replace('\u00a0', '').replace(' ', '').replace(',', '.'))
        if not value.is_finite() or value == 0 or abs(value) > 1_000_000_000:
            raise ValueError()
        if value.as_tuple().exponent < -2:
            raise ValueError('У суммы должно быть не более 2 знаков после запятой')
        return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ImportErrorCSV(f'Некорректная сумма: {raw!r}') from exc


def parse_payment_date(value: str, line: int) -> date:
    """Accept ISO and Russian dates; never show raw Python errors to end users."""
    value = (value or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            from datetime import datetime
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ImportErrorCSV(
        f'Строка {line}: неверная дата «{value or "(пусто)"}». '
        'Используйте ГГГГ-ММ-ДД (2026-10-25) или ДД.ММ.ГГГГ (25.10.2026).'
    )


def parse_csv_bytes(content: bytes) -> list[Payment]:
    if len(content) > MAX_BYTES:
        raise ImportErrorCSV('Файл превышает 2 МБ')
    try:
        raw = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ImportErrorCSV('Нужна кодировка CSV UTF-8 (допустим UTF-8 с BOM)') from exc
    try:
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=',;')
            reader = csv.DictReader(io.StringIO(raw, newline=''), dialect=dialect)
        except csv.Error:
            reader = csv.DictReader(io.StringIO(raw, newline=''), delimiter=';')
        if not reader.fieldnames:
            raise ImportErrorCSV('Пустой заголовок CSV')
        names = [x.strip().lower() if x else '' for x in reader.fieldnames]
        if len(set(names)) != len(names):
            raise ImportErrorCSV('Дублирующиеся названия столбцов')
        if not {'date', 'counterparty', 'amount'}.issubset(names):
            raise ImportErrorCSV('Необходимы столбцы date, counterparty, amount')
        reader.fieldnames = names
        parsed = []
        for line, row in enumerate(reader, start=2):
            if len(parsed) >= MAX_ROWS:
                raise ImportErrorCSV(f'Максимум {MAX_ROWS} операций')
            if None in row or any(k is None for k in row):
                raise ImportErrorCSV(f'Строка {line}: лишние столбцы')
            try:
                when = parse_payment_date((row.get('date') or ''), line)
                who = (row.get('counterparty') or '').strip()
                if not who or len(who) > 180:
                    raise ValueError('Неверное название контрагента')
                amount = to_kopecks(row.get('amount') or '')
                status = (row.get('status') or 'planned').strip().lower()
                if status not in ('planned', 'completed'):
                    raise ValueError('status должен быть planned или completed')
                vals = {k: (row.get(k) or '').strip() for k in ('inn', 'account', 'bik', 'reference')}
                for k, limit in [('inn', 12), ('account', 32), ('bik', 9), ('reference', 100)]:
                    if len(vals[k]) > limit:
                        raise ValueError(f'Поле {k} слишком длинное')
                parsed.append(Payment(len(parsed)+1, when, who, vals['inn'], vals['account'],
                                      vals['bik'], amount, vals['reference'], status))
            except (ValueError, TypeError, ImportErrorCSV) as exc:
                raise ImportErrorCSV(str(exc) if str(exc).startswith(f'Строка {line}:') else f'Строка {line}: {exc}') from exc
        if not parsed:
            raise ImportErrorCSV('В файле нет операций')
        return parsed
    except csv.Error as exc:
        raise ImportErrorCSV(f'Ошибка CSV: {exc}') from exc


def _p(n: int, offset: int, counterparty: str, amount: int, reference: str,
       inn='7707083893', account='40702810200000000001', bik='044525225') -> Payment:
    return Payment(n, date.today() + timedelta(days=offset), counterparty, inn, account, bik, amount*100, reference)


def sample_case(case: str) -> tuple[list[Payment], int]:
    if case == 'duplicates':
        return [
            _p(1,2,'ООО Альфа',-150000,'INV-204'),_p(2,3,'ООО Альфа',-150000,'INV-204'),
            _p(3,4,'Аренда офиса',-65000,'RENT-10',account='40702810500000000002'),
            _p(4,7,'Клиент Север',280000,'SALE-22',inn='',account='',bik=''),
            _p(5,11,'Поставщик Бета',-48000,'INV-208',inn='123',account='40702810800000000003'),
            _p(6,14,'Клиент Юг',230000,'SALE-23',inn='',account='',bik=''),
            _p(7,17,'Зарплата',-170000,'PAY-10',inn='',account='',bik=''),
            _p(8,22,'Поставщик Гамма',-105000,'INV-215',account='40702810100000000004'),
            _p(9,25,'Клиент Восток',160000,'SALE-24',inn='',account='',bik=''),
        ], 30000000
    if case == 'clean':
        return [
            _p(1,1,'ООО Север',-25000,'A-100'),
            _p(2,5,'ООО Север',-20000,'A-101'),
            _p(3,6,'Покупатель',80000,'INCOME',inn='',account='',bik=''),
        ], 30000000
    if case == 'cash-gap':
        return [_p(1,1,'ООО Север',-380000,'GAP-1'),
                _p(2,12,'Клиент',460000,'PAY-IN',inn='',account='',bik='')], 30000000
    if case == 'bad-details':
        return [_p(1,2,'ООО Реквизит',-55000,'BAD-INN',inn='123'),
                _p(2,3,'ООО Счёт',-48000,'BAD-KEY',account='40702810200000000009'),
                _p(3,4,'ООО Пусто',-32000,'NO-DETAILS',inn='',account='',bik='')], 30000000
    raise ValueError('Неизвестный тестовый набор')


def demo_csv(case: str) -> bytes:
    rows, _ = sample_case(case)
    out = io.StringIO(newline='')
    writer = csv.writer(out, delimiter=';', lineterminator='\n')
    writer.writerow(FIELDS)
    for p in rows:
        writer.writerow([p.payment_date.isoformat(), p.counterparty,
                         str(Decimal(p.amount_kopecks)/100), p.inn, p.account, p.bik,
                         p.reference, p.status])
    return ('\ufeff'+out.getvalue()).encode('utf-8')


def default_db_path() -> Path:
    base = os.environ.get('APPDATA') if os.name == 'nt' else None
    if base:
        return Path(base) / 'FinGuard' / '6.1' / 'finguard.sqlite3'
    return Path.home() / '.finguard' / '6.1' / 'finguard.sqlite3'


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY, payment_date TEXT NOT NULL, counterparty TEXT NOT NULL,
                inn TEXT NOT NULL, account TEXT NOT NULL, bik TEXT NOT NULL,
                amount_kopecks INTEGER NOT NULL, reference TEXT NOT NULL, status TEXT NOT NULL);
        ''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS payment_reviews (
            payment_id INTEGER PRIMARY KEY,
            verdict TEXT NOT NULL CHECK(verdict IN ('confirmed', 'dismissed')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE CASCADE
        )''')
        review_columns = {r[1] for r in self.db.execute('PRAGMA table_info(payment_reviews)')}
        for column, kind in [('related_payment_id', 'INTEGER'),
                             ('payment_fingerprint', 'TEXT'), ('related_fingerprint', 'TEXT')]:
            if column not in review_columns:
                self.db.execute(f'ALTER TABLE payment_reviews ADD COLUMN {column} {kind}')
        self.db.execute('''CREATE TABLE IF NOT EXISTS scenarios (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            parameters TEXT NOT NULL, data_signature TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        self.db.commit()
        if not self.db.execute('SELECT 1 FROM settings WHERE key="balance"').fetchone():
            self.load_case('duplicates')

    def close(self):
        self.db.close()

    def opening_balance(self) -> int:
        return int(self.db.execute("SELECT value FROM settings WHERE key='balance'").fetchone()[0])

    def opening_balance_date(self) -> date | None:
        row = self.db.execute("SELECT value FROM settings WHERE key='balance_date'").fetchone()
        return date.fromisoformat(row[0]) if row else None

    def balance_needs_reconciliation(self) -> bool:
        row = self.db.execute("SELECT value FROM settings WHERE key='balance_needs_reconciliation'").fetchone()
        return bool(row and row[0] == '1')

    def last_import_at(self) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key='last_import_at'").fetchone()
        return row[0] if row else None

    @staticmethod
    def _fingerprint(p: Payment) -> tuple:
        return (p.payment_date.isoformat(), p.counterparty, p.inn, p.account,
                p.bik, p.amount_kopecks, p.reference, p.status)

    @staticmethod
    def _digest(payload) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _fingerprint_text(cls, p: Payment) -> str:
        return json.dumps(cls._fingerprint(p), ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _validate_balance(amount):
        if type(amount) is not int or not -100_000_000_000 <= amount <= 100_000_000_000:
            raise ValueError('Баланс должен быть от −1 млрд до 1 млрд ₽')

    @classmethod
    def _validate_rows(cls, rows):
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ValueError('Неверное количество операций')
        ids = set()
        for p in rows:
            if not isinstance(p, Payment) or type(p.id) is not int or not 0 < p.id < 2**63 or p.id in ids:
                raise ValueError('Некорректный или повторный ID операции')
            ids.add(p.id)
            if type(p.payment_date) is not date or p.status not in ('planned', 'completed'):
                raise ValueError('Некорректная дата или статус операции')
            if type(p.amount_kopecks) is not int or not 0 < abs(p.amount_kopecks) <= 100_000_000_000:
                raise ValueError('Некорректная сумма операции')
            for value, limit in [(p.counterparty, 180), (p.inn, 12), (p.account, 32),
                                 (p.bik, 9), (p.reference, 100)]:
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError('Некорректное поле операции')
            if not p.counterparty.strip():
                raise ValueError('Не указан контрагент')

    def data_signature(self) -> str:
        """Signature of forecast inputs; saved scenario results are never reused."""
        return self._digest({'payments': [(p.id, self._fingerprint(p)) for p in self.payments()],
                             'balance': self.opening_balance(),
                             'balance_date': str(self.opening_balance_date()),
                             'balance_needs_reconciliation': self.balance_needs_reconciliation()})

    def _db_signature(self) -> str:
        return self._digest({table: self.db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                             for table in ('settings', 'payments', 'payment_reviews', 'scenarios')})

    @staticmethod
    def _candidate_pairs(rows):
        from .finguard_engine import check_payments
        return {issue['payment_id']: issue['related_payment_id'] for issue in check_payments(rows)
                if issue['code'] == 'duplicate'}

    def _review_records(self):
        return self.db.execute('''SELECT payment_id,verdict,updated_at,related_payment_id,
                                 payment_fingerprint,related_fingerprint FROM payment_reviews''').fetchall()

    def _preserved_reviews(self, old, new, retained_ids):
        old_by_id = {p.id: p for p in old}
        new_by_id = {p.id: p for p in new}
        old_pairs, new_pairs = self._candidate_pairs(old), self._candidate_pairs(new)
        preserved = []
        for pid, verdict, timestamp, saved_related, saved_own, saved_other in self._review_records():
            related = old_pairs.get(pid)
            if pid not in retained_ids or related not in retained_ids or new_pairs.get(pid) != related:
                continue
            own, other = old_by_id[pid], old_by_id[related]
            if (self._fingerprint(own) != self._fingerprint(new_by_id[pid]) or
                    self._fingerprint(other) != self._fingerprint(new_by_id[related])):
                continue
            # Legacy reviews have no context; current old pair is the only available evidence.
            if saved_related is not None and (saved_related != related or
                    saved_own != self._fingerprint_text(own) or saved_other != self._fingerprint_text(other)):
                continue
            preserved.append((pid, verdict, timestamp, related,
                              self._fingerprint_text(own), self._fingerprint_text(other)))
        return preserved

    def _write_reviews(self, reviews):
        self.db.executemany('''INSERT INTO payment_reviews
            (payment_id,verdict,updated_at,related_payment_id,payment_fingerprint,related_fingerprint)
            VALUES(?,?,?,?,?,?)''', reviews)

    def _write_payments(self, rows):
        self.db.executemany('''INSERT INTO payments
            (id,payment_date,counterparty,inn,account,bik,amount_kopecks,reference,status)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            [(p.id, p.payment_date.isoformat(), p.counterparty, p.inn, p.account, p.bik,
              p.amount_kopecks, p.reference, p.status) for p in rows])

    def payments(self) -> list[Payment]:
        return [Payment(int(id), date.fromisoformat(d), who, inn, account, bik, int(amount), ref, status)
                for id, d, who, inn, account, bik, amount, ref, status in
                self.db.execute('SELECT id,payment_date,counterparty,inn,account,bik,amount_kopecks,reference,status FROM payments ORDER BY payment_date,id')]

    def replace(self, rows: list[Payment], opening_balance: int | None = None):
        self._validate_rows(rows)
        if opening_balance is not None:
            self._validate_balance(opening_balance)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM payments LIMIT 1').fetchone():
                self.create_backup()
            self.db.execute('DELETE FROM payment_reviews')
            self.db.execute('DELETE FROM payments')
            self._write_payments([copy_payment(p, id=i) for i, p in enumerate(rows, start=1)])
            if opening_balance is not None:
                self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                ('balance', str(opening_balance)))
                self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                ('balance_date', date.today().isoformat()))
                self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                ('balance_needs_reconciliation', '0'))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def review_statuses(self) -> dict[int, str]:
        """Manual decisions about duplicate candidates; never alter payment amounts."""
        return {int(pid): verdict for pid, verdict in self.db.execute(
            'SELECT payment_id, verdict FROM payment_reviews')}

    def set_duplicate_review(self, payment_id: int, verdict: str):
        if verdict not in ('pending', 'confirmed', 'dismissed'):
            raise ValueError('Неизвестный статус проверки')
        rows = self.payments()
        candidates = self._candidate_pairs(rows)
        if payment_id not in candidates:
            raise ValueError('Этот платёж не является кандидатом на дубль')
        with self.db:
            if verdict == 'pending':
                self.db.execute('DELETE FROM payment_reviews WHERE payment_id=?', (payment_id,))
            else:
                by_id = {p.id: p for p in rows}
                related = candidates[payment_id]
                self.db.execute('''INSERT INTO payment_reviews
                    (payment_id,verdict,related_payment_id,payment_fingerprint,related_fingerprint)
                    VALUES(?,?,?,?,?) ON CONFLICT(payment_id) DO UPDATE SET
                    verdict=excluded.verdict,updated_at=CURRENT_TIMESTAMP,
                    related_payment_id=excluded.related_payment_id,
                    payment_fingerprint=excluded.payment_fingerprint,
                    related_fingerprint=excluded.related_fingerprint''',
                    (payment_id, verdict, related, self._fingerprint_text(by_id[payment_id]),
                     self._fingerprint_text(by_id[related])))

    def set_balance(self, amount: int, as_of: date | None = None):
        self._validate_balance(amount)
        when = as_of if as_of is not None else date.today()
        if type(when) is not date:
            raise ValueError('Некорректная дата остатка')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('balance', str(amount)))
            self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('balance_date', when.isoformat()))
            self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', ('balance_needs_reconciliation', '0'))

    def set_payment_status(self, payment_id: int, status: str, payment_date: date | None = None):
        if status not in ('planned', 'completed'):
            raise ValueError('Неизвестный статус платежа')
        if payment_date is not None and type(payment_date) is not date:
            raise ValueError('Некорректная дата платежа')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.payments()
            if not any(p.id == payment_id for p in old):
                raise ValueError('Платёж не найден')
            new = [copy_payment(p, status=status, payment_date=payment_date or p.payment_date)
                   if p.id == payment_id else p for p in old]
            reviews = self._preserved_reviews(old, new, {p.id for p in old})
            self.db.execute('UPDATE payments SET status=?,payment_date=? WHERE id=?',
                (status, next(p.payment_date.isoformat() for p in new if p.id == payment_id), payment_id))
            if any(p.id == payment_id and p.status != status for p in old):
                self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                ('balance_needs_reconciliation', '1'))
            self.db.execute('DELETE FROM payment_reviews')
            self._write_reviews(reviews)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def load_case(self, name: str):
        rows, initial = sample_case(name)
        self.replace(rows, initial)

    def import_file(self, path: str | Path) -> int:
        return self.apply_import(self.preview_import_file(path))['total']

    def _import_preview(self, incoming: list[Payment]) -> dict:
        self._validate_rows(incoming)
        old = self.payments()
        old_groups = defaultdict(list)
        for p in old:
            old_groups[self._fingerprint(p)].append(p)
        new_counts = Counter(self._fingerprint(p) for p in incoming)
        next_id = max((p.id for p in old), default=0) + 1
        rows, retained, changes = [], set(), []
        ambiguous = 0
        for p in incoming:
            key = self._fingerprint(p)
            matches = old_groups[key]
            uncertain = len(matches) > 1 or new_counts[key] > 1
            if len(matches) == new_counts[key] == 1:
                result = copy_payment(p, id=matches[0].id)
                retained.add(result.id)
            else:
                result = copy_payment(p, id=next_id)
                next_id += 1
                ambiguous += int(uncertain)
                changes.append(dict(kind='ambiguous' if uncertain else 'added',
                    counterparty=p.counterparty, date=p.payment_date.isoformat(),
                    amount_rub=float(Decimal(p.amount_kopecks) / 100)))
            rows.append(result)
        for p in old:
            if p.id not in retained:
                changes.append(dict(kind='removed', counterparty=p.counterparty,
                    date=p.payment_date.isoformat(), amount_rub=float(Decimal(p.amount_kopecks) / 100)))
        preserved = self._preserved_reviews(old, rows, retained)
        preview = dict(rows=rows, total=len(rows), added=len(rows)-len(retained),
            unchanged=len(retained), removed=len(old)-len(retained),
            reviews_preserved=len(preserved), reviews_reset=len(self._review_records())-len(preserved),
            ambiguous=ambiguous, changes=changes, db_signature=self._db_signature())
        preview['_preview_signature'] = self._digest({
            'db': preview['db_signature'], 'rows': [(p.id, self._fingerprint(p)) for p in rows]})
        return preview

    def preview_import_file(self, path: str | Path) -> dict:
        source = Path(path)
        if source.stat().st_size > MAX_BYTES:
            raise ImportErrorCSV('Файл превышает 2 МБ')
        return self._import_preview(parse_csv_bytes(source.read_bytes()))

    def apply_import(self, preview: dict) -> dict:
        if not isinstance(preview, dict) or not isinstance(preview.get('rows'), list):
            raise ValueError('Некорректный предпросмотр импорта')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if preview.get('db_signature') != self._db_signature():
                raise ValueError('Данные изменились после предпросмотра. Откройте предпросмотр заново.')
            canonical = self._import_preview(preview['rows'])
            if preview.get('_preview_signature') != canonical['_preview_signature']:
                raise ValueError('Предпросмотр изменён. Откройте файл заново.')
            rows = canonical['rows']
            old = self.payments()
            old_ids = {p.id for p in old}
            reviews = self._preserved_reviews(old, rows, old_ids & {p.id for p in rows})
            backup = self.create_backup()
            self.db.execute('DELETE FROM payment_reviews')
            self.db.execute('DELETE FROM payments')
            self._write_payments(rows)
            self._write_reviews(reviews)
            completed_before = Counter(self._fingerprint(p) for p in old if p.status == 'completed')
            completed_after = Counter(self._fingerprint(p) for p in rows if p.status == 'completed')
            if completed_before != completed_after:
                self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                ('balance_needs_reconciliation', '1'))
            self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                ('last_import_at', datetime.now(timezone.utc).isoformat()))
            self.db.commit()
            return dict(total=len(rows), reviews_preserved=len(reviews),
                        reviews_reset=canonical['reviews_reset'], backup_path=str(backup))
        except Exception:
            self.db.rollback()
            raise

    def create_backup(self) -> Path:
        """Use a separate reader, also safe before edits inside BEGIN IMMEDIATE."""
        folder = self.path.parent / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
        target = folder / f'FinGuard_{stamp}_{uuid4().hex[:8]}.sqlite3'
        partial = target.with_suffix('.partial')
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(partial)) as dest:
                    source.backup(dest)
                    if dest.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise ValueError('Не удалось проверить резервную копию')
            partial.replace(target)
            return target
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def list_backups(self) -> list[Path]:
        folder = self.path.parent / 'backups'
        return sorted(folder.glob('FinGuard_*.sqlite3'), reverse=True) if folder.exists() else []

    def _read_backup(self, path: str | Path) -> dict:
        """Read and validate the whole snapshot before changing the active database."""
        try:
            source_path = Path(path).resolve()
            if not source_path.is_file():
                raise ValueError('Файл резервной копии не найден')
            with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
                source.execute('PRAGMA trusted_schema=OFF')
                source.execute('PRAGMA query_only=ON')
                if source.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Повреждённая база SQLite')
                if source.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Нарушены связи данных резервной копии')
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'settings', 'payments'}.issubset(tables):
                    raise ValueError('В файле нет данных FinGuard')
                for name, kind, sql in source.execute(
                        "SELECT name,type,sql FROM sqlite_master WHERE name IN ('settings','payments','payment_reviews','scenarios')"):
                    if kind != 'table' or not sql or 'CREATE VIRTUAL TABLE' in sql.upper():
                        raise ValueError('Неподдерживаемая схема резервной копии')
                settings = source.execute('SELECT key,value FROM settings').fetchall()
                if any(not isinstance(k, str) or not isinstance(v, str) for k, v in settings):
                    raise ValueError('Некорректные настройки')
                if len({k for k, _ in settings}) != len(settings):
                    raise ValueError('Повторные ключи настроек')
                values = dict(settings)
                self._validate_balance(int(values['balance']))
                if 'balance_date' in values:
                    date.fromisoformat(values['balance_date'])
                if 'last_import_at' in values:
                    datetime.fromisoformat(values['last_import_at'])
                if values.get('balance_needs_reconciliation', '0') not in ('0', '1'):
                    raise ValueError('Некорректная отметка сверки остатка')
                raw = source.execute('''SELECT id,payment_date,counterparty,inn,account,bik,
                                         amount_kopecks,reference,status FROM payments''').fetchall()
                rows = [Payment(pid, date.fromisoformat(when), who, inn, account, bik,
                                amount, ref, status) for pid, when, who, inn, account, bik, amount, ref, status in raw]
                self._validate_rows(rows)
                by_id = {p.id: p for p in rows}
                pairs = self._candidate_pairs(rows)
                reviews = []
                if 'payment_reviews' in tables:
                    cols = {r[1] for r in source.execute('PRAGMA table_info(payment_reviews)')}
                    extra = ',related_payment_id,payment_fingerprint,related_fingerprint' if {
                        'related_payment_id', 'payment_fingerprint', 'related_fingerprint'}.issubset(cols) else ''
                    seen = set()
                    for record in source.execute('SELECT payment_id,verdict,updated_at' + extra + ' FROM payment_reviews'):
                        pid, verdict, timestamp = record[:3]
                        if pid in seen or pid not in by_id or verdict not in ('confirmed', 'dismissed') or not isinstance(timestamp, str):
                            raise ValueError('Некорректная ручная проверка')
                        seen.add(pid)
                        related = pairs.get(pid)
                        if related is None:
                            raise ValueError('Ручная проверка не соответствует возможному дублю')
                        context = (related, self._fingerprint_text(by_id[pid]), self._fingerprint_text(by_id[related]))
                        if extra and record[3] is not None and tuple(record[3:]) != context:
                            raise ValueError('Ручная проверка относится к другой паре платежей')
                        reviews.append((pid, verdict, timestamp, *context))
                scenarios = []
                if 'scenarios' in tables:
                    names, ids = set(), set()
                    for sid, name, parameters, signature, timestamp in source.execute(
                            'SELECT id,name,parameters,data_signature,updated_at FROM scenarios'):
                        parsed = json.loads(parameters)
                        self._scenario_values(name, parsed)
                        if type(sid) is not int or not 0 < sid < 2**63 or sid in ids or name in names or not isinstance(signature, str) or not isinstance(timestamp, str):
                            raise ValueError('Некорректный сохранённый сценарий')
                        ids.add(sid)
                        names.add(name)
                        scenarios.append((sid, name, parameters, signature, timestamp))
                return dict(settings=settings, rows=rows, reviews=reviews, scenarios=scenarios)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError(f'Некорректная резервная копия SQLite: {exc}') from exc

    def restore_backup(self, path: str | Path) -> Path:
        snapshot = self._read_backup(path)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            before = self.create_backup()
            for table in ('payment_reviews', 'payments', 'settings', 'scenarios'):
                self.db.execute(f'DELETE FROM {table}')
            self.db.executemany('INSERT INTO settings(key,value) VALUES(?,?)', snapshot['settings'])
            self._write_payments(snapshot['rows'])
            self._write_reviews(snapshot['reviews'])
            self.db.executemany('INSERT INTO scenarios(id,name,parameters,data_signature,updated_at) VALUES(?,?,?,?,?)',
                                snapshot['scenarios'])
            self.db.commit()
            return before
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _scenario_values(name, parameters):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError('Название сценария должно содержать от 1 до 120 символов')
        if not isinstance(parameters, dict):
            raise ValueError('Параметры сценария должны быть словарём')
        try:
            encoded = json.dumps(parameters, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError('Некорректные параметры сценария') from exc
        if len(encoded.encode('utf-8')) > 100_000:
            raise ValueError('Слишком большой сценарий')
        return name.strip(), encoded

    def save_scenario(self, name: str, parameters: dict) -> int:
        name, encoded = self._scenario_values(name, parameters)
        with self.db:
            self.db.execute('''INSERT INTO scenarios(name,parameters,data_signature) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET parameters=excluded.parameters,
                data_signature=excluded.data_signature,updated_at=CURRENT_TIMESTAMP''',
                (name, encoded, self.data_signature()))
        return int(self.db.execute('SELECT id FROM scenarios WHERE name=?', (name,)).fetchone()[0])

    def scenarios(self) -> list[dict]:
        signature = self.data_signature()
        return [dict(id=sid, name=name, parameters=json.loads(parameters), updated_at=timestamp,
                     data_signature=saved, stale=saved != signature)
                for sid, name, parameters, saved, timestamp in self.db.execute(
                    'SELECT id,name,parameters,data_signature,updated_at FROM scenarios ORDER BY updated_at DESC,id DESC')]

    def get_scenario(self, scenario_id: int) -> dict:
        result = next((s for s in self.scenarios() if s['id'] == scenario_id), None)
        if result is None:
            raise ValueError('Сценарий не найден')
        return result

    def delete_scenario(self, scenario_id: int):
        with self.db:
            self.db.execute('DELETE FROM scenarios WHERE id=?', (scenario_id,))

    def export_json(self, target: str | Path, payload: dict):
        destination = Path(target)
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
