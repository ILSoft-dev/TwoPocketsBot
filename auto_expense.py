"""
auto_expense.py
v1.0 - structured field extraction for category="Авто" expenses.

Deterministic keyword heuristics, no extra Groq call — same philosophy as
category_map/lookup_keyword_category: cheap and free before reaching for an
LLM. Car-name resolution lives in cars.match_car_name(), not here.
"""
import re

FUEL_KEYWORDS = ["заправ", "бензин", "топлив", "дизель", "газ"]
MAINTENANCE_KEYWORDS = [" то ", "техосмотр", "диагностик", "развал", "схожден", "шиномонтаж"]
REPAIR_KEYWORDS = [
    "ремонт", "замен", "колодк", "масл", "фильтр", "шин", "резин", "аккумулятор",
    "свеч", "тормоз", "подвеск", "сцеплен", "ремен", "радиатор", "антифриз",
]

TYPE_FUEL = "Заправка"
TYPE_MAINTENANCE = "ТО"
TYPE_REPAIR = "Ремонт"
TYPE_OTHER = "Прочее"


def classify_auto_type(text: str) -> str:
    lowered = f" {text.lower()} "
    if any(kw in lowered for kw in FUEL_KEYWORDS):
        return TYPE_FUEL
    if any(kw in lowered for kw in MAINTENANCE_KEYWORDS):
        return TYPE_MAINTENANCE
    if any(kw in lowered for kw in REPAIR_KEYWORDS):
        return TYPE_REPAIR
    return TYPE_OTHER


def extract_mileage(text: str) -> float | None:
    """Only matches a number immediately followed by 'км' — deliberately
    narrow, so it doesn't accidentally grab the money amount or some other
    unrelated number sitting in the same message."""
    match = re.search(r"(\d[\d\s.,]*)\s*км", text, re.IGNORECASE)
    if not match:
        return None
    digits = "".join(ch for ch in match.group(1) if ch.isdigit())
    return float(digits) if digits else None
