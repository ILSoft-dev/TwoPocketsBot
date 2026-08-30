"""
Парсинг текстового ввода вида "кофе 150р", "зарплата 100000₽",
"2 мороженых по 4 рубля" (количество × цена, только с явным маркером "по").

Фиксы:
- устойчивое распознавание рублей + копеек в голосовом вводе;
- поддержка "3 рубля 15", если Whisper потерял слово "копеек";
- поддержка "3,15 рубля"/"3.15 рубля";
- после извлечения суммы в комментарии не остаётся хвост "15 копеек";
- единица без пробела перед числом ("2кг", "5л") — QTY_PREFIX_PATTERN/
  QTY_SUFFIX_PATTERN раньше требовали пробел, живой ввод его часто не
  ставит;
- остальные правила исходного парсера сохранены.
"""
import re
from config import INCOME_KEYWORDS, CURRENCY_SYMBOLS

AMOUNT_PATTERN = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>руб\w*\.?|р\.?|₽|usd|\$|eur|€|byn|br)"
    r"|(?P<currency2>\$|€)\s*(?P<amount2>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)

KOPECK_WORD = r"коп\w*\.?"

# Нормальная голосовая форма:
# "3 рубля 15 копеек", "3 руб 15 коп", "3 р. 15 копеек"
KOPECK_COMPOUND_PATTERN = re.compile(
    rf"(?P<rub>\d+(?:[.,]\d+)?)\s*(?P<currency>руб\w*\.?|р\.?|₽|byn|br)\s*[,.;:—–-]?\s+"
    rf"(?P<kop>\d+)\s*{KOPECK_WORD}\s*[.,;:!?]?" ,
    re.IGNORECASE,
)

# Whisper иногда теряет последнее слово:
# "3 рубля 15"
# Разрешаем только когда "коп" отсутствует и после числа нет единицы/товара.
KOPECK_BARE_AFTER_RUBLE_PATTERN = re.compile(
    r"(?P<rub>\d+(?:[.,]\d+)?)\s*(?P<currency>руб\w*\.?|р\.?|₽|byn|br)"
    r"\s*[,.;:—–-]?\s*(?P<kop>\d{1,2})(?=\s*$)",
    re.IGNORECASE,
)

KOPECK_ONLY_PATTERN = re.compile(
    rf"(?P<kop>\d+)\s*{KOPECK_WORD}\s*[.,;:!?]?" , re.IGNORECASE
)

QUANTITY_PATTERN = re.compile(
    r"(?P<qty>\d+)\s*(?:шт\.?|штук\w*)?\s*"
    r"(?P<item>.{0,30}?)\bпо\s+"
    r"(?P<unit_price>\d+(?:[.,]\d+)?)\s*"
    r"(?P<currency>руб\w*\.?|р\.?|₽|usd|\$|eur|€|byn|br)",
    re.IGNORECASE | re.DOTALL,
)


def _clean_remainder(text: str, start: int, end: int) -> str:
    remainder = (text[:start] + " " + text[end:]).strip()
    return re.sub(r"\s+", " ", remainder).strip()


def _currency_code(raw_currency: str) -> str:
    raw = raw_currency.lower().replace(".", "")
    return CURRENCY_SYMBOLS.get(raw, "RUB")


def parse_amount(text: str):
    """
    Возвращает (amount: float, currency_code: str, remainder_text: str) или None.
    Валюта должна быть указана явно.
    """
    text = re.sub(r"\s+", " ", text).strip()

    # 1. Рубли + копейки.
    match = KOPECK_COMPOUND_PATTERN.search(text)
    if match:
        rub = float(match.group("rub").replace(",", "."))
        kop = int(match.group("kop"))
        if 0 <= kop <= 99:
            return (
                rub + kop / 100,
                _currency_code(match.group("currency")),
                _clean_remainder(text, match.start(), match.end()),
            )

    # 2. Whisper мог потерять слово "копеек":
    #    "бензин 3 рубля 15"
    match = KOPECK_BARE_AFTER_RUBLE_PATTERN.search(text)
    if match:
        rub = float(match.group("rub").replace(",", "."))
        kop = int(match.group("kop"))
        if 0 <= kop <= 99:
            return (
                rub + kop / 100,
                _currency_code(match.group("currency")),
                _clean_remainder(text, match.start(), match.end()),
            )

    # 3. Количество × цена — только с явным "по".
    qty_match = QUANTITY_PATTERN.search(text)
    if qty_match:
        qty = int(qty_match.group("qty"))
        unit_price = float(qty_match.group("unit_price").replace(",", "."))
        amount = qty * unit_price
        item_text = qty_match.group("item").strip()
        remainder = (
            text[:qty_match.start()] + " " + item_text + " " + text[qty_match.end():]
        ).strip()
        remainder = re.sub(r"\s+", " ", remainder).strip()
        return amount, _currency_code(qty_match.group("currency")), remainder

    # 4. Обычная сумма.
    match = AMOUNT_PATTERN.search(text)
    if match:
        if match.group("amount"):
            raw_amount = match.group("amount")
            raw_currency = match.group("currency")
        else:
            raw_amount = match.group("amount2")
            raw_currency = match.group("currency2")

        amount = float(raw_amount.replace(",", "."))
        return (
            amount,
            _currency_code(raw_currency),
            _clean_remainder(text, match.start(), match.end()),
        )

    # 5. Только копейки.
    kop_only = KOPECK_ONLY_PATTERN.search(text)
    if kop_only:
        amount = int(kop_only.group("kop")) / 100
        return amount, "RUB", _clean_remainder(text, kop_only.start(), kop_only.end())

    return None


