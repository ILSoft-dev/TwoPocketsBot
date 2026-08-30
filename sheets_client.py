"""
sheets_client.py
v1.1 - Google Sheets storage layer (async, aiohttp, drive.file scope only)

Changelog:
- v1.1: Транзакции/Авто get two trailing columns, Количество/Единица —
        structured quantity extracted from free-text input ("5 литров
        масла" -> 5, "л"), see parser.extract_quantity. Appended at the
        END of each HEADERS list on purpose: get_rows() pads short rows
        with "" up to len(headers), so existing spreadsheets (created
        before this change, whose physical header row doesn't have these
        two cells) keep reading fine — old rows just come back with
        Количество/Единица == "". Their on-sheet header LABELS won't say
        "Количество"/"Единица" until the user adds those manually (or a
        future migration patches row 1) — cosmetic only, doesn't affect
        the bot, which keys rows off this HEADERS constant, not the
        sheet's actual header text.

Replaces Supabase for the actual money data (transactions, car expenses,
mileage history, car registry). Supabase stays for metadata that isn't
financial: users/onboarding, categories, category_map, family graph, PIN.

Confirmed against official Google docs: every Sheets API method used here
(spreadsheets.create, spreadsheets.batchUpdate, spreadsheets.values.append,
spreadsheets.values.batchUpdate, spreadsheets.values.get) explicitly accepts
the drive.file scope — no Sensitive "spreadsheets" scope needed, so this
reuses exactly the same OAuth client/scope already configured for PixKeep.

Sheet layout (each a separate tab within one spreadsheet per owner):
    Транзакции — id, дата и время, кто, тип, категория, сумма, источник, комментарий, статус, количество, единица
    Авто       — id, дата, машина, тип, описание, сумма, пробег, кто, статус, количество, единица
    Пробег     — id, дата, машина, пробег, источник, кто
    Машины     — id, машина, статус, дата регистрации, последнее напоминание

Every row gets its own short generated ID (Sheets has no server-side auto
increment) so a specific logical row can be found and updated later (e.g.
soft-delete on /undo) even if its physical row number shifts because of
manual edits by the user.
"""
import uuid
from datetime import datetime, timezone

import aiohttp

from config import GOOGLE_HTTP_TIMEOUT_SECONDS
import sheets_cache

API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3"


class GoogleAuthError(Exception):
    """Raised on HTTP 401 so the caller can refresh the token and retry."""


class TransientSheetsError(Exception):
    """
    Raised on 429/500/502/503/504 — Google's side is briefly overloaded or
    unavailable, not a real failure. Almost always resolves itself within
    a few seconds. google_api.call() catches this and retries with backoff
    instead of letting it fall straight through to the user as
    "не получилось обратиться к Google Диску" (misleading — that message
    implies an auth/connection problem, not a transient server hiccup).
    """


def new_session() -> aiohttp.ClientSession:
    """Единая точка создания aiohttp-сессии для запросов к Google API — с
    разумным таймаутом вместо аiohttp-дефолта в 5 минут на запрос. Без
    этого зависший/медленный ответ Google мог держать апдейт бота
    "в работе" по несколько минут, прежде чем пользователь вообще видел
    хоть какую-то реакцию (ошибку или успех)."""
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=GOOGLE_HTTP_TIMEOUT_SECONDS))


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


async def _check(resp: aiohttp.ClientResponse, ok=(200, 201)):
    if resp.status == 401:
        raise GoogleAuthError("Google token unauthorized")
    if resp.status in ok:
        return
    text = await resp.text()
    if resp.status in TRANSIENT_STATUSES:
        raise TransientSheetsError(f"Sheets API {resp.status}: {text}")
    raise RuntimeError(f"Sheets API {resp.status}: {text}")


# ------------------------------------------------------------- sheet layout ---
SHEET_TRANSACTIONS = "Транзакции"
SHEET_AUTO = "Авто"
SHEET_MILEAGE = "Пробег"
SHEET_CARS = "Машины"

HEADERS = {
    SHEET_TRANSACTIONS: [
        "ID", "Дата и время", "Кто", "Тип", "Категория",
        "Сумма", "Источник", "Комментарий", "Статус",
        "Количество", "Единица",
    ],
    SHEET_AUTO: [
        "ID", "Дата", "Машина", "Тип", "Описание",
        "Сумма", "Пробег", "Кто", "Статус",
        "Количество", "Единица",
    ],
    SHEET_MILEAGE: [
        "ID", "Дата", "Машина", "Пробег", "Источник", "Кто",
    ],
    SHEET_CARS: [
        "ID", "Машина", "Статус", "Дата регистрации", "Последнее напоминание",
    ],
}


