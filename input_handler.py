"""
input_handler.py
v2.2 - text/voice/photo expense input, now backed by Google Sheets

Changelog:
- v2.2: _reply_google_error() различает GoogleAuthError ("нужно заново
        подключить Google — /start") от всего остального ("временный сбой,
        попробуй позже"). Раньше оба случая получали один и тот же текст
        "переподключи через /start" — для обычного временного сбоя Sheets
        (Google на секунду недоступен, 503 и т.п.) это был лишний и
        пугающий совет: реконнект Google не нужен и не поможет, если
        проблема вообще не в токене. GoogleAuthError теперь долетает и с
        шага обновления токена (google_oauth.refresh_access_token), не
        только с самого вызова Sheets API — см. её докстринг.
- v2.1: parser.extract_quantity() plugged in right after parse_amount —
        quantity/unit threaded through ask_category_choice's FSM state,
        save_and_confirm and the Авто-expense route (route_auto_expense /
        finalize_auto_expense / resolve_pending_intent) down to
        sheets_transactions.save_transaction/save_auto_expense. Category
        guessing and category_map keyword-learning now use item_text (the
        quantity-stripped remainder) instead of the raw remainder — fixes
        a latent bug where "5 литров масла..." would've been learned into
        category_map under the meaningless keyword "5".
- v2.0: db.add_transaction() (Supabase) replaced with sheets_transactions
        (Google Sheets, effective account resolved via family ownership).
        Category "Авто" gets structured parsing (car/type/mileage) via
        cars.match_car_name + auto_expense heuristics, with a car-choice
        disambiguation flow when the car can't be determined automatically.
        Standalone mileage updates ("пробег опель 305000 км" — no currency,
        so parse_amount would otherwise reject them) get a dedicated branch
        that shares the same car-disambiguation flow.
"""
import asyncio
import logging
import re

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReactionTypeEmoji, User
from aiogram.fsm.context import FSMContext

import supabase_client as db
import sheets_transactions as tx
import groq_client
import cars
import auto_expense
import fluid_tracker
import insights
import backdate
from sheets_client import GoogleAuthError
from parser import parse_amount, guess_type, extract_quantity
from config import forced_category, looks_like_question
from keyboards import category_choice_keyboard, car_choice_keyboard
from states import AmbiguousCategoryStates, CarResolutionStates

router = Router()

INCOME_CATEGORIES = ["Зарплата", "Подработка", "Доход"]
# Раньше была одна категория "Авто" — теперь классификатор
# (auto_expense.classify_auto_type) сам решает финальную категорию из
# четырёх, и ЛЮБАЯ из них должна вести в структурированный авто-поток
# (car/type/mileage). Берём константы прямо из auto_expense.py, а не
# дублируем строки здесь — одно место правды на тип.
AUTO_CATEGORIES = {
    auto_expense.TYPE_FUEL, auto_expense.TYPE_REPAIR,
    auto_expense.TYPE_PARTS, auto_expense.TYPE_OTHER,
}


def who_label(user: User) -> str:
    """Attribution for the 'Кто' column — matters mainly in a shared family
    sheet, where transactions from both partners land in one place."""
    return user.username or user.first_name or str(user.id)


async def react_ok(message: Message):
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")],
        )
    except Exception:
        # Реакции могут быть недоступны в некоторых чатах — не критично
        await message.answer("✅ Записано")


async def _reply_google_error(message: Message, exc: Exception) -> None:
    """Единый текст ошибки для сбоев работы с Google Диском — отличает
    GoogleAuthError ("нужно заново подключить Google" — refresh_token
    отозван/невалиден, или сам Sheets API стабильно отдаёт 401) от всего
    остального (сетевой сбой, Google на секунду недоступен — временное,
    повтор скорее всего поможет). Раньше оба случая получали один и тот же
    текст "переподключи через /start", что для временного сбоя — лишний и
    пугающий совет: реконнект Google не нужен и не решит проблему, если
    дело не в токене."""
    if isinstance(exc, GoogleAuthError):
        await message.answer(
            "Доступ к Google Диску истёк — нужно заново подключить его. Зайди в /start."
        )
    else:
        await message.answer(
            "Не получилось обратиться к Google Диску (похоже, временный сбой). "
            "Попробуй ещё раз через минуту."
        )


def first_keyword(remainder: str) -> str:
    words = remainder.strip().split()
    return words[0].lower() if words else "разное"


