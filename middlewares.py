from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import supabase_client as db
import pin_handler
from states import PinAuthStates


async def _reply(event: TelegramObject, text: str):
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.message.answer(text)
        await event.answer()


class PinMiddleware(BaseMiddleware):
    """
    Гейт доступа: если у юзера установлен pin_hash и нет активной сессии
    в Redis — блокируем любые команды/сообщения, пока не введён верный PIN.
    /start новым юзерам и тем, кто не завершил онбординг, не блокируется.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        state: FSMContext = data["state"]
        tg_user = event.from_user
        if tg_user is None:
            return await handler(event, data)

        user = db.get_user(tg_user.id)
        if not user or not user.get("onboarding_done"):
            return await handler(event, data)

        if not user.get("pin_hash"):
            return await handler(event, data)

        current_state = await state.get_state()
        if current_state == PinAuthStates.waiting_pin.state:
            return await handler(event, data)

        if await pin_handler.has_active_session(tg_user.id):
            return await handler(event, data)

        if await pin_handler.is_locked_out(tg_user.id):
            await _reply(event, "🔒 Слишком много неверных попыток. Попробуйте снова через 5 минут.")
            return

        await state.set_state(PinAuthStates.waiting_pin)
        await _reply(event, "🔐 Введите PIN-код для доступа к боту:")
        return
