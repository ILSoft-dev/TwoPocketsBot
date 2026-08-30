"""Чистая логика транзакций — без Google/Supabase/Telegram.

Нужна и боту, и тестам: фильтр периода, баланс «на руках»,
поиск дубля, мягкое undo, переименование категории в памяти,
конвертация строк Sheets <-> зеркало в Supabase (tx_mirror).
"""
from datetime import datetime, timezone


STATUS_ACTIVE = "Активна"
STATUS_DELETED = "Удалена"


def parse_dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    cleaned = "".join(ch for ch in str(value).replace(",", ".") if ch.isdigit() or ch in ".-")
    return float(cleaned) if cleaned else 0.0


def filter_active_in_range(rows: list[dict], since: datetime | None,
                           until: datetime | None,
                           date_key: str = "Дата и время") -> list[dict]:
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    result = []
    for r in rows:
        if r.get("Статус") != STATUS_ACTIVE:
            continue
        dt = parse_dt(r.get(date_key))
        if dt is None:
            continue
        if since is not None and dt < since:
            continue
        if until is not None and dt > until:
            continue
        result.append(r)
    result.sort(key=lambda r: str(r.get(date_key) or ""), reverse=True)
    return result


def report_from_rows(rows: list[dict]) -> dict:
    """Принимает и уже отфильтрованные (filter_active_in_range), и сырые
    строки — фильтрует по STATUS_ACTIVE сама, на всякий случай, чтобы
    удалённая (/undo) запись никогда не могла просочиться в отчёт, даже
    если вызывающий код забыл отфильтровать заранее."""
    active = [r for r in rows if r.get("Статус") == STATUS_ACTIVE]
    income = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "income")
    expense = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "expense")
    by_category: dict[str, float] = {}
    for r in active:
        if r.get("Тип") == "expense":
            cat = r.get("Категория") or "Разное"
            by_category[cat] = by_category.get(cat, 0) + to_float(r.get("Сумма"))
    top5 = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]
    return {"income": income, "expense": expense, "balance": income - expense, "top5": top5}


def sum_all_time(all_rows: list[dict]) -> tuple[float, float]:
    """(общий доход, общий расход) по ВСЕМ активным записям — нужно для
    баланса «на руках», который не привязан к текущему отчётному периоду."""
    active = [r for r in all_rows if r.get("Статус") == STATUS_ACTIVE]
    income = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "income")
    expense = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "expense")
    return income, expense


def balance_on_hand(cash_on_hand, all_income: float, all_expense: float) -> float | None:
    """Начальный остаток с онбординга + все доходы − все расходы с начала учёта."""
    if cash_on_hand is None or cash_on_hand == "":
        return None
    try:
        start = float(cash_on_hand)
    except (TypeError, ValueError):
        return None
    return start + all_income - all_expense


def find_recent_duplicate(rows: list[dict], who: str, amount: float, comment: str,
                          now: datetime | None = None, window_seconds: int = 120) -> dict | None:
    """Идемпотентность: повтор той же траты за короткое окно не пишет вторую
    строку (двойной тап, повторно доставленный апдейт Telegram и т.п.)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    comment = (comment or "").strip()
    for r in rows:
        if r.get("Статус") != STATUS_ACTIVE:
            continue
        if (r.get("Кто") or "") != who:
            continue
        if abs(to_float(r.get("Сумма")) - amount) > 1e-9:
            continue
        if (r.get("Комментарий") or "").strip() != comment:
            continue
        dt = parse_dt(r.get("Дата и время"))
        if dt is None:
            continue
        if abs((now - dt).total_seconds()) <= window_seconds:
            return r
    return None


def apply_soft_delete_last(rows: list[dict], who: str) -> tuple[dict | None, list[dict]]:
    """Возвращает (удалённая_строка, новый_список). Не мутирует вход."""
    candidates = [r for r in rows if r.get("Статус") == STATUS_ACTIVE and r.get("Кто") == who]
    if not candidates:
        return None, list(rows)
    candidates = sorted(candidates, key=lambda r: str(r.get("Дата и время") or ""), reverse=True)
    last = dict(candidates[0])
    new_rows = []
    for r in rows:
        if r.get("ID") == last.get("ID"):
            updated = dict(r)
            updated["Статус"] = STATUS_DELETED
            new_rows.append(updated)
            last = updated
        else:
            new_rows.append(r)
    return last, new_rows


def rename_category_in_rows(rows: list[dict], old_name: str, new_name: str) -> tuple[list[dict], int]:
    changed = 0
    out = []
    for r in rows:
        row = dict(r)
        if row.get("Категория") == old_name:
            row["Категория"] = new_name
            changed += 1
        out.append(row)
    return out, changed


def sheet_row_to_mirror(owner_user_id: int, row: dict) -> dict:
    return {
        "sheet_row_id": row.get("ID"),
        "owner_user_id": owner_user_id,
        "who": row.get("Кто") or "",
        "type": row.get("Тип") or "",
        "category": row.get("Категория") or "",
        "amount": to_float(row.get("Сумма")),
        "date_time": row.get("Дата и время"),
        "source": row.get("Источник") or "",
        "comment": row.get("Комментарий") or "",
        "status": row.get("Статус") or STATUS_ACTIVE,
        "quantity": row.get("Количество") if row.get("Количество") not in (None, "") else None,
        "unit": row.get("Единица") or "",
    }


def mirror_to_sheet_row(m: dict) -> dict:
    return {
        "ID": m.get("sheet_row_id"),
        "Дата и время": m.get("date_time"),
        "Кто": m.get("who") or "",
        "Тип": m.get("type") or "",
        "Категория": m.get("category") or "",
        "Сумма": m.get("amount"),
        "Источник": m.get("source") or "",
        "Комментарий": m.get("comment") or "",
        "Статус": m.get("status") or STATUS_ACTIVE,
        "Количество": m.get("quantity") if m.get("quantity") is not None else "",
        "Единица": m.get("unit") or "",
    }