async def resolve_expense_category(user_id: int, remainder: str) -> tuple[str, bool]:
    """Возвращает (категория, ambiguous). ambiguous=True — нужно спросить юзера."""
    forced = forced_category(remainder)
    if forced:
        return forced, False

    kw_category = await asyncio.to_thread(db.lookup_keyword_category, user_id, remainder)
    if kw_category:
        return kw_category, False

    categories = [c["name"] for c in await asyncio.to_thread(db.get_categories, user_id)]
    try:
        guessed = await asyncio.to_thread(groq_client.categorize_text, remainder, categories)
    except Exception:
        logging.exception("resolve_expense_category: Groq categorize_text failed")
        return "Разное", True  # сбой LLM — не роняем сообщение, просто спросим юзера
    return guessed, guessed == "Разное"


def resolve_income_category(remainder: str) -> tuple[str, bool]:
    lowered = remainder.lower()
    if "зарплат" in lowered or " зп" in f" {lowered}":
        return "Зарплата", False
    if "подработ" in lowered:
        return "Подработка", False
    return "Доход", False


# ------------------------------------------------------------- saving ------
async def save_and_confirm(message: Message, user_id: int, who: str, amount: float,
                           tx_type: str, category: str, source: str, tg_user_id: int,
                           comment: str = "", quantity: float | None = None, unit: str | None = None):
    """tg_user_id — Telegram ID (НЕ внутренний Supabase user_id, это два
    разных числа!) — backdate.py хранит сессию "задним числом" по
    Telegram ID (доступен без похода в базу из message/callback напрямую),
    а не по внутреннему id. Раньше здесь по ошибке передавался user_id
    (внутренний) в backdate.get_active_date — ключи никогда не совпадали,
    и режим "задним числом" тихо не срабатывал вообще, при этом ничего не
    падало (просто get_active_date всегда возвращал None)."""
    backdate_dt = await backdate.get_active_date(tg_user_id)
    try:
        await tx.save_transaction(user_id, who, amount, tx_type, category, source, comment,
                                  quantity=quantity, unit=unit, override_datetime=backdate_dt)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception as e:
        logging.exception("save_and_confirm: unexpected error writing to Sheets")
        await _reply_google_error(message, e)
        return
    if backdate_dt:
        # Явный текст с датой на КАЖДОЕ подтверждение, не тихая реакция —
        # см. backdate.py docstring про защиту от того, чтобы человек забыл,
        # что режим всё ещё активен, и следующая обычная трата ушла не тем
        # числом.
        await message.answer(f"✅ Записано на {backdate_dt.strftime('%d.%m.%Y')}")
    else:
        await react_ok(message)


async def finalize_auto_expense(message: Message, user_id: int, who: str, amount: float,
                                tx_type: str, car_name: str, auto_type: str,
                                description: str, mileage: float | None, source: str,
                                tg_user_id: int,
                                quantity: float | None = None, unit: str | None = None):
    # Автосоздание категории, если её ещё нет в таблице — тот же паттерн,
    # что уже есть в new_category_named для вручную введённого имени.
    # Закрывает практический разрыв при деплое: у уже существующих
    # пользователей в категориях всё ещё старая одна "Авто", а не новые
    # четыре — без этого auto_type писался бы в лист корректно, но не
    # появлялся бы как управляемая категория в /categories, пока юзер не
    # добавит её вручную.
    try:
        existing_names = [c["name"] for c in await asyncio.to_thread(db.get_categories, user_id)]
        if auto_type not in existing_names:
            await asyncio.to_thread(db.add_category, user_id, auto_type)
    except Exception:
        logging.exception("finalize_auto_expense: failed to auto-create category %s", auto_type)

    # tg_user_id — см. docstring save_and_confirm выше, тот же самый баг
    # был и здесь (backdate по внутреннему id вместо Telegram ID).
    backdate_dt = await backdate.get_active_date(tg_user_id)
    try:
        await tx.save_auto_expense(user_id, who, amount, tx_type, car_name, auto_type,
                                   description, mileage, source, quantity=quantity, unit=unit,
                                   override_datetime=backdate_dt)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception as e:
        logging.exception("finalize_auto_expense: unexpected error writing to Sheets")
        await _reply_google_error(message, e)
        return
    if backdate_dt:
        await message.answer(f"✅ Записано на {backdate_dt.strftime('%d.%m.%Y')}")
    else:
        await react_ok(message)
    if mileage is not None:
        await maybe_warn_fluids(message, user_id, car_name, mileage)


