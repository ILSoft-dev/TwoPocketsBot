from aiogram import Router
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

import supabase_client as db
import pin_handler
from config import PIN_MAX_ATTEMPTS
from states import PinAuthStates

router = Router()


@router.message(PinAuthStates.waiting_pin)
async def check_pin(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    entered = message.text.strip() if message.text else ""

    if user and user.get("pin_hash") and pin_handler.verify_pin(entered, user["pin_hash"]):
        await pin_handler.open_session(message.from_user.id)
        await state.clear()
        await message.answer("✅ Доступ разрешён. Повтори свою команду/сообщение.")
        return

    attempts = await pin_handler.register_failed_attempt(message.from_user.id)
    remaining = PIN_MAX_ATTEMPTS - attempts
    if remaining > 0:
        await message.answer(f"❌ Неверный PIN. Осталось попыток: {remaining}")
    else:
        await message.answer("🔒 Слишком много неверных попыток. Доступ заблокирован на 5 минут.")
