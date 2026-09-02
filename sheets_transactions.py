"""
sheets_transactions.py
v1.4 - transactions CRUD backed by Google Sheets (replaces Supabase transactions table)

Changelog:
- v1.4: get_transactions_in_range_for_account() — narrative_report.py
        (годовой отчёт, run_annual_report_sweep) звал эту функцию, но её не
        было в файле вообще — модуль был бы обречён падать при импорте
        сразу, как только что-то реально вызвало бы годовой отчёт.
        get_transactions_in_range теперь тоже делегирует в общее ядро
        _get_transactions_in_range_for_account, без дублирования логики.
- v1.3: get_report_range() — get_report с явной верхней границей, для
        отчёта по ЗАКРЫТОМУ прошлому периоду (report.py, кнопки периодов).
        get_report теперь просто вызывает get_report_range(..., until=None).
- v1.2: save_transaction/save_auto_expense accept optional quantity/unit
        (see parser.extract_quantity) and append them as the two trailing
        columns added to Транзакции/Авто — see sheets_client.py's own
        changelog for why they're at the end and why old rows are safe.
- v1.1: every Sheets API call now goes through google_api.call() (refresh
        access token on 401, retry once) — see google_api.py's docstring
        for why this was missing and what it broke.

All money data now lives in the *effective* Google account's spreadsheet
(supabase_client.get_effective_google_account — own account, or the family
owner's if in a family). Supabase itself only holds metadata (users,
categories, category_map, family graph, PIN) — see schema.sql.
"""
from datetime import datetime, timedelta, timezone

import aiohttp

import supabase_client as db
import sheets_client as sc
import google_api
from sheets_client import to_float  # re-exported: history.py/undo.py use tx.to_float

STATUS_ACTIVE = "Активна"
STATUS_DELETED = "Удалена"


class NoGoogleAccount(Exception):
    """Raised when the effective account has no Google Drive connected.
    Callers should tell the user to /start (or reconnect), not crash."""


def _get_account(user_id: int) -> dict:
    account = db.get_effective_google_account(user_id)
    if not account:
        raise NoGoogleAccount()
    return account


# --------------------------------------------------------------- writing ----
async def save_transaction(user_id: int, who: str, amount: float, tx_type: str,
                           category: str, source: str, comment: str = "",
                           quantity: float | None = None, unit: str | None = None) -> str:
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
                [sc.now_iso(), who, tx_type, category, amount, source, comment, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
            )
        return await google_api.call(box, _do)


async def save_auto_expense(user_id: int, who: str, amount: float, tx_type: str,
                            car_name: str, auto_type: str, description: str,
                            mileage: float | None, source: str,
                            quantity: float | None = None, unit: str | None = None) -> tuple[str, str]:
    """Writes both the general Транзакции row (so /report totals include
    it like any other expense) and the structured Авто row. Also logs a
    mileage point if one was mentioned in the message. Returns
    (transactions_row_id, auto_row_id)."""
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do_tx(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
                [sc.now_iso(), who, tx_type, "Авто", amount, source, description, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
            )
        tx_id = await google_api.call(box, _do_tx)

        async def _do_auto(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_AUTO,
                [sc.now_iso(), car_name, auto_type, description, amount, mileage or "", who, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
            )
        auto_id = await google_api.call(box, _do_auto)

        if mileage is not None:
            async def _do_mileage(token):
                return await sc.append_row(
                    session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE,
                    [sc.now_iso(), car_name, mileage, "Из авто-траты", who],
                )
            await google_api.call(box, _do_mileage)

    return tx_id, auto_id


async def save_mileage_point(user_id: int, who: str, car_name: str, mileage: float,
                             source: str = "Ручной ввод") -> str:
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE,
                [sc.now_iso(), car_name, mileage, source, who],
            )
        return await google_api.call(box, _do)


# --------------------------------------------------------------- reading ----
def _parse_dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_transactions_since(user_id: int, since: datetime) -> list[dict]:
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
            )
        rows = await google_api.call(box, _do)

    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    result = []
    for r in rows:
        if r["Статус"] != STATUS_ACTIVE:
            continue
        dt = _parse_dt(r["Дата и время"])
        if dt is None:
            continue
        if dt >= since:
            result.append(r)
    result.sort(key=lambda r: r["Дата и время"], reverse=True)
    return result