async def ask_car_disambiguation(message: Message, state: FSMContext, active_cars: list[dict]):
    if active_cars:
        await state.set_state(CarResolutionStates.waiting_car_choice)
        await message.answer("Это про какую машину?", reply_markup=car_choice_keyboard(active_cars))
    else:
        await state.set_state(CarResolutionStates.waiting_new_car_name)
        await message.answer("У тебя пока нет зарегистрированных машин. Как назвать эту?")


async def route_auto_expense(message: Message, state: FSMContext, user_id: int, who: str,
                             amount: float, tx_type: str, source: str, remainder: str,
                             tg_user_id: int,
                             quantity: float | None = None, unit: str | None = None,
                             preferred_type: str | None = None, item_text: str | None = None):
    """preferred_type — если человек ЯВНО выбрал одну из авто-категорий сам
    (кнопкой в диалоге уточнения, а не через forced_category/category_map/
    угадывание Groq), его выбор побеждает классификатор по тексту. Иначе
    редкое, но реальное несоответствие: нажал "Запчасти", а
    classify_auto_type по тексту решил "Ремонт/ТО" — и записалось не то,
    что человек только что подтвердил.

    item_text — remainder БЕЗ количества/единицы (parser.extract_quantity),
    используется как база для КОММЕНТАРИЯ (после чистки марки через
    cars.clean_description) — без этого "40 л бензин opel" писалось бы в
    комментарий целиком, хотя количество уже отдельно лежит в своей
    колонке. remainder (полный, с числами) по-прежнему используется для
    распознавания машины/пробега/типа — там числа наоборот нужны
    (extract_mileage ищет "... км" по всему тексту). Если item_text не
    передан — используем remainder как есть, просто без дополнительной
    чистки."""
    account = db.get_effective_google_account(user_id)
    try:
        active_cars = await cars.list_active_cars(account) if account else []
    except Exception as e:
        logging.exception("route_auto_expense: unexpected error listing cars")
        await _reply_google_error(message, e)
        return

    base_description = item_text if item_text is not None else remainder
    matched_name = cars.match_car_name(remainder, active_cars)
    mileage = auto_expense.extract_mileage(remainder)
    auto_type = preferred_type or auto_expense.classify_auto_type(remainder)

    if matched_name:
        description = cars.clean_description(base_description, matched_name)
        await finalize_auto_expense(message, user_id, who, amount, tx_type, matched_name,
                                    auto_type, description, mileage, source, tg_user_id, quantity, unit)
        await state.clear()
        return

    if len(active_cars) == 1:
        only_car = active_cars[0]["Машина"]
        description = cars.clean_description(base_description, only_car)
        await finalize_auto_expense(message, user_id, who, amount, tx_type, only_car,
                                    auto_type, description, mileage, source, tg_user_id, quantity, unit)
        await state.clear()
        return

    await state.update_data(pending_intent="auto_expense", pending_payload={
        "amount": amount, "tx_type": tx_type, "auto_type": auto_type,
        "description": base_description, "mileage": mileage, "source": source,
        "quantity": quantity, "unit": unit,
    })
    await ask_car_disambiguation(message, state, active_cars)


async def handle_mileage_message(message: Message, state: FSMContext, user_id: int,
                                 who: str, text: str):
    leftover, mileage = cars.parse_mileage_message(text)
    if mileage is None:
        await message.answer(
            "Не вижу валюту рядом с числом, и не смог понять пробег 🤔\n"
            "Если это трата — укажи валюту («кофе 150р»). Если хочешь "
            "обновить пробег — напиши, например, «пробег опель 305000 км»."
        )
        return

    account = db.get_effective_google_account(user_id)
    try:
        active_cars = await cars.list_active_cars(account) if account else []
    except Exception as e:
        logging.exception("handle_mileage_message: unexpected error listing cars")
        await _reply_google_error(message, e)
        return
    matched_name = cars.match_car_name(leftover, active_cars)

    if matched_name:
        await save_mileage_and_confirm(message, user_id, who, matched_name, mileage)
        return
    if len(active_cars) == 1:
        await save_mileage_and_confirm(message, user_id, who, active_cars[0]["Машина"], mileage)
        return

    await state.update_data(pending_intent="mileage_update", pending_payload={"mileage": mileage})
    await ask_car_disambiguation(message, state, active_cars)


