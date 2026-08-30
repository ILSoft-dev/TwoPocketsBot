from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import supabase_client as db
from states import SettingsStates
from keyboards import settings_menu_keyboard, currency_keyboard

router = Router()


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    await message.answer(
        f"⚙️ <b>Настройки</b>\n"
        f"Валюта: {user['currency']}\n"
        f"Начало периода: {user['month_start']} число",
        reply_markup=settings_menu_keyboard(),
    )


@router.callback_query(F.data == "settings:currency")
async def settings_currency(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_currency)
    await callback.message.answer("Выбери валюту:", reply_markup=currency_keyboard())
    await callback.answer()


@router.callback_query(SettingsStates.waiting_currency, F.data.startswith("currency:"))
async def settings_currency_set(callback: CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    currency = callback.data.split(":")[1]
    db.update_user(user["id"], currency=currency)
    await state.clear()
    await callback.message.answer(f"✅ Валюта изменена на {currency}.")
    await callback.answer()


@router.callback_query(F.data == "settings:period")
async def settings_period(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_month_start)
    await callback.message.answer("Введи новый день начала отчётного периода (1-28):")
    await callback.answer()


@router.message(SettingsStates.waiting_month_start)
async def settings_period_set(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 28):
        await message.answer("Нужно число от 1 до 28.")
        return
    user = db.get_user(message.from_user.id)
    db.update_user(user["id"], month_start=int(text))
    await state.clear()
    await message.answer(f"✅ Начало периода: {text} число.")
