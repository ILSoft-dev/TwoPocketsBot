"""
undo.py
v0.2 - /undo, теперь через sheets_transactions (мягкое удаление в Google
Sheets — колонка "Статус" -> "Удалена", не физическое удаление строки).

Изначально этого файла не было среди присланных, хоть main.py/README на
него ссылались — v0.1 был минимальной заглушкой на Supabase; здесь просто
переключил ту же логику на новое хранилище.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx

router = Router()


def who_label(user) -> str:
    return user.username or user.first_name or str(user.id)


@router.message(Command("undo"))
async def cmd_undo(message: Message):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)

    try:
        deleted = await tx.soft_delete_last(user["id"], who)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception:
        logging.exception("undo: unexpected error updating Sheets")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return

    if not deleted:
        await message.answer("Нет твоих записей для отмены.")
        return

    sign = "+" if deleted["Тип"] == "income" else "-"
    amount = tx.to_float(deleted["Сумма"])
    await message.answer(f"Отменено: {sign}{amount:g} ({deleted['Категория']})")
