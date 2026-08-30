from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import logging

import supabase_client as db
import sheets_transactions as tx
from keyboards import categories_menu_keyboard
from states import CategoryStates

router = Router()


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    cats = db.get_categories(user["id"])
    names = "\n".join(f"• {c['name']}" for c in cats)
    await message.answer(
        f"📁 <b>Твои категории:</b>\n{names}\n\n"
        "Нажми на категорию, чтобы переименовать, или добавь новую:",
        reply_markup=categories_menu_keyboard(cats),
    )


@router.callback_query(F.data == "cat_add")
async def start_add_category(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CategoryStates.waiting_new_name)
    await callback.message.answer("Введи название новой категории:")
    await callback.answer()


@router.message(CategoryStates.waiting_new_name)
async def add_category(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    name = message.text.strip()
    db.add_category(user["id"], name)
    await state.clear()
    await message.answer(f"✅ Категория «{name}» добавлена.")


@router.callback_query(F.data.startswith("cat_rename:"))
async def start_rename_category(callback: CallbackQuery, state: FSMContext):
    old_name = callback.data.split(":", 1)[1]
    await state.update_data(old_name=old_name)
    await state.set_state(CategoryStates.waiting_rename_new)
    await callback.message.answer(f"Новое название для «{old_name}»:")
    await callback.answer()


@router.message(CategoryStates.waiting_rename_new)
async def rename_category(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    data = await state.get_data()
    new_name = message.text.strip()
    db.rename_category(user["id"], data["old_name"], new_name)
    try:
        await tx.rename_category_in_sheet(user["id"], data["old_name"], new_name)
    except Exception:
        logging.exception("rename_category: failed to update Sheets history")
        await state.clear()
        await message.answer(
            f"✅ «{data['old_name']}» переименована в «{new_name}» в списке категорий.\n"
            "Историю в Google-таблице обновить не удалось — новые записи "
            "пойдут уже с новым именем."
        )
        return
    await state.clear()
    await message.answer(f"✅ «{data['old_name']}» переименована в «{new_name}».")
