"""
today.py
/today — записи за сегодняшний календарный день.

"Сегодня" — по UTC, тот же пояс, что использует весь остальной бот (своего
часового пояса пользователя бот не хранит, см. period_utils.py и прочие
даты в проекте). Переиспользует форматирование из history.py
(_clean_label/_category_suffix/_format_money) — тот же вид записи
("-150.00 RUB — кофе (Продукты)"), чтобы /today и /history не расходились
в оформлении.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx
from history import _clean_label, _category_suffix, _format_money

router = Router()


@router.message(Command("today"))
async def cmd_today(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        rows = await tx.get_transactions_since(user["id"], since)
    except tx.NoGoogleAccount:
        await message.answer(
            "Google Drive не подключён — пройди заново /start, чтобы подключить."
        )
        return
    except Exception:
        logging.exception("today: unexpected error fetching from Sheets")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return

    currency = user.get("currency", "RUB")
    if not rows:
        await message.answer("Сегодня записей пока нет.")
        return

    # rows уже отсортированы новыми-сверху (tx_logic.filter_active_in_range) —
    # та же свежее-сверху лента, что и в /history, не пересортировываем.
    lines = ["📅 <b>Сегодня</b>\n"]
    day_expense = Decimal("0")
    day_income = Decimal("0")

    for row in rows:
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

    lines.append(f"\n<i>Расход: {day_expense:.2f} {currency}, Доход: {day_income:.2f} {currency}</i>")

    await message.answer("\n".join(lines))
