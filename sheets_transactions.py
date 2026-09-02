"""
sheets_transactions.py
v1.4 - transactions CRUD backed by Google Sheets (replaces Supabase transactions table)

Changelog:
- v1.4: tx_mirror — Supabase-зеркало листа «Транзакции» (schema.sql). Как
        только у аккаунта появилась хотя бы одна строка в зеркале, чтение
        (/report, /history, /undo, insights.py) идёт ИЗ ЗЕРКАЛА, не из
        Sheets — сильно меньше вызовов Google API (и меньше шансов словить
        503 именно там, где пользователь ждёт ответа прямо сейчас).
        Источник правды по-прежнему Sheets: зеркало — лишь read-cache,
        который поддерживается в консистентности incremental-записями
        (mirror_upsert_row и т.п. после каждого успешного append/update) и
        подстрахован ежедневной сверкой (run_mirror_reconcile_sweep, см.
        main.py daily_cron) — если зеркало где-то разошлось (например,
        Supabase на секунду недоступен именно в момент mirror_upsert_row —
        сам этот вызов не роняет запись, просто тихо пропускает апдейт
        зеркала), сверка раз в сутки это обнаружит и пересоберёт зеркало
        заново из живого Sheets. Ручной аналог — команда /resync
        (см. resync.py), если не хочется ждать сутки.
        save_transaction/save_auto_expense также получили защиту от
        дублей (find_recent_duplicate, окно 120 сек) — повторный тап или
        повторно доставленный Telegram-апдейт больше не пишет вторую
        одинаковую строку.
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
categories, category_map, family graph) plus the tx_mirror read-cache —
see schema.sql.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import supabase_client as db
import sheets_client as sc
import google_api
import tx_logic
from sheets_client import to_float  # re-exported: history.py/undo.py use tx.to_float

STATUS_ACTIVE = tx_logic.STATUS_ACTIVE
STATUS_DELETED = tx_logic.STATUS_DELETED
logger = logging.getLogger(__name__)


class NoGoogleAccount(Exception):
    """Raised when the effective account has no Google Drive connected.
    Callers should tell the user to /start (or reconnect), not crash."""


def _get_account(user_id: int) -> dict:
    account = db.get_effective_google_account(user_id)
    if not account:
        raise NoGoogleAccount()
    return account


# --------------------------------------------------------------- reading ----
async def _load_tx_rows(account: dict) -> list[dict]:
    """Живое чтение листа «Транзакции» из Sheets, В ОБХОД зеркала. Нужно
    для первой гидратации, для /resync и для дедуп-проверки (та должна
    видеть самые свежие данные, а не потенциально секунду устаревший кэш)."""
    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do(token):
            return await sc.get_rows(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
                use_cache=False,
            )
        return await google_api.call(box, _do)


async def _all_tx_rows_for_account(account: dict) -> list[dict]:
    """Как _all_tx_rows, но принимает уже resolved account — нужно там, где
    account уже на руках (save_transaction/save_auto_expense), чтобы не
    резолвить его дважды за одну операцию. Обёртка asyncio.to_thread вокруг
    db.mirror_* обязательна: клиент Supabase синхронный, без неё эти вызовы
    блокировали бы event loop целиком (тормозило бы ВСЕХ пользователей
    бота, а не только текущего)."""
    mirrored = await asyncio.to_thread(db.mirror_list_rows, account["id"])
    if mirrored:
        return [tx_logic.mirror_to_sheet_row(m) for m in mirrored]

    rows = await _load_tx_rows(account)
    await asyncio.to_thread(db.mirror_hydrate, account["id"], rows)
    return rows


async def _all_tx_rows(user_id: int) -> list[dict]:
    """Сначала зеркало в Supabase (после первой гидратации). Если таблицы
    ещё нет или она пустая для этого аккаунта — читаем Sheets и наполняем
    зеркало. Падение зеркала (Supabase недоступен и т.п.) не ломает бота —
    просто каждый раз читаем Sheets заново, как было до v1.4."""
    account = _get_account(user_id)
    return await _all_tx_rows_for_account(account)


async def get_transactions_since(user_id: int, since: datetime) -> list[dict]:
    rows = await _all_tx_rows(user_id)
    return tx_logic.filter_active_in_range(rows, since, None)


async def get_transactions_in_range(user_id: int, since: datetime | None,
                                    until: datetime | None) -> list[dict]:
    """Как get_transactions_since, но с обеими границами — нужно для
    вопросов про конкретный прошлый месяц ("сколько потратил в июне"), где
    важно НЕ захватить данные после конца месяца. since/until=None —
    открытая граница с этой стороны."""
    rows = await _all_tx_rows(user_id)
    return tx_logic.filter_active_in_range(rows, since, until)


async def get_transactions_in_range_for_account(account: dict, since: datetime | None,
                                                until: datetime | None) -> list[dict]:
    """Как get_transactions_in_range, но принимает уже resolved account —
    нужно там, где обход идёт по аккаунтам напрямую (годовой/ежедневный
    sweep, см. narrative_report.py), а не по конкретному telegram user_id,
    для которого ещё пришлось бы отдельно резолвить effective account."""
    rows = await _all_tx_rows_for_account(account)
    return tx_logic.filter_active_in_range(rows, since, until)


async def get_history(user_id: int, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return await get_transactions_since(user_id, since)


async def get_report(user_id: int, since: datetime) -> dict:
    return await get_report_range(user_id, since, None)


async def get_report_range(user_id: int, since: datetime, until: datetime | None) -> dict:
    """Как get_report, но с явной верхней границей — нужно для отчёта по
    ЗАКРЫТОМУ прошлому периоду (report.py, кнопки предыдущих периодов),
    где нельзя просто "с такого-то числа и дальше", а нужно НЕ захватить
    данные уже следующего периода."""
    all_rows = await _all_tx_rows(user_id)
    period_rows = tx_logic.filter_active_in_range(all_rows, since, until)
    return tx_logic.report_from_rows(period_rows)


async def get_all_time_totals(user_id: int) -> tuple[float, float]:
    """(доход, расход) по всей истории — для баланса «на руках» в /report,
    который не привязан к текущему отчётному периоду."""
    all_rows = await _all_tx_rows(user_id)
    active = tx_logic.filter_active_in_range(all_rows, None, None)
    return tx_logic.sum_all_time(active)


# --------------------------------------------------------------- writing ----
async def save_transaction(user_id: int, who: str, amount: float, tx_type: str,
                           category: str, source: str, comment: str = "",
                           quantity: float | None = None, unit: str | None = None) -> str:
    account = _get_account(user_id)
    try:
        existing = await _all_tx_rows_for_account(account)
        dup = tx_logic.find_recent_duplicate(existing, who, amount, comment or "")
        if dup:
            return dup.get("ID") or ""
    except Exception:
        logger.exception("save_transaction: duplicate check failed, writing anyway")

    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_TRANSACTIONS,
                [sc.now_iso(), who, tx_type, category, amount, source, comment, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
            )
        row_id = await google_api.call(box, _do)

    await asyncio.to_thread(db.mirror_upsert_row, account["id"], {
        "ID": row_id, "Дата и время": sc.now_iso(), "Кто": who, "Тип": tx_type,
        "Категория": category, "Сумма": amount, "Источник": source,
        "Комментарий": comment, "Статус": STATUS_ACTIVE,
        "Количество": quantity if quantity is not None else "", "Единица": unit or "",
    })
    return row_id


async def save_auto_expense(user_id: int, who: str, amount: float, tx_type: str,
                            car_name: str, auto_type: str, description: str,
                            mileage: float | None, source: str,
                            quantity: float | None = None, unit: str | None = None) -> tuple[str, str]:
    """Writes both the general Транзакции row (so /report totals include
    it like any other expense) and the structured Авто row. Also logs a
    mileage point if one was mentioned in the message. Returns
    (transactions_row_id, auto_row_id)."""
    account = _get_account(user_id)
    try:
        existing = await _all_tx_rows_for_account(account)
        dup = tx_logic.find_recent_duplicate(existing, who, amount, description or "")
        if dup:
            return dup.get("ID") or "", ""
    except Exception:
        logger.exception("save_auto_expense: duplicate check failed, writing anyway")

    tx_id = sc.new_row_id()
    auto_id = sc.new_row_id()
    mileage_id = sc.new_row_id() if mileage is not None else None
    box = google_api.TokenBox(account)
    sid = account["google_spreadsheet_id"]
    now = sc.now_iso()
    async with sc.new_session() as session:
        async def _do_tx(token):
            if await sc.row_id_exists(session, token, sid, sc.SHEET_TRANSACTIONS, tx_id):
                return tx_id
            return await sc.append_row(
                session, token, sid, sc.SHEET_TRANSACTIONS,
                [now, who, tx_type, "Авто", amount, source, description, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
                row_id=tx_id,
            )
        tx_id = await google_api.call(box, _do_tx)

        async def _do_auto(token):
            if await sc.row_id_exists(session, token, sid, sc.SHEET_AUTO, auto_id):
                return auto_id
            return await sc.append_row(
                session, token, sid, sc.SHEET_AUTO,
                [now, car_name, auto_type, description, amount, mileage or "", who, STATUS_ACTIVE,
                 quantity if quantity is not None else "", unit or ""],
                row_id=auto_id,
            )
        auto_id = await google_api.call(box, _do_auto)

        if mileage_id is not None:
            async def _do_mileage(token):
                if await sc.row_id_exists(session, token, sid, sc.SHEET_MILEAGE, mileage_id):
                    return mileage_id
                return await sc.append_row(
                    session, token, sid, sc.SHEET_MILEAGE,
                    [now, car_name, mileage, "Из авто-траты", who],
                    row_id=mileage_id,
                )
            await google_api.call(box, _do_mileage)

    db.mirror_upsert_row(account["id"], {
        "ID": tx_id, "Дата и время": now, "Кто": who, "Тип": tx_type,
        "Категория": "Авто", "Сумма": amount, "Источник": source,
        "Комментарий": description, "Статус": STATUS_ACTIVE,
        "Количество": quantity if quantity is not None else "", "Единица": unit or "",
    })
    return tx_id, auto_id


async def save_mileage_point(user_id: int, who: str, car_name: str, mileage: float,
                             source: str = "Ручной ввод") -> str:
    account = _get_account(user_id)
    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE,
                [sc.now_iso(), car_name, mileage, source, who],
            )
        return await google_api.call(box, _do)


# --------------------------------------------------------------- /undo -------
async def soft_delete_last(user_id: int, who: str) -> dict | None:
    """Finds the most recent ACTIVE transaction attributed to `who` (not
    just anyone in a shared family sheet) and marks it Удалена. Returns the
    deleted row for the confirmation message, or None if nothing to undo."""
    account = _get_account(user_id)
    rows = await _all_tx_rows(user_id)
    last, _ = tx_logic.apply_soft_delete_last(rows, who)
    if not last:
        return None

    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do_update(token):
            return await sc.update_cell(
                session, token, account["google_spreadsheet_id"],
                sc.SHEET_TRANSACTIONS, last["ID"], "Статус", STATUS_DELETED,
            )
        await google_api.call(box, _do_update)

    db.mirror_mark_deleted(last["ID"])
    return last


# ------------------------------------------------------- category rename -----
async def rename_category_in_sheet(user_id: int, old_name: str, new_name: str) -> int:
    """Переименовать категорию во ВСЕХ строках листа «Транзакции» у
    эффективного аккаунта (и в зеркале). Если Google не подключён — 0, без
    исключения: метаданные (categories/category_map) в Supabase уже
    обновлены отдельно (supabase_client.rename_category), саму таблицу
    просто не трогаем в этом случае."""
    try:
        account = _get_account(user_id)
    except NoGoogleAccount:
        db.mirror_rename_category(None, old_name, new_name)
        return 0

    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do(token):
            return await sc.replace_column_value(
                session, token, account["google_spreadsheet_id"],
                sc.SHEET_TRANSACTIONS, "Категория", old_name, new_name,
            )
        changed = await google_api.call(box, _do)

    db.mirror_rename_category(account["id"], old_name, new_name)
    return changed


# ----------------------------------------------------------- reconciliation --
async def reconcile_mirror(account: dict) -> bool:
    """Сверяет число строк в зеркале с реальным Sheets; если разошлось —
    стирает зеркало (следующее чтение перегидрирует его заново с нуля).
    Возвращает True, если сброс произошёл. Ничего не делает, если зеркало
    ещё не гидрировано (mirror_count в (None, 0)) — там сверять не с чем,
    это нормальное "холодное" состояние, а не дрейф."""
    mirror_count = db.mirror_row_count(account["id"])
    if not mirror_count:
        return False
    try:
        live_rows = await _load_tx_rows(account)
    except Exception:
        logger.warning("reconcile_mirror: live Sheets read failed for account %s", account["id"])
        return False
    if len(live_rows) != mirror_count:
        logger.warning(
            "reconcile_mirror: drift detected for account %s (mirror=%d, sheets=%d) — clearing mirror",
            account["id"], mirror_count, len(live_rows),
        )
        db.mirror_clear(account["id"])
        return True
    return False


async def run_mirror_reconcile_sweep() -> int:
    """Раз в сутки (main.py daily_cron) — по одному live-чтению Sheets на
    уникальный аккаунт-владелец. Дороже, чем просто доверять зеркалу, но
    именно поэтому не на каждый /report, а раз в день: ловит "тихий" дрейф
    зеркала (см. changelog v1.4), который иначе никак не проявится, кроме
    как чуть неверными цифрами в /report/history — бессрочно."""
    fixed = 0
    seen_owner_ids: set[int] = set()
    for row in db.list_google_connected_users():
        owner_id = row["id"]
        if owner_id in seen_owner_ids:
            continue
        seen_owner_ids.add(owner_id)
        account = db.get_google_account(owner_id)
        if not account:
            continue
        try:
            if await reconcile_mirror(account):
                fixed += 1
        except Exception:
            logger.exception("run_mirror_reconcile_sweep: account %s failed", owner_id)
    return fixed
