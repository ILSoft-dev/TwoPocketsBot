"""
google_oauth_web.py
v1.1 - aiohttp route for the Google OAuth callback.

Registered into the same aiohttp app that already serves /health for
UptimeRobot (see main.py). The bot instance isn't a module-level global in
this codebase (unlike PixKeep), so it's reached via request.app["bot"], and
the FSM storage via request.app["storage"] — needed to check/advance
onboarding state, since this callback fires from a browser redirect, not a
normal Telegram update the dispatcher would otherwise route through FSM.
"""
import logging

import aiohttp
from aiohttp import web
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import supabase_client as db
import sheets_client
from google_oauth import exchange_code, get_user_email
from states import OnboardingStates
from start import ask_cars_intro


async def oauth_callback(request: web.Request) -> web.Response:
    state_param = request.query.get("state", "")
    code = request.query.get("code", "")
    error = request.query.get("error")
    if error:
        return web.Response(text=f"Отказано в доступе: {error}", content_type="text/plain")

    try:
        pending, access_token, refresh_token = await exchange_code(state_param, code)
        user_id, tg_id = pending["user_id"], pending["tg_id"]
        email = await get_user_email(access_token)

        async with sheets_client.new_session() as session:
            spreadsheet_id = await sheets_client.create_budget_spreadsheet(
                session, access_token, "Финансовый дом"
            )

        db.save_google_tokens(user_id, email, access_token, refresh_token, spreadsheet_id)

        bot = request.app["bot"]
        await bot.send_message(
            tg_id,
            f"Google Drive подключён ✅ ({email})\n"
            "Создал таблицу «Финансовый дом» с листами Транзакции/Авто/Пробег/Машины."
        )

        # Продолжаем онбординг только если человек реально ждал именно этого
        # шага — иначе (например, переподключение Google из /settings уже
        # после онбординга) не лезем в машины непрошено.
        fsm_key = StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id)
        fsm = FSMContext(storage=request.app["storage"], key=fsm_key)
        current_state = await fsm.get_state()
        if current_state == OnboardingStates.waiting_google_connect.state:
            await ask_cars_intro(bot, tg_id, fsm)

        return web.Response(
            text="Готово! Google Drive подключён, таблица создана. Можешь вернуться в Telegram.",
            content_type="text/plain",
        )
    except Exception as e:
        logging.exception("google oauth callback failed")
        return web.Response(text=f"Ошибка авторизации: {e}", content_type="text/plain")
