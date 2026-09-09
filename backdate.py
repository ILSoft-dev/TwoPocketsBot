"""
backdate.py
Режим "задним числом" — /backdate выбирает дату, дальше траты пишутся с
ней вместо сегодняшней, пока не /done (или явная фраза-выход, или таймаут).

АРХИТЕКТУРНОЕ РЕШЕНИЕ: активная сессия хранится в Redis напрямую, а НЕ в
aiogram FSM state. Причина — FSM state в этом боте уже занят другими
диалогами (уточнение неоднозначной категории, дизамбигуация машины), и
режим "задним числом" должен их не блокировать, а сосуществовать с ними:
человек может быть одновременно "в сессии backdate" И "отвечает на вопрос
про машину" для конкретной авто-траты. Если бы это был один FSM state —
пришлось бы либо блокировать обычный ввод на время режима (плохой UX для
фичи, которая специально задумана как "фоновый модификатор", а не диалог),
либо городить вложенные состояния. Отдельный namespace в Redis снимает
этот конфликт полностью.

ЗАЩИТА ОТ ЗАВИСАНИЯ (см. историю бага с /start и переименованием
категории в этом же проекте — зависший режим тихо портит следующие
сообщения): скользящий таймаут 30 минут БЕЗ СООБЩЕНИЙ (не "без трат" —
любое сообщение продлевает), плюс:
  - на КАЖДОЕ подтверждение записи явным текстом с датой, не тихой
    реакцией — человек не может не заметить, что режим всё ещё активен;
  - на КАЖДОЕ входящее сообщение (через BackdateSessionMiddleware,
    main.py) — если сессия истекла, тихо чистится и ОДИН раз уведомляет,
    прежде чем сообщение пойдёт в обычную обработку;
  - /start (start.py) явно чистит эту сессию отдельно от FSM state, т.к.
    Redis-ключ вне state.clear() не входит.

Хранение: значение — "{дата}|{expires_at}", по двум причинам не просто
TTL самого ключа. Во-первых, чтобы отличить "истекло" от "никогда не
было" на СЛЕДУЮЩЕМ сообщении и показать уведомление — Redis TTL этого не
умеет, ключ просто исчезает. Во-вторых, backstop TTL самого ключа
(REDIS_BACKSTOP_TTL_SECONDS, сильно больше 30 минут) — на случай, если
человек замолчал насовсем и lazy-проверка никогда не сработает; тогда
ключ всё равно физически исчезнет сам, просто без уведомления.
"""
import logging
from datetime import date, datetime, timedelta, timezone

import redis.asyncio as redis_asyncio
from aiogram import BaseMiddleware, Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import REDIS_URL, redis_connection_kwargs
from states import BackdateStates
import tx_logic

router = Router()
logger = logging.getLogger(__name__)

SESSION_TIMEOUT_SECONDS = 30 * 60
REDIS_BACKSTOP_TTL_SECONDS = 2 * 60 * 60  # физическая подстраховка, см. docstring выше
MAX_DAYS_BACK = 730  # больше двух лет назад — почти наверняка опечатка в дате
EXIT_PHRASES = {"всё", "все", "хватит", "готово", "done", "стоп", "отмена"}

_redis = redis_asyncio.Redis.from_url(REDIS_URL, decode_responses=True, **redis_connection_kwargs())


def _key(user_id: int) -> str:
    return f"backdate:{user_id}"


def _parse_stored(raw: str) -> tuple[date, datetime]:
    date_str, expires_at_str = raw.split("|")
    return date.fromisoformat(date_str), datetime.fromisoformat(expires_at_str)


async def _set_active(user_id: int, chosen_date: date) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TIMEOUT_SECONDS)
    value = f"{chosen_date.isoformat()}|{expires_at.isoformat()}"
    await _redis.set(_key(user_id), value, ex=REDIS_BACKSTOP_TTL_SECONDS)


async def get_active_date(user_id: int) -> datetime | None:
    """Для input_handler.py: если сейчас активна валидная сессия — дата,
    которой нужно проштамповать НОВУЮ транзакцию (день из сессии, время —
    текущее, чтобы несколько трат за один день задним числом сортировались
    и дедуплицировались осмысленно). None — режим не активен, писать как
    обычно. Не продлевает и не уведомляет — этим занимается touch()
    (вызывается middleware'ом РАНЬШЕ, на этапе получения сообщения)."""
    raw = await _redis.get(_key(user_id))
    if not raw:
        return None
    chosen_date, expires_at = _parse_stored(raw)
    if datetime.now(timezone.utc) > expires_at:
        return None
    now = datetime.now(timezone.utc)
    return datetime.combine(chosen_date, now.time(), tzinfo=timezone.utc)