async def maybe_warn_fluids(message: Message, user_id: int, car_name: str, mileage: float):
    """Called after every fresh mileage point (standalone update, reminder
    'без изменений', or a repair message that mentioned mileage) — checks
    whether any tracked fluid is due soon and warns if so. Runs AFTER the
    actual save already succeeded and was confirmed to the user, so any
    failure here is just a missed nice-to-have warning, not a lost
    transaction — still worth catching so it doesn't surface as a scary
    unhandled exception in the logs for something non-critical."""
    account = db.get_effective_google_account(user_id)
    if not account:
        return
    try:
        due = await fluid_tracker.check_due_fluids(account, car_name, mileage)
    except Exception:
        logging.exception("maybe_warn_fluids: unexpected error checking fluids")
        return
    if due:
        await message.answer(fluid_tracker.format_due_warning(due, car_name))


async def save_mileage_and_confirm(message: Message, user_id: int, who: str,
                                   car_name: str, mileage: float):
    try:
        await tx.save_mileage_point(user_id, who, car_name, mileage)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception as e:
        logging.exception("save_mileage_and_confirm: unexpected error writing to Sheets")
        await _reply_google_error(message, e)
        return
    await message.answer(f"Записал пробег «{car_name}»: {mileage:g} км")
    await maybe_warn_fluids(message, user_id, car_name, mileage)


@router.callback_query(F.data.startswith("mileage_same:"))
async def mileage_unchanged(callback: CallbackQuery):
    """'Без изменений' на еженедельном напоминании (reminders.py) — не
    просто игнорируем, а честно логируем точку с тем же пробегом на
    сегодняшнюю дату, чтобы средний км/месяц учитывал реальный простой."""
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    account = db.get_effective_google_account(user["id"])

    if not account:
        await callback.message.answer("Google Drive не подключён — пройди заново /start.")
        await callback.answer()
        return

    try:
        active_cars = await cars.list_active_cars(account)
        car_row = next((c for c in active_cars if c["ID"] == car_id), None)
        if not car_row:
            await callback.message.edit_text("Не нашёл эту машину — возможно, её уже удалили.")
            await callback.answer()
            return

        last_mileage = await cars.get_latest_mileage(account, car_row["Машина"])
        if last_mileage is None:
            await callback.message.edit_text(
                f"Нет предыдущих записей пробега для «{car_row['Машина']}» — напиши пробег вручную."
            )
            await callback.answer()
            return

        await tx.save_mileage_point(user["id"], who, car_row["Машина"], last_mileage, source="Без изменений")
    except Exception as e:
        logging.exception("mileage_unchanged: unexpected error")
        await _reply_google_error(callback.message, e)
        await callback.answer()
        return

    await callback.message.edit_text(f"Записал: «{car_row['Машина']}» без изменений ({last_mileage:g} км)")
    await maybe_warn_fluids(callback.message, user["id"], car_row["Машина"], last_mileage)
    await callback.answer()


# ------------------------------------------------------- car disambiguation --
@router.callback_query(CarResolutionStates.waiting_car_choice, F.data.startswith("car_choice:"))
async def car_choice_picked(callback: CallbackQuery, state: FSMContext):
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    account = db.get_effective_google_account(user["id"])
    try:
        active_cars = await cars.list_active_cars(account) if account else []
    except Exception as e:
        logging.exception("car_choice_picked: unexpected error listing cars")
        await _reply_google_error(callback.message, e)
        await callback.answer()
        return
    car_row = next((c for c in active_cars if c["ID"] == car_id), None)
    car_name = car_row["Машина"] if car_row else "?"

    await resolve_pending_intent(callback.message, state, user["id"], callback.from_user, car_name)
    await callback.answer()


@router.callback_query(CarResolutionStates.waiting_car_choice, F.data == "car_choice_new")
async def car_choice_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CarResolutionStates.waiting_new_car_name)
    await callback.message.answer("Как назвать машину?")
    await callback.answer()


@router.message(CarResolutionStates.waiting_new_car_name)
async def new_car_named(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название машины текстом.")
        return

    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)
    account = db.get_effective_google_account(user["id"])
    if account:
        try:
            await cars.add_car(account, name, who=who)
        except Exception as e:
            logging.exception("new_car_named: unexpected error adding car")
            await _reply_google_error(message, e)
            return

    await resolve_pending_intent(message, state, user["id"], message.from_user, name)