async def get_report(user_id: int, since: datetime) -> dict:
    return await get_report_range(user_id, since, None)


async def get_report_range(user_id: int, since: datetime, until: datetime | None) -> dict:
    """Как get_report, но с явной верхней границей — нужно для отчёта по
    ЗАКРЫТОМУ прошлому периоду (report.py, кнопки предыдущих периодов),
    где нельзя просто "с such-то числа и дальше", а нужно НЕ захватить
    данные уже следующего периода."""
    rows = await get_transactions_in_range(user_id, since, until)
    income = sum(to_float(r["Сумма"]) for r in rows if r["Тип"] == "income")
    expense = sum(to_float(r["Сумма"]) for r in rows if r["Тип"] == "expense")

    by_category: dict[str, float] = {}
    for r in rows:
        if r["Тип"] == "expense":
            by_category[r["Категория"]] = by_category.get(r["Категория"], 0) + to_float(r["Сумма"])
    top5 = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]

    return {"income": income, "expense": expense, "balance": income - expense, "top5": top5}


async def get_history(user_id: int, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return await get_transactions_since(user_id, since)


async def _get_transactions_in_range_for_account(account: dict, since: datetime | None,
                                                  until: datetime | None) -> list[dict]:
    """Общее ядро для get_transactions_in_range/get_transactions_in_range_for_account
    — принимает уже готовый account, не занимается его резолвом по user_id."""
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
            )
        rows = await google_api.call(box, _do)

    if since is not None:
        since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    if until is not None:
        until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)

    result = []
    for r in rows:
        if r["Статус"] != STATUS_ACTIVE:
            continue
        dt = _parse_dt(r["Дата и время"])
        if dt is None:
            continue
        if since is not None and dt < since:
            continue
        if until is not None and dt > until:
            continue
        result.append(r)
    result.sort(key=lambda r: r["Дата и время"], reverse=True)
    return result


async def get_transactions_in_range(user_id: int, since: datetime | None,
                                    until: datetime | None) -> list[dict]:
    """Как get_transactions_since, но с обеими границами — нужно для
    вопросов про конкретный прошлый месяц ("сколько потратил в июне"), где
    важно НЕ захватить данные после конца месяца. since/until=None —
    открытая граница с этой стороны."""
    account = _get_account(user_id)
    return await _get_transactions_in_range_for_account(account, since, until)


async def get_transactions_in_range_for_account(account: dict, since: datetime | None,
                                                 until: datetime | None) -> list[dict]:
    """Как get_transactions_in_range, но принимает уже готовый account dict,
    а не user_id — нужно там, где эффективный аккаунт уже известен без
    похода через supabase_client.get_effective_google_account
    (narrative_report.run_annual_report_sweep, который итерирует
    db.list_google_connected_users() и работает с account напрямую)."""
    return await _get_transactions_in_range_for_account(account, since, until)


# --------------------------------------------------------------- /undo -------
async def soft_delete_last(user_id: int, who: str) -> dict | None:
    """Finds the most recent ACTIVE transaction attributed to `who` (not
    just anyone in a shared family sheet) and marks it Удалена. Returns the
    deleted row for the confirmation message, or None if nothing to undo."""
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do_get(token):
            return await sc.get_rows(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
            )
        rows = await google_api.call(box, _do_get)

        candidates = [r for r in rows if r["Статус"] == STATUS_ACTIVE and r["Кто"] == who]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r["Дата и время"], reverse=True)
        last = candidates[0]

        async def _do_update(token):
            return await sc.update_cell(
                session, token, account["google_spreadsheet_id"],
                sc.SHEET_TRANSACTIONS, last["ID"], "Статус", STATUS_DELETED,
            )
        await google_api.call(box, _do_update)

    return last
