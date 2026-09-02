"""
cars.py
v1.4 - car registry + mileage logging, backed by Google Sheets (sheets_client.py)

Changelog:
- v1.4: get_last_auto_event() возвращает (событие, count) вместо просто
        события — insights._answer_last_date теперь может сказать "и всего
        так было N раз", а не только дату последнего. BREAKING: вызывающий
        код должен распаковывать кортеж (обновлён insights.py).
- v1.3: get_last_auto_event() теперь распознаёт синонимы жидкостей через
        fluid_tracker.detect_fluid_type — "когда менял антифриз" находит и
        запись, где в Описании написано "охлаждающая жидкость" (и вообще
        любой синоним из fluid_tracker.FLUID_KEYWORDS для этого типа), не
        только точную/однокоренную подстроку самого слова из вопроса.
- v1.2: get_last_auto_event() — latest Авто-sheet row for a car matching a
        free-text keyword, backs insights.py's intent=last_date answers
        ("когда менял масло на опеле?").
- v1.1: functions now take the full `account` dict (needs "id" and
        "google_refresh_token", not just access_token+spreadsheet_id) so
        every Sheets API call can go through google_api.call() and refresh
        an expired access token instead of failing outright.

Cars live in the "Машины" tab (registry: name + status) and mileage points
in the "Пробег" tab. Both belong to whichever spreadsheet is the *effective*
one for a given user at call time (their own, or the family owner's) —
callers pass in the account dict already resolved via
supabase_client.get_effective_google_account().
"""
import re

import aiohttp

import sheets_client as sc
import fluid_tracker
import google_api

STATUS_ACTIVE = "Активна"
STATUS_ARCHIVED = "Архив"


async def add_car(account: dict, name: str, who: str = "",
                  starting_mileage: float | None = None) -> str:
    """Register a new car. If a starting mileage is given, also logs it as
    the first point in the mileage history (source: manual input) so
    average-km/month math has a real anchor from day one."""
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do_car(token):
            return await sc.append_row(
                session, token, account["google_spreadsheet_id"], sc.SHEET_CARS,
                [name, STATUS_ACTIVE, sc.now_iso(), ""],
            )
        car_id = await google_api.call(box, _do_car)

        if starting_mileage is not None:
            async def _do_mileage(token):
                return await sc.append_row(
                    session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE,
                    [sc.now_iso(), name, starting_mileage, "Ручной ввод", who],
                )
            await google_api.call(box, _do_mileage)

    return car_id


async def list_active_cars(account: dict) -> list[dict]:
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_CARS)
        rows = await google_api.call(box, _do)
    return [r for r in rows if r["Статус"] == STATUS_ACTIVE]


def match_car_name(text: str, active_cars: list[dict]) -> str | None:
    """Substring match against registered car names (case-insensitive) —
    same cheap deterministic approach category_map already uses for
    keyword->category lookup, no LLM call needed for this."""
    lowered = text.lower()
    for car in active_cars:
        if car["Машина"].lower() in lowered:
            return car["Машина"]
    return None


async def get_mileage_distance(account: dict, car_name: str,
                               since, until) -> float | None:
    """Пройденное расстояние в диапазоне [since, until] (любая из границ
    может быть None — открытая). Берёт последнюю точку ДО since как базу
    (если её нет — первую точку внутри диапазона), последнюю точку внутри
    диапазона — как конец. None, если внутри диапазона вообще нет точек."""
    from datetime import timezone as _tz

    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE)
        rows = await google_api.call(box, _do)

    def _parse(value):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)

    since_aware = since if (since is None or since.tzinfo) else since.replace(tzinfo=_tz.utc)
    until_aware = until if (until is None or until.tzinfo) else until.replace(tzinfo=_tz.utc)

    points = []
    for r in rows:
        if r["Машина"] != car_name:
            continue
        dt = _parse(r["Дата"])
        if dt is None:
            continue
        points.append((dt, sc.to_float(r["Пробег"])))
    if not points:
        return None
    points.sort(key=lambda p: p[0])

    in_range = [p for p in points if (since_aware is None or p[0] >= since_aware)
               and (until_aware is None or p[0] <= until_aware)]
    if not in_range:
        return None

    before = [p for p in points if since_aware is not None and p[0] < since_aware]
    baseline = before[-1][1] if before else in_range[0][1]
    latest = in_range[-1][1]
    return latest - baseline


def _keyword_matches(keyword: str, text: str) -> bool:
    """Подстрочное совпадение с запасом на падежные окончания — тот же
    принцип, что insights._item_matches и fluid_tracker.detect_fluid_type:
    берём основу слова (~70% длины, минимум 3 символа), чтобы "масло" из
    вопроса нашло "масла" или "маслом" в свободном тексте Описания."""
    kw = keyword.lower().strip()
    lowered = text.lower()
    if kw in lowered:
        return True
    stem_len = max(3, int(len(kw) * 0.7))
    return kw[:stem_len] in lowered