def new_row_id() -> str:
    """Short, good-enough-unique row identifier (not a DB primary key, just
    something stable to find a logical row again after it's been appended).
    Prefixed with a letter on purpose: a pure-hex-digit string has a small
    but real chance (~0.7% per row) of looking like a plain number, and
    Google Sheets can then silently store/return it AS a number (stripping
    leading zeros) instead of text — breaking exact-string lookups later."""
    return f"r{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------- create file ---
async def create_budget_spreadsheet(session: aiohttp.ClientSession, token: str,
                                    title: str) -> str:
    """Create a new spreadsheet with all four tabs pre-populated with
    bold header rows. Returns the new spreadsheet's ID."""
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name in HEADERS],
    }
    async with session.post(API, json=body, headers=_headers(token)) as resp:
        await _check(resp, ok=(200,))
        data = await resp.json()
    spreadsheet_id = data["spreadsheetId"]
    # Map tab title -> sheetId, needed for the formatting batchUpdate below
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in data["sheets"]}

    # Write header rows
    value_ranges = [
        {"range": f"{name}!A1", "values": [cols]}
        for name, cols in HEADERS.items()
    ]
    async with session.post(
        f"{API}/{spreadsheet_id}/values:batchUpdate",
        json={"valueInputOption": "RAW", "data": value_ranges},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))

    # Bold header rows + freeze row 1, one request per tab
    requests = []
    for name in HEADERS:
        sid = sheet_ids[name]
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })
        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        })
    async with session.post(
        f"{API}/{spreadsheet_id}:batchUpdate",
        json={"requests": requests},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))

    return spreadsheet_id


