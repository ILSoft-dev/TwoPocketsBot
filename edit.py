"""
edit.py
/edit — редактирование существующей транзакции: дата, сумма или категория.
Только своей (тот же scope, что у /undo — не трогаем чужие записи, даже
если счёт общий на семью). НЕ трогает лист "Авто" — у авто-трат сумма и
тип там записаны отдельной строкой без общего ID с "Транзакции" (см.
sheets_transactions.save_auto_expense), синхронизировать оттуда нечем
надёжно, так что /edit правит только основной финансовый лист.

UI — список последних транзакций кнопками ("дата · сумма · комментарий"),
а не календарь год→месяц→день. Причина: даже дойдя до конкретной даты по
календарю, всё равно нужен список ("какая из трат этого дня") — то есть
список нужен в любом случае, календарь просто добавил бы лишний уровень
тапов поверх него для типичного случая (поправить недавнее). "Другой
месяц" — запасной путь вглубь истории, сразу на уровень месяца (без
отдельного уровня "год" — он почти никогда не нужен).

После выбора записи — какое поле менять (Дата/Сумма/Категория), потом
соответствующее новое значение. Категория — кнопками из уже существующих
+ "Добавить" для новой (тот же паттерн, что у keyboards.category_choice_keyboard,
но с отдельными callback-префиксами — это ПРАВКА одной строки, не выбор
категории для новой траты, смешивать в одном неймспейсе не стоит).
"""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, User
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

import supabase_client as db
import sheets_transactions as tx
import tx_logic
from states import EditStates

router = Router()
logger = logging.getLogger(__name__)

PAGE_SIZE = 8
MONTHS_BACK = 6
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
             "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def who_label(user: User) -> str:
    """Та же атрибуция, что и в input_handler.py/undo.py — намеренно
    локальная копия, не кросс-импорт, см. существующий паттерн в undo.py."""
    return user.username or user.first_name or str(user.id)


def _row_label(row: dict, currency: str) -> str:
    dt = tx_logic.parse_dt(row.get("Дата и время"))
    date_str = dt.strftime("%d.%m") if dt else "??.??"
    amount = tx_logic.to_float(row.get("Сумма"))
    comment = (row.get("Комментарий") or row.get("Категория") or "").strip()
    if len(comment) > 22:
        comment = comment[:21] + "…"
    return f"{date_str} · {amount:g}{currency} · {comment}"


def _list_keyboard(rows: list[dict], offset: int, currency: str, month_key: str | None = None):
    builder = InlineKeyboardBuilder()
    page = rows[offset:offset + PAGE_SIZE]
    for row in page:
        builder.button(text=_row_label(row, currency), callback_data=f"edit_pick:{row['ID']}")
    rows_layout = [1] * len(page)

    nav_buttons = []
    if offset + PAGE_SIZE < len(rows):
        prefix = f"edit_month:{month_key}" if month_key else "edit_list"
        nav_buttons.append(("Показать ещё", f"{prefix}:{offset + PAGE_SIZE}"))
    if not month_key:
        nav_buttons.append(("Другой месяц", "edit_months"))
    for text, data in nav_buttons:
        builder.button(text=text, callback_data=data)
    if nav_buttons:
        rows_layout.append(len(nav_buttons))

    builder.adjust(*rows_layout) if rows_layout else None
    return builder.as_markup()


def _months_keyboard():
    builder = InlineKeyboardBuilder()
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    for i in range(MONTHS_BACK):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        label = f"{MONTHS_RU[mm - 1].capitalize()} {yy}"
        builder.button(text=label, callback_data=f"edit_month:{yy:04d}-{mm:02d}:0")
    builder.adjust(2)
    return builder.as_markup()


def _field_choice_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Дата", callback_data="edit_field:date")
    builder.button(text="💰 Сумма", callback_data="edit_field:amount")
    builder.button(text="🏷 Категория", callback_data="edit_field:category")
    builder.adjust(3)
    return builder.as_markup()


def _category_keyboard(categories: list[str]):
    """Отдельный неймспейс callback_data (edit_cat_choice*) от
    keyboards.category_choice_keyboard (cat_choice*) — та отвечает за
    выбор категории для НОВОЙ траты, эта — за правку категории у уже
    существующей строки через /edit. Смешивать не стоит, даже если оба
    state-scoped и технически не столкнутся."""
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat, callback_data=f"edit_cat_choice:{cat}")
    builder.button(text="➕ Добавить категорию", callback_data="edit_cat_choice_new")
    builder.adjust(2)
    return builder.as_markup()


@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext):
    await state.clear()
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    who = who_label(message.from_user)
    try:
        rows = await tx.get_own_transactions(user["id"], who)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception:
        logging.exception("cmd_edit: failed to fetch transactions")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return

    if not rows:
        await message.answer("Пока нечего редактировать — у тебя ещё нет ни одной записи.")
        return

    currency = user.get("currency", "RUB")
    await message.answer(
        "Выбери запись для правки:",
        reply_markup=_list_keyboard(rows, 0, currency),
    )


@router.callback_query(F.data.startswith("edit_list:"))
async def edit_list_page(callback: CallbackQuery):
    offset = int(callback.data.split(":", 1)[1])
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    rows = await tx.get_own_transactions(user["id"], who)
    currency = user.get("currency", "RUB")
    await callback.message.edit_reply_markup(reply_markup=_list_keyboard(rows, offset, currency))
    await callback.answer()