async def get_last_auto_event(account: dict, car_name: str, keyword: str | None) -> tuple[dict | None, int]:
    """(самая свежая подходящая запись, СКОЛЬКО ВСЕГО подходящих записей).
    keyword None/пустой -> просто последняя запись по машине, любого типа.
    Используется для вопросов "когда" ("когда менял масло на опеле?") —
    insights.py вызывает это вместо car_period_stats, т.к. вопрос не
    ограничен текущим отчётным периодом.

    Если keyword — это распознаваемый тип техжидкости (fluid_tracker),
    матчим ПО ТИПУ жидкости, а не по буквальному слову: "антифриз" из
    вопроса найдёт запись с Описанием "охлаждающая жидкость" (и любой
    другой синоним из fluid_tracker.FLUID_KEYWORDS для того же типа), не
    только однокоренные формы самого "антифриз". Для всего остального
    (не жидкость — "бензин", "шиномонтаж" и т.п.) — обычное подстрочное
    совпадение с запасом на падежи."""
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_AUTO)
        rows = await google_api.call(box, _do)

    candidates = [r for r in rows if r["Машина"] == car_name and r["Статус"] == STATUS_ACTIVE]
    if keyword:
        fluid = fluid_tracker.detect_fluid_type(keyword)
        if fluid:
            candidates = [
                r for r in candidates
                if fluid_tracker.detect_fluid_type(str(r["Описание"])) == fluid
            ]
        else:
            candidates = [r for r in candidates if _keyword_matches(keyword, str(r["Описание"]))]
    if not candidates:
        return None, 0
    candidates.sort(key=lambda r: str(r["Дата"]), reverse=True)
    return candidates[0], len(candidates)


async def get_latest_mileage(account: dict, car_name: str) -> float | None:
    """Most recent mileage point for a car — used both for the reminder's
    'Без изменений' button (re-log the same value with today's date) and,
    later, for average-km/month math."""
    box = google_api.TokenBox(account)
    async with aiohttp.ClientSession() as session:
        async def _do(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE)
        rows = await google_api.call(box, _do)
    points = [r for r in rows if r["Машина"] == car_name]
    if not points:
        return None
    points.sort(key=lambda r: str(r["Дата"]), reverse=True)
    return sc.to_float(points[0]["Пробег"])


async def archive_and_export_car(account: dict, car_id: str) -> str | None:
    """Removes a car from active tracking (soft — Статус -> Архив, no data
    deleted from the main spreadsheet) and exports its full history (auto
    expenses + mileage points) as a standalone spreadsheet with a summary
    tab. Returns the export's shareable link, or None if the car wasn't
    found (e.g. already removed)."""
    box = google_api.TokenBox(account)
    spreadsheet_id = account["google_spreadsheet_id"]

    async with aiohttp.ClientSession() as session:
        async def _get_cars(token):
            return await sc.get_rows(session, token, spreadsheet_id, sc.SHEET_CARS)
        cars_rows = await google_api.call(box, _get_cars)

        car_row = next((c for c in cars_rows if c["ID"] == car_id), None)
        if not car_row:
            return None
        car_name = car_row["Машина"]

        async def _get_auto(token):
            return await sc.get_rows(session, token, spreadsheet_id, sc.SHEET_AUTO)
        auto_rows = [r for r in await google_api.call(box, _get_auto) if r["Машина"] == car_name]

        async def _get_mileage(token):
            return await sc.get_rows(session, token, spreadsheet_id, sc.SHEET_MILEAGE)
        mileage_rows = [r for r in await google_api.call(box, _get_mileage) if r["Машина"] == car_name]

        total_spent = sum(sc.to_float(r["Сумма"]) for r in auto_rows)
        by_type: dict[str, float] = {}
        for r in auto_rows:
            by_type[r["Тип"]] = by_type.get(r["Тип"], 0) + sc.to_float(r["Сумма"])
        mileages = [sc.to_float(r["Пробег"]) for r in mileage_rows if r["Пробег"]]

        summary_rows = [
            ["Показатель", "Значение"],
            ["Машина", car_name],
            ["Дата регистрации", car_row.get("Дата регистрации", "")],
            ["Всего потрачено", f"{total_spent:g}"],
        ]
        for t, amount in sorted(by_type.items()):
            summary_rows.append([f"  из них: {t}", f"{amount:g}"])
        if mileages:
            summary_rows.append(["Пробег: от", f"{min(mileages):g}"])
            summary_rows.append(["Пробег: до", f"{max(mileages):g}"])

        auto_headers = sc.HEADERS[sc.SHEET_AUTO]
        mileage_headers = sc.HEADERS[sc.SHEET_MILEAGE]
        auto_export = [auto_headers] + [[r[h] for h in auto_headers] for r in auto_rows]
        mileage_export = [mileage_headers] + [[r[h] for h in mileage_headers] for r in mileage_rows]

        async def _create_export(token):
            return await sc.create_plain_spreadsheet(
                session, token, f"Архив: {car_name}",
                {"Сводка": summary_rows, "Траты": auto_export, "Пробег": mileage_export},
            )
        export_id = await google_api.call(box, _create_export)

        async def _publish(token):
            return await sc.publish_and_get_url(session, token, export_id)
        link = await google_api.call(box, _publish)

        async def _archive(token):
            return await sc.update_cell(
                session, token, spreadsheet_id, sc.SHEET_CARS, car_id, "Статус", STATUS_ARCHIVED
            )
        await google_api.call(box, _archive)

    return link


def parse_mileage(text: str) -> float | None:
    """Pull a plain number out of free-form mileage input — accepts
    '305000', '305 000', '305.000', '305000 км', etc. Returns None if no
    digits at all (so the caller can ask again instead of saving garbage)."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return float(digits) if digits else None


def parse_mileage_message(text: str) -> tuple[str, float | None]:
    """For standalone 'пробег опель 305000 км' messages (no currency, so
    parser.parse_amount would reject them). Strips the 'пробег' keyword,
    digits, and 'км' unit, leaving whatever's left for car-name matching.
    Returns (leftover_text, mileage)."""
    mileage = parse_mileage(text)
    working = re.sub(r"(?i)пробег[а-я]*", "", text)
    working = re.sub(r"[\d\s.,]+", " ", working)
    working = re.sub(r"(?i)\bкм\b", "", working)
    return working.strip(), mileage