def guess_type(remainder_text: str) -> str:
    """'income' если встретилось ключевое слово дохода, иначе 'expense'."""
    lowered = remainder_text.lower()
    for kw in INCOME_KEYWORDS:
        if kw in lowered:
            return "income"
    return "expense"


# --------------------------------------------------------- quantity/unit ----

NUMERAL_WORDS = {
    "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20,
}
_NUMERAL_ALT = "|".join(sorted(NUMERAL_WORDS, key=len, reverse=True))

_UNIT_STEMS = [
    ("килограмм", "кг"), ("кг", "кг"),
    ("миллилитр", "мл"), ("мл", "мл"),
    ("грамм", "г"), ("г", "г"),
    ("литр", "л"), ("л", "л"),
    ("штук", "шт"), ("шт", "шт"),
    ("упаковк", "уп"), ("уп", "уп"),
]
# "г"/"л" без точки ("5г", "5л") изначально требовали точку — боялись
# ложных срабатываний на случайных словах на "г"/"л". Но эта единица стоит
# ВПРИТЫК к цифре (см. QTY_PREFIX/QTY_SUFFIX_PATTERN), так что риск
# минимален, а "2кг"/"5л" без пробела — обычное дело при быстром наборе.
_UNIT_ALT = (
    r"килограмм\w*|кг\.?|"
    r"миллилитр\w*|мл\.?|"
    r"грамм\w*|г\.?|"
    r"литр\w*|л\.?|"
    r"штук\w*|шт\.?|"
    r"упаковк\w*|уп\.?"
)

QTY_PREFIX_PATTERN = re.compile(
    rf"^(?P<qty>\d+(?:[.,]\d+)?|{_NUMERAL_ALT})"
    rf"(?:\s*(?P<unit>{_UNIT_ALT}))?"
    r"\s+(?P<item>\S.*)$",
    re.IGNORECASE,
)

BARE_UNIT_PATTERN = re.compile(
    rf"^(?P<unit>{_UNIT_ALT})\s+(?P<item>\S.*)$",
    re.IGNORECASE,
)

# Единица без пробела перед ней ("2кг", "5л") — обычное дело при быстром
# наборе текста, не только "2 кг" с пробелом.
QTY_SUFFIX_PATTERN = re.compile(
    rf"^(?P<item>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?|{_NUMERAL_ALT})\s*(?P<unit>{_UNIT_ALT})$",
    re.IGNORECASE,
)


def _normalize_unit(raw: str) -> str:
    lowered = raw.lower()
    for stem, code in _UNIT_STEMS:
        if lowered.startswith(stem):
            return code
    return lowered


def _resolve_qty(raw_qty: str) -> float | None:
    if raw_qty[0].isdigit():
        return float(raw_qty.replace(",", "."))
    return NUMERAL_WORDS.get(raw_qty.lower())


def extract_quantity(remainder_text: str) -> tuple[float | None, str | None, str]:
    """Возвращает (количество, единица, текст_товара)."""
    text = remainder_text.strip()
    if not text:
        return None, None, remainder_text

    match = QTY_PREFIX_PATTERN.match(text)
    if match:
        item = match.group("item").strip()
        qty = _resolve_qty(match.group("qty")) if item else None
        if qty is not None:
            raw_unit = match.group("unit")
            unit = _normalize_unit(raw_unit) if raw_unit else None
            return qty, unit, item

    match = BARE_UNIT_PATTERN.match(text)
    if match:
        item = match.group("item").strip()
        if item:
            return 1.0, _normalize_unit(match.group("unit")), item

    match = QTY_SUFFIX_PATTERN.match(text)
    if match:
        item = match.group("item").strip()
        qty = _resolve_qty(match.group("qty")) if item else None
        if qty is not None:
            return qty, _normalize_unit(match.group("unit")), item

    return None, None, remainder_text
