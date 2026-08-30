from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def currency_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in ("BYN", "RUB", "USD"):
        builder.button(text=code, callback_data=f"currency:{code}")
    builder.adjust(3)
    return builder.as_markup()


def skip_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data=callback_data)
    return builder.as_markup()


def yes_no_keyboard(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да", callback_data=yes_cb)
    builder.button(text="Нет", callback_data=no_cb)
    builder.adjust(2)
    return builder.as_markup()


def category_choice_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat, callback_data=f"cat_choice:{cat}")
    builder.button(text="➕ Добавить категорию", callback_data="cat_choice_new")
    builder.adjust(2)
    return builder.as_markup()


def categories_menu_keyboard(categories: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=f"✏️ {cat['name']}", callback_data=f"cat_rename:{cat['name']}")
    builder.button(text="➕ Добавить категорию", callback_data="cat_add")
    builder.adjust(2)
    return builder.as_markup()


def family_invite_keyboard(invite_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"family_accept:{invite_id}")
    builder.button(text="❌ Отклонить", callback_data=f"family_decline:{invite_id}")
    builder.adjust(2)
    return builder.as_markup()


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💱 Валюта", callback_data="settings:currency")
    builder.button(text="📅 Начало периода", callback_data="settings:period")
    builder.adjust(1)
    return builder.as_markup()


def google_connect_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Подключить Google Drive", url=url)
    return builder.as_markup()


def guide_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📖 Открыть инструкцию", url=url)
    return builder.as_markup()


def add_car_or_skip_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить машину", callback_data="car_add")
    builder.button(text="Пропустить", callback_data="car_skip")
    builder.adjust(1)
    return builder.as_markup()


def add_another_car_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё одна машина", callback_data="car_add")
    builder.button(text="Готово", callback_data="car_done")
    builder.adjust(1)
    return builder.as_markup()


def car_choice_keyboard(active_cars: list[dict]) -> InlineKeyboardMarkup:
    """active_cars: rows from cars.list_active_cars() — needs ID + Машина."""
    builder = InlineKeyboardBuilder()
    for car in active_cars:
        builder.button(text=car["Машина"], callback_data=f"car_choice:{car['ID']}")
    builder.button(text="➕ Другая машина", callback_data="car_choice_new")
    builder.adjust(2)
    return builder.as_markup()


def mileage_reminder_keyboard(car_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Без изменений", callback_data=f"mileage_same:{car_id}")
    return builder.as_markup()


def cars_menu_keyboard(active_cars: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for car in active_cars:
        builder.button(text=f"🗑 {car['Машина']}", callback_data=f"car_remove:{car['ID']}")
    builder.button(text="➕ Добавить машину", callback_data="car_manage_add")
    builder.adjust(1)
    return builder.as_markup()


def car_delete_confirm_keyboard(car_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=f"car_remove_confirm:{car_id}")
    builder.button(text="Отмена", callback_data="car_remove_cancel")
    builder.adjust(2)
    return builder.as_markup()


def report_periods_keyboard(periods: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """periods: [(periods_back, label), ...] — periods_back=0 это текущий
    период (открытый, "до сейчас"), 1+ — закрытые предыдущие периоды."""
    builder = InlineKeyboardBuilder()
    for periods_back, label in periods:
        builder.button(text=label, callback_data=f"report_period:{periods_back}")
    builder.adjust(2)
    return builder.as_markup()


def family_leave_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, выйти", callback_data="family_leave_confirm")
    builder.button(text="Отмена", callback_data="family_leave_cancel")
    builder.adjust(2)
    return builder.as_markup()
