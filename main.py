import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.types import BotCommand
from aiohttp import web

from config import (
    BOT_TOKEN, REDIS_URL, REDIS_KEY_PREFIX, PORT, CRON_SECRET,
    SUPABASE_KEEPALIVE_INTERVAL_SECONDS, redis_connection_kwargs, require_env,
)

# До остальных импортов: google_oauth_web / sheets_cache / insights создают
# Redis/Supabase-клиенты уже на import. Если звать require_env() только
# внутри main(), процесс успеет упасть чужой ошибкой раньше явной проверки.
require_env()

from google_oauth_web import oauth_callback
import supabase_client as db
import sheets_transactions as tx
import narrative_report
import reminders
import car_stats
import backdate

# Роутеры — порядок важен! Команды и FSM-специфичные хендлеры должны
# регистрироваться РАНЬШЕ input_handler (там generic F.text/F.voice/F.photo,
# который иначе перехватит любое сообщение, включая команды).
import start
import report
import history
import categories
import family
import settings
import undo
import cars_command
import car_stats_command
import resync
import edit
import input_handler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="🚀 Онбординг / статус"),
    BotCommand(command="guide", description="📖 Подробная инструкция"),
    BotCommand(command="report", description="📊 Сводка за период"),
    BotCommand(command="history", description="🧾 История операций"),
    BotCommand(command="undo", description="↩️ Отменить свою последнюю запись"),
    BotCommand(command="categories", description="📁 Список категорий, добавить свою"),
    BotCommand(command="family", description="👨‍👩‍👧 Семейный бюджет"),
    BotCommand(command="cars", description="🚗 Машины: список, добавить, удалить"),
    BotCommand(command="carstats", description="📈 Статистика по машине"),
    BotCommand(command="settings", description="⚙️ Валюта, период"),
    BotCommand(command="resync", description="🔄 Пересобрать кэш из Google-таблицы"),
    BotCommand(command="edit", description="✏️ Изменить дату существующей записи"),
    BotCommand(command="backdate", description="🕐 Вносить траты задним числом"),
    BotCommand(command="done", description="✅ Выйти из режима «задним числом»"),
]


async def health_check(request):
    return web.Response(text="ok")


async def daily_cron(request: web.Request) -> web.Response:
    """Одна ежедневная задача вместо нескольких: напоминания о пробеге
    (раз в неделю на машину — reminders.py сам решает, кому пора),
    ежемесячная статистика (car_stats.py сам решает, у кого сегодня конец
    периода), сверка зеркала транзакций (tx_mirror) с реальным Sheets —
    страховка от тихого расхождения, см. sheets_transactions.py changelog
    v1.4 — годовой отчёт (narrative_report.py сам решает, что сегодня
    1 января, иначе сразу возвращает 0) — и структурные заметки за
    закрытый период (тот же принцип "сам решает, что сегодня закрытие").
    Один пинг UptimeRobot закрывает всё сразу."""
    if not _cron_secret_matches(request):
        logger.info("cron=forbidden")
        return web.Response(status=403, text="forbidden")
    logger.info("cron=ok")
    bot = request.app["bot"]
    reminders_sent = await reminders.run_reminder_sweep(bot)
    stats_sent = await car_stats.run_monthly_stats_sweep(bot)
    mirrors_fixed = await tx.run_mirror_reconcile_sweep()
    annual_sent = await narrative_report.run_annual_report_sweep(bot)
    notes_sent = await narrative_report.run_period_notes_sweep(bot)
    return web.Response(
        text=(
            f"ok, reminders_sent={reminders_sent}, stats_sent={stats_sent}, "
            f"mirrors_fixed={mirrors_fixed}, annual_sent={annual_sent}, "
            f"notes_sent={notes_sent}"
        )
    )



def _cron_secret_matches(request: web.Request) -> bool:
    """Секрет: ?secret= (UptimeRobot), заголовок X-Cron-Secret или
    Authorization: Bearer. require_env() не даёт стартовать без CRON_SECRET."""
    provided = request.query.get("secret")
    if not provided:
        provided = request.headers.get("X-Cron-Secret")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[len("Bearer "):]
    return provided == CRON_SECRET


async def run_health_server(bot: Bot, storage):
    app = web.Application()
    app["bot"] = bot        # google_oauth_web.oauth_callback needs this to send messages
    app["storage"] = storage  # ...and this to advance the onboarding FSM after OAuth
    app.router.add_get("/health", health_check)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/cron/daily", daily_cron)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def supabase_keepalive_loop():
    """Держит Supabase-проект активным независимо от внешних cron-пингов —
    free-tier проекты Supabase приостанавливаются после ~7 дней без
    обращений к API. Пингует сразу при старте, потом раз в
    SUPABASE_KEEPALIVE_INTERVAL_SECONDS (по умолчанию 6 часов). Ошибки
    ловятся внутри цикла — сам keep-alive никогда не должен уронить бота."""
    while True:
        try:
            ok = await asyncio.to_thread(db.ping)
            logger.info(f"Supabase keep-alive ping: {'ok' if ok else 'failed'}")
        except Exception:
            logger.exception("Supabase keep-alive loop error")
        await asyncio.sleep(SUPABASE_KEEPALIVE_INTERVAL_SECONDS)


async def run_polling_with_retry(dp: Dispatcher, bot: Bot):
    """dp.start_polling() обычно не возвращается, пока бот не остановят
    штатно — если он всё же упал с исключением (сетевой сбой и т.п.),
    перезапускаем через паузу, а не роняем весь процесс."""
    while True:
        try:
            await dp.start_polling(bot)
            break  # штатная остановка (например, dp.stop_polling()) — не перезапускаем
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Polling crashed. Restarting in 5s...")
            await asyncio.sleep(5)


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = RedisStorage.from_url(
        REDIS_URL,
        connection_kwargs=redis_connection_kwargs(),
        key_builder=DefaultKeyBuilder(prefix=REDIS_KEY_PREFIX),
    )
    dp = Dispatcher(storage=storage)

    dp.message.middleware(backdate.SessionMiddleware())  # см. backdate.py — до ЛЮБОГО хендлера

    dp.include_router(start.router)
    dp.include_router(report.router)
    dp.include_router(history.router)
    dp.include_router(categories.router)
    dp.include_router(family.router)
    dp.include_router(settings.router)
    dp.include_router(undo.router)
    dp.include_router(cars_command.router)
    dp.include_router(car_stats_command.router)
    dp.include_router(resync.router)
    dp.include_router(edit.router)
    dp.include_router(backdate.router)
    dp.include_router(input_handler.router)  # последний

    await run_health_server(bot, storage)  # для UptimeRobot + OAuth-callback на Render
    asyncio.create_task(supabase_keepalive_loop())
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await run_polling_with_retry(dp, bot)


if __name__ == "__main__":
    asyncio.run(main())
