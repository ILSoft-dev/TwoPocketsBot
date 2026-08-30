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
from parser import parse_amount


def check(text, expected_amount, expected_remainder):
    result = parse_amount(text)
    assert result is not None, text
    amount, currency, remainder = result
    assert abs(amount - expected_amount) < 1e-9, (text, result)
    assert remainder == expected_remainder, (text, result)


check("бензин 3 рубля 15 копеек", 3.15, "бензин")
check("бензин 3 руб 15 коп", 3.15, "бензин")
check("бензин 3 р. 15 копеек", 3.15, "бензин")
check("бензин 3 рубля 15", 3.15, "бензин")
check("бензин 3,15 рубля", 3.15, "бензин")
check("бензин 3.15 рубля", 3.15, "бензин")
check("конфеты 43 копейки", 0.43, "конфеты")
check("2 мороженых по 4 рубля", 8.0, "мороженых")
check("кофе 150р", 150.0, "кофе")

print("All parser tests passed.")