async def resolve_pending_intent(message: Message, state: FSMContext, user_id: int,
                                 from_user: User, car_name: str):
    data = await state.get_data()
    who = who_label(from_user)
    intent = data.get("pending_intent")
    payload = data.get("pending_payload", {})

    if intent == "auto_expense":
        description = cars.clean_description(payload["description"], car_name)
        await finalize_auto_expense(
            message, user_id, who, payload["amount"], payload["tx_type"], car_name,
            payload["auto_type"], description, payload["mileage"], payload["source"],
            from_user.id, payload.get("quantity"), payload.get("unit"),
        )
    elif intent == "mileage_update":
        await save_mileage_and_confirm(message, user_id, who, car_name, payload["mileage"])

    await state.clear()


# --------------------------------------------------------- ambiguous category-
async def ask_category_choice(
    message: Message, state: FSMContext, amount: float, tx_type: str, source: str, remainder: str,
    quantity: float | None = None, unit: str | None = None, item_text: str | None = None,
):
    user = db.get_user(message.from_user.id) or db.get_or_create_user(
        message.from_user.id, message.from_user.username
    )
    categories = [c["name"] for c in db.get_categories(user["id"])]
    if tx_type == "income":
        categories = INCOME_CATEGORIES + [c for c in categories if c not in INCOME_CATEGORIES]

    # Храним ПОЛНЫЙ remainder, а не только первое слово — если в итоге
    # выберут одну из авто-категорий (Топливо/Ремонт-ТО/Запчасти/Прочее),
    # нужен весь текст для разбора машины/типа/пробега.
    # item_text отдельно — "очищенный" от количества/единицы вариант
    # remainder (см. parser.extract_quantity), нужен для обучения
    # category_map по первому слову ("5 литров масла..." иначе выучило бы
    # ключевым словом бессмысленную "5").
    await state.update_data(amount=amount, tx_type=tx_type, source=source, remainder=remainder,
                            quantity=quantity, unit=unit, item_text=item_text or remainder)
    await state.set_state(AmbiguousCategoryStates.waiting_choice)
    await message.answer(
        f"Не уверен насчёт категории для {amount:g}. Выбери подходящую:",
        reply_markup=category_choice_keyboard(categories),
    )


@router.callback_query(AmbiguousCategoryStates.waiting_choice, F.data == "cat_choice_new")
async def category_choice_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AmbiguousCategoryStates.waiting_new_category_name)
    await callback.message.answer("Введи название новой категории:")
    await callback.answer()


@router.message(AmbiguousCategoryStates.waiting_new_category_name)
async def new_category_named(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название категории текстом.")
        return

    data = await state.get_data()
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)
    remainder = data.get("remainder", "")
    quantity, unit = data.get("quantity"), data.get("unit")
    item_text = data.get("item_text", remainder)

    existing_names = [c["name"] for c in await asyncio.to_thread(db.get_categories, user["id"])]
    if name not in existing_names:
        try:
            await asyncio.to_thread(db.add_category, user["id"], name)
        except Exception:
            logging.exception("new_category_named: unexpected error adding category")
            await message.answer(
                "Не получилось создать категорию — попробуй другое название "
                "или выбери из уже существующих через /categories."
            )
            return

    if name in AUTO_CATEGORIES:
        await route_auto_expense(
            message, state, user["id"], who,
            data["amount"], data["tx_type"], data["source"], remainder,
            tg_user_id=message.from_user.id, quantity=quantity, unit=unit,
            preferred_type=name, item_text=item_text,
        )
        return

    await save_and_confirm(message, user["id"], who, data["amount"], data["tx_type"],
                           name, data["source"], tg_user_id=message.from_user.id,
                           comment=remainder, quantity=quantity, unit=unit)
    keyword = first_keyword(item_text)
    if keyword:
        db.remember_keyword_category(user["id"], keyword, name)

    await state.clear()


