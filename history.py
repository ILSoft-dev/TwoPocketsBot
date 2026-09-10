"""
history.py
Исправленная версия /history.

Показывает название конкретной покупки из поля "Комментарий", а не
категорию. Категория используется только для старых/безымянных записей
(например, фото чека, где комментария нет).

Суммы всегда отображаются с двумя знаками после десятичного разделителя.

Changelog:
- категория в скобках рядом с описанием покупки ("Хлеб (Продукты)") —
  чтобы сразу видеть, что трата легла в верную категорию, не открывая
  таблицу. Особенно полезно для авто-трат (Топливо/Ремонт-ТО/Запчасти/
  Прочее — см. auto_expense.py) — ошибка классификации там не всегда
  заметна на глаз, а тут видна сразу. Не дублируется, если категория и так
  уже показана как название покупки (типичный случай для старых записей
  без комментария, где label — это и есть fallback на категорию).
- под каждым днём — итоговая строка "Расход: X, Доход: Y" за этот день;
  если за день не было дохода (или расхода) — так и пишем "0", а не
  пропускаем строку молча, чтобы сразу было видно полную картину дня.
"""
import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx

router = Router()

DEFAULT_DAYS = 7


def _format_money(value) -> str:
    """Безопасно форматирует сумму для истории: 3 -> 3.00, 3.5 -> 3.50."""
    try:
        amount = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    return f"{amount:.2f}"


_LEADING_PURCHASE_VERB_RE = re.compile(
    r"^\s*(?:я\s+)?"
    r"(?:купил(?:а|и)?|покупал(?:а|и)?|взял(?:а|и)?|"
    r"оплатил(?:а|и)?|заплатил(?:а|и)?|потратил(?:а|и)?)"
    r"\s+(?:на\s+)?",
    re.IGNORECASE,
)


def _clean_label(row: dict) -> str:
    """
    Приоритет:
      1. Комментарий — фактическое название покупки;
      2. Категория — fallback для старых/безымянных записей;
      3. 'Без описания' — крайний случай.

    Для голосового ввода убираем только служебное начало вроде
    «купил», «взял», «оплатил», чтобы история показывала предмет покупки:
    «бензин», а не «купил бензин».
    """
    comment = str(row.get("Комментарий", "") or "").strip()
    if comment:
        comment = " ".join(comment.split())
        comment = _LEADING_PURCHASE_VERB_RE.sub("", comment).strip()
        return comment or "Без описания"

    category = str(row.get("Категория", "") or "").strip()
    return category or "Без описания"


def _category_suffix(row: dict, label: str) -> str:
    """Категория в скобках рядом с описанием — см. changelog выше. Пусто,
    если категории нет, или если label и так уже равен категории (чтобы
    не получить "Продукты (Продукты)" на старых записях без комментария)."""
    category = str(row.get("Категория", "") or "").strip()
    if not category or category.lower() == label.strip().lower():
        return ""
    return f" ({category})"


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    days = DEFAULT_DAYS
    if command.args and command.args.strip().isdigit():
        days = int(command.args.strip())

    try:
        rows = await tx.get_history(user["id"], days)
    except tx.NoGoogleAccount:
        await message.answer(
            "Google Drive не подключён — пройди заново /start, чтобы подключить."
        )
        return
    except Exception:
        logging.exception("history: unexpected error fetching from Sheets")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return

    currency = user.get("currency", "RUB")

    if not rows:
        await message.answer(f"За последние {days} дн. записей нет.")
        return

    lines = [f"🧾 <b>История за {days} дн.</b>\n"]
    last_day = None
    day_expense = Decimal("0")
    day_income = Decimal("0")

    def flush_day_total():
        if last_day is not None:
            lines.append(
                f"<i>Расход: {day_expense:.2f} {currency}, "
                f"Доход: {day_income:.2f} {currency}</i>"
            )

    for row in rows:
        dt = str(row.get("Дата и время", ""))[:10]
        if dt != last_day:
            flush_day_total()
            lines.append(f"\n<b>{dt}</b>")
            last_day = dt
            day_expense = Decimal("0")
            day_income = Decimal("0")

        is_income = row.get("Тип") == "income"
        sign = "+" if is_income else "-"
        label = _clean_label(row)
        amount_str = _format_money(row.get("Сумма", ""))
        try:
            amount_dec = Decimal(amount_str)
        except InvalidOperation:
            amount_dec = Decimal("0")
        if is_income:
            day_income += amount_dec
        else:
            day_expense += amount_dec

        lines.append(f"{sign}{amount_str} {currency} — {label}{_category_suffix(row, label)}")

    flush_day_total()  # итог последнего дня — цикл его не закрывает

    await message.answer("\n".join(lines))
