import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import supabase_client as db
import cars
import backdate
from google_oauth import build_auth_url
from states import OnboardingStates
from config import GUIDE_URL
from keyboards import (
    currency_keyboard,
    skip_keyboard,
    google_connect_keyboard,
    add_car_or_skip_keyboard,
    add_another_car_keyboard,
    guide_keyboard,
)

router = Router()

HELP_TEXT = (
    "💰 <b>Финансовый дом</b>\n\n"
    "Просто пиши траты и доходы текстом, голосом или фото чека — я сам разберу.\n"
    "Пример: <i>«кофе 150р»</i>, <i>«зарплата 100000р»</i>.\n"
    "⚠️ Валюту указывать обязательно — иначе не смогу отличить сумму от количества.\n\n"
    "📖 Полная инструкция: /guide\n\n"
    "Команды:\n"
    "/report — сводка за период\n"
    "/history — последние траты\n"
    "/categories — свои категории\n"
    "/family — семейный бюджет\n"
    "/undo — отменить последнюю запись\n"
    "/settings — валюта, период\n"
    "/resync — пересобрать кэш из таблицы\n"
    "/edit — изменить дату записи\n"
    "/backdate — вносить траты задним числом\n"
    "/cars — машины\n"
    "/carstats — статистика по машине\n"
    "/guide — подробная инструкция"
)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    # /start ВСЕГДА сбрасывает любое незавершённое состояние диалога —
    # неважно, онбординг это или что угодно ещё (переименование категории,
    # уточнение машины, ввод настроек). Раньше сброс происходил только для
    # НОВых пользователей (через полный проход онбординга до его конца), а
    # для уже онбордингованных /start просто показывал приветствие и
    # выходил, ничего не трогая в state. На практике это означало: если
    # человек бросил диалог на середине и нажал /start, ожидая отмены (что
    # естественно — это стандартный смысл /start в любом боте), состояние
    # оставалось висеть в Redis и подхватывало СЛЕДУЮЩЕЕ обычное сообщение
    # как ответ на давно брошенный вопрос — например, обычная запись траты
    # "хлеб 2р" однажды перехватилась как "новое имя категории" для ранее
    # брошенного /categories → переименовать.
    await state.clear()
    # backdate.py хранит активный режим "задним числом" ОТДЕЛЬНО от FSM
    # state (см. его docstring — намеренно, чтобы сосуществовать с любым
    # другим диалогом), поэтому state.clear() выше её не коснётся — чистим
    # явно, тем же принципом "/start отменяет всё".
    await backdate.clear(message.from_user.id)
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)

    if user.get("onboarding_done"):
        await message.answer(f"С возвращением! 👋\n\n{HELP_TEXT}")
        return

    await state.set_state(OnboardingStates.waiting_currency)
    await message.answer(
        "Привет! Я помогу вести учёт доходов и расходов. 🏠\n\n"
        "Если что — подробная инструкция всегда доступна по /guide.\n\n"
        "Для начала — в какой валюте будем считать?",
        reply_markup=currency_keyboard(),
    )


@router.message(Command("guide"))
async def cmd_guide(message: Message):
    await message.answer(
        "📖 Инструкция: онбординг, все команды, семейный бюджет, машины и "
        "нюансы, которые легко не заметить.",
        reply_markup=guide_keyboard(GUIDE_URL),
    )


@router.callback_query(OnboardingStates.waiting_currency, F.data.startswith("currency:"))
async def choose_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split(":")[1]
    await state.update_data(currency=currency)
    await state.set_state(OnboardingStates.waiting_month_start)
    await callback.message.answer(
        "Отлично. Какого числа у тебя начинается отчётный период "
        "(обычно — день зарплаты)? Введи число от 1 до 28.\n"
        "Если не уверен — просто пришли «1»."
    )
    await callback.answer()


@router.message(OnboardingStates.waiting_month_start)
async def choose_month_start(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 28):
        await message.answer("Нужно число от 1 до 28. Попробуй ещё раз.")
        return

    await state.update_data(month_start=int(text))
    await state.set_state(OnboardingStates.waiting_cash_on_hand)
    await message.answer(
        "Сколько денег у тебя сейчас на руках (для отслеживания остатка)?\n\n"
        "Это шаг для удобства — выбор за тобой, и пропуск никак не помешает "
        "работе бота, если хочешь сохранить конфиденциальность.",
        reply_markup=skip_keyboard("skip_cash"),
    )


@router.callback_query(OnboardingStates.waiting_cash_on_hand, F.data == "skip_cash")
async def skip_cash(callback: CallbackQuery, state: FSMContext):
    await state.update_data(cash_on_hand=None)
    await ask_google_connect(callback.message, state)
    await callback.answer()


@router.message(OnboardingStates.waiting_cash_on_hand)
async def enter_cash(message: Message, state: FSMContext):
    text = message.text.strip().replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await message.answer("Введи число (например, 15000) или нажми «Пропустить».")
        return

    await state.update_data(cash_on_hand=amount)
    await ask_google_connect(message, state)