async def touch(message: Message) -> None:
    """Вызывается BackdateSessionMiddleware на КАЖДОЕ входящее сообщение,
    до всякой другой обработки. Если сессия ещё жива — продлевает её ещё
    на 30 минут от этого сообщения (не только от сохранённых трат — само
    условие таймаута сформулировано как "не было СООБЩЕНИЙ", а не "не
    было трат"). Если истекла — тихо чистит и уведомляет один раз."""
    raw = await _redis.get(_key(message.from_user.id))
    if not raw:
        return
    chosen_date, expires_at = _parse_stored(raw)
    if datetime.now(timezone.utc) > expires_at:
        await _redis.delete(_key(message.from_user.id))
        await message.answer(
            "⏱ Автоматически вышел из режима «задним числом» — не было "
            "сообщений 30+ минут. Дальше траты идут с сегодняшней датой."
        )
    else:
        await _set_active(message.from_user.id, chosen_date)


async def clear(user_id: int) -> None:
    await _redis.delete(_key(user_id))


def _choice_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Вчера", callback_data="backdate:yesterday")
    builder.button(text="Позавчера", callback_data="backdate:day_before")
    builder.button(text="Другая дата", callback_data="backdate:custom")
    builder.adjust(2, 1)
    return builder.as_markup()


async def _activate(message: Message, user_id: int, chosen_date: date) -> None:
    await _set_active(user_id, chosen_date)
    await message.answer(
        f"✅ Режим «задним числом» включён: сегодняшние траты будут "
        f"с датой {chosen_date.strftime('%d.%m.%Y')}.\n"
        f"Пиши траты как обычно — каждая запись подтвердится с этой датой. "
        f"Выйти — /done, или сам отключится через 30 минут без сообщений."
    )


@router.message(Command("backdate"))
async def cmd_backdate(message: Message):
    await message.answer("На какую дату вносим траты?", reply_markup=_choice_keyboard())


@router.callback_query(F.data.startswith("backdate:"))
async def backdate_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    today = datetime.now(timezone.utc).date()
    if choice == "yesterday":
        await _activate(callback.message, callback.from_user.id, today - timedelta(days=1))
    elif choice == "day_before":
        await _activate(callback.message, callback.from_user.id, today - timedelta(days=2))
    else:
        await state.set_state(BackdateStates.waiting_custom_date)
        await callback.message.edit_text("Напиши дату в формате ДД.ММ или ДД.ММ.ГГ (например, 25.08)")
    await callback.answer()


@router.message(BackdateStates.waiting_custom_date)
async def backdate_custom_date(message: Message, state: FSMContext):
    parsed = tx_logic.parse_user_date(message.text or "", max_days_back=MAX_DAYS_BACK)
    if not parsed:
        await message.answer(
            "Не понял дату (или она в будущем / больше двух лет назад). "
            "Формат: ДД.ММ или ДД.ММ.ГГ, например 25.08"
        )
        return
    await state.clear()
    await _activate(message, message.from_user.id, parsed)


@router.message(Command("done"))
async def cmd_done(message: Message):
    was_active = await get_active_date(message.from_user.id) is not None
    await clear(message.from_user.id)
    if was_active:
        await message.answer("Вышел из режима «задним числом». Дальше — сегодняшняя дата.")
    else:
        await message.answer("Режим «задним числом» и так не был включён.")


@router.message(F.text.func(lambda t: bool(t) and t.strip().lower() in EXIT_PHRASES))
async def exit_phrase(message: Message):
    if await get_active_date(message.from_user.id) is None:
        # Режим не активен — это сообщение не про backdate вообще (мало ли
        # кто-то просто написал "всё" по другому поводу), пропускаем дальше
        # к обычным хендлерам, а не глушим молча.
        raise SkipHandler
    await clear(message.from_user.id)
    await message.answer("Вышел из режима «задним числом». Дальше — сегодняшняя дата.")


class SessionMiddleware(BaseMiddleware):
    """Регистрируется ОДИН раз глобально в main.py (dp.message.middleware),
    а не вызывается вручную в каждом хендлере — так новый хендлер в
    будущем не сможет "забыть" продлить/проверить таймаут сессии. Не
    вмешивается в сам ответ хендлера, только продлевает/чистит сессию ДО
    него — см. touch() выше."""

    async def __call__(self, handler, event: Message, data):
        try:
            await touch(event)
        except Exception:
            logger.exception("backdate.SessionMiddleware: touch failed, continuing anyway")
        return await handler(event, data)