@router.callback_query(AmbiguousCategoryStates.waiting_choice, F.data.startswith("cat_choice:"))
async def category_chosen(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    data = await state.get_data()
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    remainder = data.get("remainder", "")
    quantity, unit = data.get("quantity"), data.get("unit")
    item_text = data.get("item_text", remainder)

    if category in AUTO_CATEGORIES:
        await route_auto_expense(
            callback.message, state, user["id"], who,
            data["amount"], data["tx_type"], data["source"], remainder,
            tg_user_id=callback.from_user.id, quantity=quantity, unit=unit,
            preferred_type=category, item_text=item_text,
        )
        # route_auto_expense сам решает, чистить ли state (может понадобиться
        # дизамбигуация машины — тогда state переходит в CarResolutionStates)
        await callback.answer()
        return

    await save_and_confirm(callback.message, user["id"], who, data["amount"], data["tx_type"],
                           category, data["source"], tg_user_id=callback.from_user.id,
                           comment=remainder, quantity=quantity, unit=unit)
    keyword = first_keyword(item_text)
    if keyword:
        db.remember_keyword_category(user["id"], keyword, category)

    await state.clear()
    await callback.answer()


# ------------------------------------------------------------ media intake ---
async def process_text_input(message: Message, state: FSMContext, text: str, source: str):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not user.get("onboarding_done"):
        await message.answer("Сначала пройди короткую настройку: /start")
        return

    who = who_label(message.from_user)
    parsed = parse_amount(text)

    if parsed is None:
        # ВАЖЕН ПОРЯДОК: сначала проверяем "это вопрос?" — иначе "какой
        # пробег у матиза?" попал бы в ветку обновления пробега ниже (там
        # тоже есть слово "пробег", но это вопрос, а не новое показание).
        if looks_like_question(text):
            answer = await insights.answer_question(user["id"], text)
            await message.answer(answer)
            return
        # Отдельная ветка: "пробег опель 305000 км" — валюты в таком
        # сообщении нет и не будет, это не трата, а обновление пробега.
        if re.search(r"(?i)пробег", text):
            await handle_mileage_message(message, state, user["id"], who, text)
            return
        await message.answer(
            "Не вижу валюту рядом с числом 🤔\n"
            "Указывай так: «кофе 150р», «зарплата 100000₽» — иначе не могу "
            "отличить сумму от количества/массы."
        )
        return

    amount, _currency_code, remainder = parsed
    tx_type = guess_type(remainder)
    # item_text — remainder без ведущего "5 литров"/"килограмм" и т.п.,
    # используется для категоризации и обучения category_map, чтобы туда
    # не попадало число вместо осмысленного слова. Полный remainder
    # по-прежнему идёт в комментарий и в разбор авто-трат (там количество/
    # единица не убираются — не мешают распознаванию машины/пробега).
    quantity, unit, item_text = extract_quantity(remainder)

    if tx_type == "income":
        category, ambiguous = resolve_income_category(remainder)
    else:
        category, ambiguous = await resolve_expense_category(user["id"], item_text)

    if ambiguous:
        await ask_category_choice(message, state, amount, tx_type, source, remainder,
                                  quantity, unit, item_text)
        return

    if category in AUTO_CATEGORIES:
        await route_auto_expense(message, state, user["id"], who, amount, tx_type, source, remainder,
                                 tg_user_id=message.from_user.id, quantity=quantity, unit=unit,
                                 item_text=item_text)
        return

    await save_and_confirm(message, user["id"], who, amount, tx_type, category, source,
                           tg_user_id=message.from_user.id, comment=remainder, quantity=quantity, unit=unit)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    file = await message.bot.get_file(message.voice.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    try:
        text = await asyncio.to_thread(
            groq_client.transcribe_voice, file_bytes.read(), filename="voice.ogg"
        )
    except Exception:
        logging.exception("handle_voice: Groq transcribe_voice failed")
        await message.answer("Не удалось распознать голос (сбой сервиса), попробуй ещё раз или напиши текстом.")
        return
    if not text:
        await message.answer("Не удалось распознать голос, попробуй ещё раз.")
        return
    await process_text_input(message, state, text, source="voice")


@router.message(F.photo)
async def handle_receipt_photo(message: Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not user.get("onboarding_done"):
        await message.answer("Сначала пройди короткую настройку: /start")
        return

    largest_photo = message.photo[-1]
    file = await message.bot.get_file(largest_photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    try:
        total = await asyncio.to_thread(groq_client.extract_receipt_total, file_bytes.read())
    except Exception:
        logging.exception("handle_receipt_photo: Groq extract_receipt_total failed")
        await message.answer(
            "Не смог обработать фото (сбой сервиса). Попробуй ещё раз "
            "или напиши сумму текстом."
        )
        return

    if total is None:
        await message.answer("Не смог распознать сумму на чеке. Попробуй сфотографировать чётче.")
        return

    await ask_category_choice(message, state, total, "expense", "receipt", remainder="")


# Этот хендлер должен регистрироваться ПОСЛЕДНИМ в диспетчере (после команд и FSM-специфичных
# хендлеров), чтобы не перехватывать текст, относящийся к онбордингу/настройкам/т.д.
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    await process_text_input(message, state, message.text, source="text")
