"""
fluid_tracker.py
v1.1 - technical fluid replacement tracking (oil, antifreeze, etc.)

Changelog:
- v1.1: check_due_fluids now fetches the Авто sheet ONCE and checks all
        fluid types against that single result, instead of doing 5
        separate Sheets API calls (one per fluid type) for data that
        never changes between them. Also now goes through google_api.call()
        so an expired token gets refreshed instead of failing the check.

No engine-specific numbers hardcoded (the family has two different cars) —
generic, commonly-cited intervals per fluid TYPE, easy to adjust below if
they don't fit a specific car. "Which fluid was this repair about" is
detected by scanning the free-text Описание of Ремонт-type Авто rows for
keywords — no extra column/schema needed, reuses what's already stored.
"""
import aiohttp

import sheets_client as sc
import google_api

FLUID_INTERVALS_KM = {
    "Масло": 10_000,
    "Антифриз": 60_000,
    "Тормозная жидкость": 40_000,
    "Фильтр воздуха": 15_000,
    "Свечи": 30_000,
}

FLUID_KEYWORDS = {
    # Одно слово — минимальная основа без окончания, чтобы ловить все
    # падежи ("масло"/"масла"/"маслом"/"масле" и т.д. — просто "масл").
    # Составной термин — кортеж из основ через AND (оба должны встретиться,
    # порядок и падеж не важны): "тормозную жидкость" и "тормозной жидкости"
    # ловятся одинаково через ("тормозн", "жидкост").
    "Масло": ["масл"],
    "Антифриз": ["антифриз", "охлажда"],
    "Тормозная жидкость": [("тормозн", "жидкост")],
    "Фильтр воздуха": [("фильтр", "возду")],
    "Свечи": ["свеч"],
}

# Предупреждаем, когда осталось меньше этой доли интервала (0.1 = последние 10%)
WARN_FRACTION = 0.1


def detect_fluid_type(text: str) -> str | None:
    lowered = text.lower()
    for fluid, keywords in FLUID_KEYWORDS.items():
        for kw in keywords:
            if isinstance(kw, tuple):
                if all(part in lowered for part in kw):
                    return fluid
            elif kw in lowered:
                return fluid
    return None


async def check_due_fluids(account: dict, car_name: str, current_mileage: float) -> list[dict]:
    """Fluids at/past the warning threshold. Fluids with NO logged
    replacement at all are silently skipped — we have no baseline to
    measure from, so flagging them would just be noise, not signal.
    Deliberately does NOT require Тип=='Ремонт' on rows — auto_expense's
    type heuristic and this module's fluid-keyword detection are
    independent classifiers that can disagree (e.g. "заправил антифриза"
    trips the Заправка keyword in auto_expense but is clearly an antifreeze
    replacement here); matching on the fluid keyword alone is more robust
    than requiring both heuristics to agree."""
    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _do(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_AUTO)
        rows = await google_api.call(box, _do)

    car_rows = [r for r in rows if r["Машина"] == car_name]

    due = []
    for fluid, interval in FLUID_INTERVALS_KM.items():
        candidates = [
            sc.to_float(r["Пробег"]) for r in car_rows
            if r["Пробег"] and detect_fluid_type(str(r["Описание"])) == fluid
        ]
        if not candidates:
            continue
        last_at = max(candidates)
        due_at = last_at + interval
        remaining = due_at - current_mileage
        if remaining <= interval * WARN_FRACTION:
            due.append({
                "fluid": fluid, "last_at": last_at, "due_at": due_at, "remaining_km": remaining,
            })
    return due


def format_due_warning(due_list: list[dict], car_name: str) -> str:
    lines = [f"⚠️ «{car_name}»: пора проверить —"]
    for item in due_list:
        if item["remaining_km"] <= 0:
            lines.append(f"• {item['fluid']}: пробег уже {-item['remaining_km']:g} км сверх нормы")
        else:
            lines.append(f"• {item['fluid']}: осталось ~{item['remaining_km']:g} км")
    return "\n".join(lines)
