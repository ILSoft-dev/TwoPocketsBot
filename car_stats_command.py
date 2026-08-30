"""
car_stats_command.py
v1.1 - /carstats: on-demand narrative statistics for a chosen car, current
reporting period (same month_start setting /report already uses).

Changelog:
- v1.1: wrapped every Sheets-touching call in try/except so a Google API
        failure (even after google_api.py's refresh-retry) gives the user
        a clear message instead of silently doing nothing.
"""
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import supabase_client as db
import cars
import car_stats
from report import period_start

router = Router()

SHEETS_ERROR_TEXT = (
    "Не получилось обратиться к Google Диску. Если повторится — "
    "переподключи через /start."
)


def _car_stats_keyboard(active_cars: list[dict]):
    builder = InlineKeyboardBuilder()
    for car in active_cars:
        builder.button(text=car["Машина"], callback_data=f"carstat_choice:{car['ID']}")
    builder.adjust(2)
    return builder.as_markup()


async def send_car_stats(message: Message, user: dict, account: dict, car_row: dict):
    since = period_start(user.get("month_start", 1))
    try:
        stats = await car_stats.car_period_stats(account, car_row["Машина"], since)
    except Exception:
        logging.exception("send_car_stats: unexpected error fetching stats")
        await message.answer(SHEETS_ERROR_TEXT)
        return
    currency = user.get("currency", "RUB")
    await message.answer(car_stats.format_stats_text(car_row["Машина"], stats, currency))


@router.message(Command("carstats"))
async def cmd_carstats(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    account = db.get_effective_google_account(user["id"])
    if not account:
        await message.answer("Google Drive не подключён — пройди заново /start.")
        return

    try:
        active = await cars.list_active_cars(account)
    except Exception:
        logging.exception("cmd_carstats: unexpected error listing cars")
        await message.answer(SHEETS_ERROR_TEXT)
        return

    if not active:
        await message.answer("Нет зарегистрированных машин. Добавь через /cars.")
        return

    if len(active) == 1:
        await send_car_stats(message, user, account, active[0])
        return

    await message.answer("По какой машине показать статистику?", reply_markup=_car_stats_keyboard(active))


@router.callback_query(F.data.startswith("carstat_choice:"))
async def carstat_choice_picked(callback: CallbackQuery):
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    account = db.get_effective_google_account(user["id"])
    if not account:
        await callback.message.answer("Google Drive не подключён.")
        await callback.answer()
        return

    try:
        active = await cars.list_active_cars(account)
    except Exception:
        logging.exception("carstat_choice_picked: unexpected error listing cars")
        await callback.message.answer(SHEETS_ERROR_TEXT)
        await callback.answer()
        return

    car_row = next((c for c in active if c["ID"] == car_id), None)
    if not car_row:
        await callback.message.edit_text("Не нашёл эту машину.")
        await callback.answer()
        return

    await send_car_stats(callback.message, user, account, car_row)
    await callback.answer()