@router.callback_query(F.data == "edit_months")
async def edit_months(callback: CallbackQuery):
    await callback.message.edit_text("За какой месяц?", reply_markup=_months_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("edit_month:"))
async def edit_month_page(callback: CallbackQuery):
    _, month_key, offset_str = callback.data.split(":")
    offset = int(offset_str)
    yy, mm = (int(x) for x in month_key.split("-"))

    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    rows = await tx.get_own_transactions(user["id"], who)
    month_rows = [
        r for r in rows
        if (d := tx_logic.parse_dt(r.get("Дата и время"))) and d.year == yy and d.month == mm
    ]

    if not month_rows:
        await callback.answer("За этот месяц записей нет", show_alert=True)
        return

    currency = user.get("currency", "RUB")
    label = f"{MONTHS_RU[mm - 1].capitalize()} {yy}:"
    await callback.message.edit_text(
        label, reply_markup=_list_keyboard(month_rows, offset, currency, month_key=month_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_pick:"))
async def edit_pick(callback: CallbackQuery, state: FSMContext):
    row_id = callback.data.split(":", 1)[1]
    await state.update_data(edit_row_id=row_id)
    await callback.message.edit_text("Что поменять?", reply_markup=_field_choice_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def edit_field_choice(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]

    if field == "date":
        await state.set_state(EditStates.waiting_new_date)
        await callback.message.edit_text("Какая дата должна быть? Формат: ДД.ММ или ДД.ММ.ГГ")
        await callback.answer()
        return

    if field == "amount":
        await state.set_state(EditStates.waiting_new_amount)
        await callback.message.edit_text("Какая сумма должна быть? Просто число, без валюты.")
        await callback.answer()
        return

    if field == "category":
        user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
        categories = [c["name"] for c in await asyncio.to_thread(db.get_categories, user["id"])]
        await callback.message.edit_text("Какая категория?", reply_markup=_category_keyboard(categories))
        await callback.answer()
        return


async def _apply_field_update(message: Message, state: FSMContext, updater, *args,
                              success_text: str, not_found_text: str) -> None:
    """Общий хвост для всех трёх правок — вызов update_transaction_*,
    одинаковая обработка ошибок/scope, один раз, не три копии."""
    data = await state.get_data()
    row_id = data.get("edit_row_id")
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)

    try:
        ok = await updater(user["id"], who, row_id, *args)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        await state.clear()
        return
    except Exception:
        logging.exception("edit.py: unexpected error updating transaction")
        await message.answer(
            "Не получилось сохранить изменение. Если повторится — "
            "переподключи через /start."
        )
        await state.clear()
        return

    await state.clear()
    if not ok:
        await message.answer(not_found_text)
        return
    await message.answer(success_text)


@router.message(EditStates.waiting_new_date)
async def edit_new_date(message: Message, state: FSMContext):
    new_date = tx_logic.parse_user_date(message.text or "")
    if not new_date:
        await message.answer(
            "Не понял дату (или она в будущем / больше двух лет назад). "
            "Формат: ДД.ММ или ДД.ММ.ГГ, например 25.08"
        )
        return
    await _apply_field_update(
        message, state, tx.update_transaction_date, new_date,
        success_text=f"✅ Дата изменена на {new_date.strftime('%d.%m.%Y')}",
        not_found_text="Не нашёл эту запись (может, её уже удалили, или это была "
                       "не твоя запись). Попробуй /edit заново.",
    )


@router.message(EditStates.waiting_new_amount)
async def edit_new_amount(message: Message, state: FSMContext):
    new_amount = tx_logic.parse_user_amount(message.text or "")
    if new_amount is None:
        await message.answer("Не понял сумму. Просто число, например 150 или 34.99")
        return
    await _apply_field_update(
        message, state, tx.update_transaction_amount, new_amount,
        success_text=f"✅ Сумма изменена на {new_amount:g}",
        not_found_text="Не нашёл эту запись (может, её уже удалили, или это была "
                       "не твоя запись). Попробуй /edit заново.",
    )


@router.callback_query(F.data.startswith("edit_cat_choice:"))
async def edit_category_chosen(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    await _apply_field_update(
        callback.message, state, tx.update_transaction_category, category,
        success_text=f"✅ Категория изменена на «{category}»",
        not_found_text="Не нашёл эту запись (может, её уже удалили, или это была "
                       "не твоя запись). Попробуй /edit заново.",
    )
    await callback.answer()


@router.callback_query(F.data == "edit_cat_choice_new")
async def edit_category_new_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditStates.waiting_new_category_name)
    await callback.message.edit_text("Как назвать категорию?")
    await callback.answer()


@router.message(EditStates.waiting_new_category_name)
async def edit_category_new_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым — напиши текстом.")
        return

    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    existing_names = [c["name"] for c in await asyncio.to_thread(db.get_categories, user["id"])]
    if name not in existing_names:
        try:
            await asyncio.to_thread(db.add_category, user["id"], name)
        except Exception:
            logging.exception("edit_category_new_name: unexpected error adding category")
            await message.answer(
                "Не получилось создать категорию — попробуй другое название "
                "или выбери из уже существующих через /categories."
            )
            return

    await _apply_field_update(
        message, state, tx.update_transaction_category, name,
        success_text=f"✅ Категория изменена на «{name}»",
        not_found_text="Не нашёл эту запись (может, её уже удалили, или это была "
                       "не твоя запись). Попробуй /edit заново.",
    )
