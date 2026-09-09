"""
auto_expense.py
v1.1 - structured field extraction for auto-related expenses (category
"Топливо"/"Ремонт/ТО"/"Запчасти"/"Прочее" — см. changelog).

Deterministic keyword heuristics, no extra Groq call — same philosophy as
category_map/lookup_keyword_category: cheap and free before reaching for an
LLM. Car-name resolution lives in cars.match_car_name(), not here.

Changelog:
- v1.1: Раньше было 4 типа (Заправка/ТО/Ремонт/Прочее), которые лишь
        помечали строку в служебном листе "Авто" — сама категория в
        основном листе "Транзакции" всегда была жёстко "Авто", независимо
        от типа. Теперь тип классификатора И ЕСТЬ финальная категория:
        input_handler.py больше не пишет фиксированную "Авто" — категорию
        определяет classify_auto_type().

        Разделение по ПРИНЦИПУ "названо действие или названа вещь":
        - Ремонт/ТО — есть слово-действие ("ремонт", "замена", "то",
          "диагностика" и т.п.) — платишь за работу/услугу, даже если
          рядом упомянута конкретная деталь ("замена рычагов" — это
          Ремонт/ТО, не Запчасти, действие перевешивает).
        - Запчасти — названа деталь или техническая жидкость (рычаг,
          наконечник, антифриз, масло...) БЕЗ слова-действия рядом —
          значит, купил вещь, а не платил за работу.
        - Прочее — специально БЕЗ явных ключевых слов, естественный
          фоллбэк: если сообщение про машину, но не топливо и не
          подошло ни под действие, ни под деталь — освежители,
          чехлы, моющие средства, омывайка и т.п. (проверено: ни одно
          не пересекается с ключевыми словами других категорий).

        Порядок проверки важен: Топливо → Ремонт/ТО (действие) →
        Запчасти (вещь) → Прочее (фоллбэк). Действие проверяется РАНЬШЕ
        детали именно для случая "замена рычагов".
"""
import re

FUEL_KEYWORDS = ["заправ", "бензин", "топлив", "дизель", "газ"]

# Слово-ДЕЙСТВИЕ — платишь за работу/услугу, даже если рядом названа
# конкретная деталь (см. docstring выше). " то " с пробелами по краям —
# иначе "то" как подстрока ловит половину русских слов ("иногда", "нету"),
# строка в classify_auto_type ниже дополнена пробелами специально для этого.
ACTION_KEYWORDS = [
    "ремонт", "замен", " то ", "техосмотр", "диагностик",
    "развал", "схожден", "шиномонтаж",
]

# Названа ВЕЩЬ (деталь или техническая жидкость) без слова-действия рядом —
# значит, купил вещь, не платил за работу. " шин" с пробелом перед словом —
# иначе ловит "шин" как подстроку слова "маШИНа/маШИНу" в любом падеже, и
# ЛЮБОЕ сообщение со словом "машина" попало бы в "Запчасти".
PARTS_KEYWORDS = [
    "колодк", "масл", "фильтр", " шин", "резин", "аккумулятор",
    "свеч", "тормоз", "подвеск", "сцеплен", "ремен", "радиатор", "антифриз",
    "рычаг", "наконечник", "шаров", "втулк", "стойк",
]

TYPE_FUEL = "Топливо"
TYPE_REPAIR = "Ремонт/ТО"
TYPE_PARTS = "Запчасти"
TYPE_OTHER = "Прочее"


def classify_auto_type(text: str) -> str:
    lowered = f" {text.lower()} "
    if any(kw in lowered for kw in FUEL_KEYWORDS):
        return TYPE_FUEL
    if any(kw in lowered for kw in ACTION_KEYWORDS):
        return TYPE_REPAIR
    if any(kw in lowered for kw in PARTS_KEYWORDS):
        return TYPE_PARTS
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
