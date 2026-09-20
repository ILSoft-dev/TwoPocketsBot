import sys
from pathlib import Path
import types

# Минимальный stub config, чтобы протестировать parser без установки всего бота.
config = types.ModuleType("config")
config.INCOME_KEYWORDS = ["зарплат", "доход"]
config.CURRENCY_SYMBOLS = {
    "руб": "RUB", "рублей": "RUB", "рубля": "RUB", "рублях": "RUB",
    "р": "RUB", "₽": "RUB", "byn": "BYN", "br": "BYN",
    "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
}
sys.modules["config"] = config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import parse_amount, extract_quantity


def check(text, expected_amount, expected_remainder, expected_currency=None):
    result = parse_amount(text)
    assert result is not None, text
    amount, currency, remainder = result
    assert abs(amount - expected_amount) < 1e-9, (text, result)
    assert remainder == expected_remainder, (text, result)
    if expected_currency is not None:
        assert currency == expected_currency, (text, result)


def check_none(text):
    assert parse_amount(text) is None, text


def check_quantity(text, expected_qty, expected_unit, expected_item):
    qty, unit, item = extract_quantity(text)
    assert qty == expected_qty and unit == expected_unit and item == expected_item, \
        (text, (qty, unit, item))


check("бензин 3 рубля 15 копеек", 3.15, "бензин")
check("бензин 3 руб 15 коп", 3.15, "бензин")
check("бензин 3 р. 15 копеек", 3.15, "бензин")
check("бензин 3 рубля 15", 3.15, "бензин")
check("бензин 3,15 рубля", 3.15, "бензин")
check("бензин 3.15 рубля", 3.15, "бензин")
check("конфеты 43 копейки", 0.43, "конфеты")
check("2 мороженых по 4 рубля", 8.0, "мороженых")
check("кофе 150р", 150.0, "кофе")

# Валюты и написания, не покрытые примерами выше (см. ТЗ 1.7).
check("такси 150 ₽", 150.0, "такси", "RUB")  # пробел перед символом
check("обед $12.5", 12.5, "обед", "USD")  # символ ПЕРЕД суммой, доллар
check("кофе 12€", 12.0, "кофе", "EUR")  # символ ПОСЛЕ суммы, евро
check("проезд 10 byn", 10.0, "проезд", "BYN")  # белорусские рубли словом
check("бензин 8 рублей 43", 8.43, "бензин")  # Whisper потерял слово "копеек"

# Отсутствие валюты — честный отказ, а не угадывание суммы/количества.
check_none("")
check_none("сахар 5")  # "5" без валюты рядом — не сумма, не число (могло бы быть кг)

# Количество/единица (parser.extract_quantity) — отдельный шаг ПОСЛЕ
# parse_amount, применяется к остатку текста (см. input_handler.py). Тестируем
# оба вместе, как в реальном потоке: сначала отрезаем сумму, потом разбираем
# остаток на количество/единицу/товар.
amount, currency, remainder = parse_amount("5л масла 45р")
assert abs(amount - 45.0) < 1e-9 and currency == "RUB" and remainder == "5л масла", (amount, currency, remainder)
check_quantity(remainder, 5.0, "л", "масла")

amount, currency, remainder = parse_amount("два мороженых 50р")
assert abs(amount - 50.0) < 1e-9 and currency == "RUB" and remainder == "два мороженых", (amount, currency, remainder)
check_quantity(remainder, 2.0, None, "мороженых")  # словом "два", не цифрой

print("All parser tests passed.")