async def ask_google_connect(message: Message, state: FSMContext):
    user = db.get_or_create_user(message.chat.id, message.chat.username)
    await state.set_state(OnboardingStates.waiting_google_connect)
    auth_url = await build_auth_url(user_id=user["id"], tg_id=message.chat.id)
    await message.answer(
        "Теперь подключи свой Google Drive — туда будут сохраняться все "
        "траты и доходы (не в базу разработчика, а прямо на твой личный "
        "Диск). Откроется страница Google, войди и разреши доступ — пароль "
        "я не вижу.\n\n"
        "Это нужно даже если ты не планируешь объединять бюджет с кем-то — "
        "у тебя всегда будет своя таблица.",
        reply_markup=google_connect_keyboard(auth_url),
    )


@router.message(OnboardingStates.waiting_google_connect)
async def google_connect_reminder(message: Message, state: FSMContext):
    # Подключение идёт через браузер и веб-callback (google_oauth_web.py),
    # не через обычное сообщение — этот хендлер просто мягко напоминает,
    # если человек вместо кнопки написал что-то в чат.
    await message.answer(
        "Нажми кнопку выше, чтобы подключить Google Drive — без этого дальше "
        "не продолжить, боту нужно, куда сохранять твои траты."
    )


# --- Дальше state переключается google_oauth_web.py (веб-callback), не отсюда ---


async def ask_cars_intro(bot, chat_id: int, state: FSMContext):
    """Callable both from a normal Telegram handler (bot=message.bot,
    chat_id=message.chat.id) and from google_oauth_web.py's callback, which
    has no incoming Message object — only the bot instance and tg_id."""
    await state.set_state(OnboardingStates.waiting_cars_intro)
    await bot.send_message(
        chat_id,
        "Отслеживаешь машину? Могу вести пробег, заправки и ремонт по ней "
        "отдельно (и по нескольким сразу, если их несколько в семье).\n\n"
        "Это можно сделать и позже — просто напиши в чат что-то вроде "
        "«пробег опель 305000 км» или «замена масла матиз 35р на пробеге "
        "309000 км», и я сам заведу машину.",
        reply_markup=add_car_or_skip_keyboard(),
    )


@router.callback_query(OnboardingStates.waiting_cars_intro, F.data == "car_skip")
@router.callback_query(OnboardingStates.waiting_add_another_car, F.data == "car_done")
async def cars_done(callback: CallbackQuery, state: FSMContext):
    await finish_onboarding(callback.message, state)
    await callback.answer()


@router.callback_query(
    OnboardingStates.waiting_cars_intro, F.data == "car_add",
)
@router.callback_query(
    OnboardingStates.waiting_add_another_car, F.data == "car_add",
)
async def car_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingStates.waiting_car_name)
    await callback.message.answer("Как назвать машину? (например, «Опель» или «Матиз»)")
    await callback.answer()


@router.message(OnboardingStates.waiting_car_name)
async def car_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название машины текстом.")
        return
    await state.update_data(pending_car_name=name)
    await state.set_state(OnboardingStates.waiting_car_mileage)
    await message.answer(
        f"Какой сейчас пробег у «{name}»? Можно пропустить, если не под рукой.",
        reply_markup=skip_keyboard("skip_car_mileage"),
    )


@router.callback_query(OnboardingStates.waiting_car_mileage, F.data == "skip_car_mileage")
async def car_mileage_skipped(callback: CallbackQuery, state: FSMContext):
    await save_car_and_continue(callback.message, state, mileage=None)
    await callback.answer()


@router.message(OnboardingStates.waiting_car_mileage)
async def car_mileage_entered(message: Message, state: FSMContext):
    mileage = cars.parse_mileage(message.text)
    if mileage is None:
        await message.answer("Не вижу числа — введи пробег цифрами или нажми «Пропустить».")
        return
    await save_car_and_continue(message, state, mileage=mileage)


async def save_car_and_continue(message: Message, state: FSMContext, mileage: float | None):
    data = await state.get_data()
    name = data["pending_car_name"]
    user = db.get_or_create_user(message.chat.id, message.chat.username)
    account = db.get_google_account(user["id"])

    if account:
        try:
            await cars.add_car(
                account, name, who=message.chat.username or str(message.chat.id),
                starting_mileage=mileage,
            )
            await message.answer(f"«{name}» добавлена ✅")
        except Exception:
            logging.exception("save_car_and_continue: unexpected error adding car")
            await message.answer(
                f"«{name}» запомнил, но не смог сохранить — ошибка обращения "
                "к Google Диску. Добери машину позже через /cars."
            )
    else:
        # Не должно происходить в норме (Google подключается раньше этого
        # шага), но на случай гонки/ошибки — не роняем онбординг.
        await message.answer(
            f"«{name}» запомнил, но не смог сохранить — Google Drive не "
            "подключён. Добери машину позже через /cars."
        )

    await state.set_state(OnboardingStates.waiting_add_another_car)
    await message.answer("Добавить ещё одну машину?", reply_markup=add_another_car_keyboard())


async def finish_onboarding(message: Message, state: FSMContext):
    data = await state.get_data()
    user = db.get_or_create_user(message.chat.id, message.chat.username)
    db.finish_onboarding(
        user_id=user["id"],
        currency=data["currency"],
        month_start=data["month_start"],
        cash_on_hand=data.get("cash_on_hand"),
    )
    await state.clear()
    await message.answer(f"Готово! Всё настроено. 🎉\n\n{HELP_TEXT}")
