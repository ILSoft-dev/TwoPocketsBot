"""
resync.py
Ручной аналог ежедневной сверки зеркала (см. sheets_transactions.
run_mirror_reconcile_sweep). Если пользователь заметил, что /report или
/history показывают что-то не то (например, сразу после того, как сам
отредактировал строку прямо в Google-таблице — зеркало ЭТОГО не видит,
см. sheets_transactions.py docstring), можно не ждать ночной сверки, а
сразу пересобрать зеркало командой /resync.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("resync"))
async def cmd_resync(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    try:
        account = db.get_effective_google_account(user["id"])
    except Exception:
        account = None
    if not account:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return

    db.mirror_clear(account["id"])
    await message.answer("🔄 Кэш очищен, читаю таблицу заново…")

    try:
        rows = await tx._load_tx_rows(account)  # noqa: SLF001 — намеренно внутренняя функция
        db.mirror_hydrate(account["id"], rows)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception:
        logger.exception("cmd_resync: hydration failed")
        await message.answer(
            "Не получилось перечитать таблицу сейчас (Google Диск недоступен). "
            "Кэш всё равно очищен — следующий /report или /history попробуют снова."
        )
        return

    await message.answer(f"✅ Готово. Перечитано строк: {len(rows)}.")
