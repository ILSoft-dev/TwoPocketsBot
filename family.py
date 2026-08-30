from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery

import supabase_client as db
from keyboards import family_invite_keyboard, family_leave_confirm_keyboard

router = Router()


@router.message(Command("family"))
async def cmd_family(message: Message, command: CommandObject):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    args = (command.args or "").strip()

    if args == "leave":
        family_id = db.get_family_id(user["id"])
        if not family_id:
            await message.answer("Ты пока не в семейном бюджете.")
            return
        await message.answer(
            "Точно выйти из семейного бюджета? История транзакций останется, "
            "но новые записи снова будут только твои.",
            reply_markup=family_leave_confirm_keyboard(),
        )
        return

    if not args.startswith("@"):
        await message.answer(
            "Чтобы объединить бюджет с партнёром, напиши:\n"
            "<code>/family @username</code>\n\n"
            "Чтобы выйти из семейного бюджета: <code>/family leave</code>"
        )
        return

    target_username = args.lstrip("@")
    target_user = db.get_user_by_username(target_username)
    if not target_user:
        await message.answer(
            "Не нашёл такого пользователя — он должен хотя бы раз написать боту (/start)."
        )
        return

    if db.get_family_id(user["id"]) and db.get_family_id(user["id"]) == db.get_family_id(target_user["id"]):
        await message.answer("Вы уже в одном семейном бюджете.")
        return

    invite = db.create_family_invite(user["id"], target_user["tg_id"])
    await message.answer(f"Приглашение отправлено @{target_username}. Ждём подтверждения.")
    await message.bot.send_message(
        target_user["tg_id"],
        f"👨‍👩‍👧 @{message.from_user.username or user['tg_id']} предлагает объединить "
        f"семейный бюджет. Общий кошелёк — доходы и расходы будут видны обоим.",
        reply_markup=family_invite_keyboard(invite["id"]),
    )


@router.callback_query(F.data.startswith("family_accept:"))
async def accept_invite(callback: CallbackQuery):
    invite_id = int(callback.data.split(":")[1])
    accepting_user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    family_id = db.accept_family_invite(invite_id, accepting_user["id"])

    if not family_id:
        await callback.message.answer("Приглашение уже неактуально.")
        await callback.answer()
        return

    await callback.message.answer("✅ Готово! Теперь у вас общий семейный бюджет.")
    await callback.answer()


@router.callback_query(F.data.startswith("family_decline:"))
async def decline_invite(callback: CallbackQuery):
    await callback.message.answer("Приглашение отклонено.")
    await callback.answer()


@router.callback_query(F.data == "family_leave_confirm")
async def leave_family_confirm(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    db.leave_family(user["id"])
    await callback.message.answer("Вы вышли из семейного бюджета.")
    await callback.answer()


@router.callback_query(F.data == "family_leave_cancel")
async def leave_family_cancel(callback: CallbackQuery):
    await callback.message.answer("Отменено, остаёшься в семейном бюджете.")
    await callback.answer()
