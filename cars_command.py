"""
cars_command.py
v1.0 - /cars: list active cars with delete buttons, add a new one.

Reuses cars.py (data layer) — this file is just the Telegram-facing menu,
kept separate from the onboarding car-registration flow in start.py (same
underlying cars.add_car(), different FSM states so the two flows don't
collide or need to know about each other).
"""
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import supabase_client as db
import cars
from states import CarManageStates
from keyboards import cars_menu_keyboard, car_delete_confirm_keyboard

router = Router()


def who_label(user) -> str:
    return user.username or user.first_name or str(user.id)


async def _active_cars_or_none(message: Message, user_id: int):
    account = db.get_effective_google_account(user_id)
    if not account:
        await message.answer("Сначала подключи Google Drive: /start")
        return None, None
    try:
        active = await cars.list_active_cars(account)
    except Exception:
        logging.exception("_active_cars_or_none: unexpected error listing cars")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return None, None
    return account, active


@router.message(Command("cars"))
async def cmd_cars(message: Message):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    account, active = await _active_cars_or_none(message, user["id"])
    if account is None:
        return

    if not active:
        await message.answer(
            "Пока нет зарегистрированных машин.",
            reply_markup=cars_menu_keyboard([]),
        )
        return

    await message.answer("Твои машины:", reply_markup=cars_menu_keyboard(active))


@router.callback_query(F.data == "car_manage_add")
async def car_manage_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CarManageStates.waiting_new_name)
    await callback.message.answer("Как назвать машину?")
    await callback.answer()


@router.message(CarManageStates.waiting_new_name)
async def car_manage_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название машины текстом.")
        return
    await state.update_data(pending_car_name=name)
    await state.set_state(CarManageStates.waiting_new_mileage)
    await message.answer(
        f"Какой сейчас пробег у «{name}»? Можно пропустить (просто напиши «-»)."
    )


@router.message(CarManageStates.waiting_new_mileage)
async def car_manage_mileage_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["pending_car_name"]
    text = message.text.strip()

    mileage = None
    if text != "-":
        mileage = cars.parse_mileage(text)
        if mileage is None:
            await message.answer("Не вижу числа — введи пробег цифрами или «-», чтобы пропустить.")
            return

    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)
    account = db.get_effective_google_account(user["id"])
    if account:
        try:
            await cars.add_car(account, name, who=who, starting_mileage=mileage)
        except Exception:
            logging.exception("car_manage_mileage_entered: unexpected error adding car")
            await message.answer(
                "Не получилось сохранить машину в Google Диск. Если "
                "повторится — переподключи через /start."
            )
            await state.clear()
            return
        await message.answer(f"«{name}» добавлена ✅")
    else:
        await message.answer("Google Drive не подключён — пройди заново /start.")

    await state.clear()


@router.callback_query(F.data.startswith("car_remove:"))
async def car_remove_ask(callback: CallbackQuery):
    car_id = callback.data.split(":", 1)[1]
    await callback.message.answer(
        "Удалить машину? История трат и пробега будет выгружена в отдельный "
        "файл, машина перестанет отслеживаться (напоминания, статистика).",
        reply_markup=car_delete_confirm_keyboard(car_id),
    )
    await callback.answer()


@router.callback_query(F.data == "car_remove_cancel")
async def car_remove_cancel(callback: CallbackQuery):
    await callback.message.edit_text("Отменено, машина осталась.")
    await callback.answer()


@router.callback_query(F.data.startswith("car_remove_confirm:"))
async def car_remove_execute(callback: CallbackQuery):
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    account = db.get_effective_google_account(user["id"])

    if not account:
        await callback.message.edit_text("Google Drive не подключён.")
        await callback.answer()
        return

    try:
        link = await cars.archive_and_export_car(account, car_id)
    except Exception:
        logging.exception("car_remove_execute: unexpected error archiving car")
        await callback.message.edit_text(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        await callback.answer()
        return

    if link is None:
        await callback.message.edit_text("Не нашёл эту машину — возможно, уже удалена.")
    else:
        await callback.message.edit_text(f"Машина удалена. Полная история сохранена здесь:\n{link}")
    await callback.answer()