# --------------------------------------------------------------- write rows --
async def create_plain_spreadsheet(session: aiohttp.ClientSession, token: str,
                                    title: str, sheets: dict[str, list[list]]) -> str:
    """Generic version of create_budget_spreadsheet, for one-off exports
    (e.g. a car's full history when it's removed) rather than the fixed
    4-tab budget layout. `sheets` maps tab name -> rows (first row treated
    as the header and bolded); an empty rows list still creates the tab,
    just with no data written."""
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name in sheets],
    }
    async with session.post(API, json=body, headers=_headers(token)) as resp:
        await _check(resp, ok=(200,))
        data = await resp.json()
    spreadsheet_id = data["spreadsheetId"]
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in data["sheets"]}

    value_ranges = [{"range": f"{name}!A1", "values": rows} for name, rows in sheets.items() if rows]
    if value_ranges:
        async with session.post(
            f"{API}/{spreadsheet_id}/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": value_ranges},
            headers=_headers(token),
        ) as resp:
            await _check(resp, ok=(200,))

    requests = [
        {"repeatCell": {
            "range": {"sheetId": sheet_ids[name], "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }}
        for name, rows in sheets.items() if rows
    ]
    if requests:
        async with session.post(
            f"{API}/{spreadsheet_id}:batchUpdate", json={"requests": requests}, headers=_headers(token),
        ) as resp:
            await _check(resp, ok=(200,))

    return spreadsheet_id


async def append_row(session: aiohttp.ClientSession, token: str,
                     spreadsheet_id: str, sheet_name: str,
                     values: list, row_id: str | None = None) -> str:
    """Append a row, auto-generating and prepending its ID (or reusing
    `row_id` if given — needed so a retried multi-step write, like
    save_auto_expense's Транзакции+Авто+Пробег trio, can check first
    whether its own earlier attempt already wrote this exact row instead
    of blindly appending a duplicate). Returns the ID."""
    row_id = row_id or new_row_id()
    row = [row_id] + values
    async with session.post(
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A:A:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
    await sheets_cache.invalidate(spreadsheet_id, sheet_name)
    return row_id


async def row_id_exists(session: aiohttp.ClientSession, token: str,
                        spreadsheet_id: str, sheet_name: str, row_id: str) -> bool:
    """Cheap existence check (just the ID column) — used before a retried
    append to avoid writing the same logical row twice."""
    async with session.get(
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A2:A",
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
        data = await resp.json()
    ids = [r[0] if r else "" for r in data.get("values", [])]
    return row_id in ids


# --------------------------------------------------------------- read rows ---
async def get_rows(session: aiohttp.ClientSession, token: str,
                   spreadsheet_id: str, sheet_name: str,
                   use_cache: bool = True) -> list[dict]:
    """Return all data rows as dicts keyed by header name (ID included)."""
    if use_cache:
        cached = await sheets_cache.get(spreadsheet_id, sheet_name)
        if cached is not None:
            return cached

    headers = HEADERS[sheet_name]
    last_col = chr(ord("A") + len(headers) - 1)
    async with session.get(
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A2:{last_col}",
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
        data = await resp.json()

    rows = []
    for raw in data.get("values", []):
        padded = raw + [""] * (len(headers) - len(raw))  # short rows -> pad
        rows.append(dict(zip(headers, padded)))
    await sheets_cache.set(spreadsheet_id, sheet_name, rows)
    return rows


async def find_row(session: aiohttp.ClientSession, token: str,
                   spreadsheet_id: str, sheet_name: str,
                   row_id: str) -> dict | None:
    rows = await get_rows(session, token, spreadsheet_id, sheet_name)
    for row in rows:
        if row["ID"] == row_id:
            return row
    return None


# ------------------------------------------------------------- update cell ---
async def update_cell(session: aiohttp.ClientSession, token: str,
                      spreadsheet_id: str, sheet_name: str,
                      row_id: str, column_name: str, new_value) -> bool:
    """Find the row by its ID and overwrite one cell in it.
    Returns False if the row_id wasn't found (e.g. user deleted it manually)."""
    headers = HEADERS[sheet_name]
    if column_name not in headers:
        raise ValueError(f"Unknown column {column_name!r} for sheet {sheet_name!r}")

    async with session.get(
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A2:A",
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
        id_column = await resp.json()

    ids = [r[0] if r else "" for r in id_column.get("values", [])]
    if row_id not in ids:
        return False
    row_index = ids.index(row_id) + 2  # +2: header row + 1-based sheet rows

    col_letter = chr(ord("A") + headers.index(column_name))
    async with session.put(
        f"{API}/{spreadsheet_id}/values/{sheet_name}!{col_letter}{row_index}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [[new_value]]},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
    await sheets_cache.invalidate(spreadsheet_id, sheet_name)
    return True


async def replace_column_value(session: aiohttp.ClientSession, token: str,
                               spreadsheet_id: str, sheet_name: str,
                               column_name: str, old_value: str, new_value: str) -> int:
    """Массово заменить значение колонки (для переименования категории).
    Читает лист без кэша, пишет batchUpdate, сбрасывает кэш. Возвращает
    число затронутых строк."""
    headers = HEADERS[sheet_name]
    if column_name not in headers:
        raise ValueError(f"Unknown column {column_name!r} for sheet {sheet_name!r}")

    rows = await get_rows(session, token, spreadsheet_id, sheet_name, use_cache=False)
    col_letter = chr(ord("A") + headers.index(column_name))
    data = []
    for i, row in enumerate(rows):
        if str(row.get(column_name, "")) == str(old_value):
            row_index = i + 2
            data.append({
                "range": f"{sheet_name}!{col_letter}{row_index}",
                "values": [[new_value]],
            })
    if not data:
        return 0

    async with session.post(
        f"{API}/{spreadsheet_id}/values:batchUpdate",
        json={"valueInputOption": "USER_ENTERED", "data": data},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
    await sheets_cache.invalidate(spreadsheet_id, sheet_name)
    return len(data)


# ---------------------------------------------------------------- publish ---
async def publish_and_get_url(session: aiohttp.ClientSession, token: str,
                              spreadsheet_id: str) -> str:
    """Same 'anyone with the link, reader' pattern as PixKeep's drive_utils.py
    — useful e.g. for the one-off export file created when a car is removed."""
    async with session.post(
        f"{DRIVE_API}/files/{spreadsheet_id}/permissions",
        json={"type": "anyone", "role": "reader"},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200, 201))
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def now_iso() -> str:
    """Timestamp for the 'Дата и время' / 'Дата' columns — local convention
    used across TwoPockets sheets."""
    return datetime.now(timezone.utc).isoformat()


def to_float(value) -> float:
    """Sheets values.get can return a cell as either a native JSON number
    or a string depending on how it was entered/formatted — handle both.
    Shared low-level helper; both sheets_transactions.py and cars.py need it."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    cleaned = "".join(ch for ch in str(value).replace(",", ".") if ch.isdigit() or ch in ".-")
    return float(cleaned) if cleaned else 0.0
