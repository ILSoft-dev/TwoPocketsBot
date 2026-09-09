from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    waiting_currency = State()
    waiting_month_start = State()
    waiting_cash_on_hand = State()
    waiting_google_connect = State()   # ждём OAuth-callback (внешний, не текст)
    waiting_cars_intro = State()       # "хочешь добавить машину?" да/пропустить
    waiting_car_name = State()
    waiting_car_mileage = State()
    waiting_add_another_car = State()


class CategoryStates(StatesGroup):
    waiting_new_name = State()
    waiting_rename_old = State()
    waiting_rename_new = State()


class FamilyStates(StatesGroup):
    waiting_username = State()


class SettingsStates(StatesGroup):
    waiting_currency = State()
    waiting_month_start = State()


class AmbiguousCategoryStates(StatesGroup):
    waiting_choice = State()
    waiting_new_category_name = State()


class CarResolutionStates(StatesGroup):
    """Reused for both 'какая машина?' disambiguation flows: an Авто-category
    expense whose car wasn't mentioned/matched, and a standalone 'пробег ...'
    mileage update in the same situation. FSM data carries an 'intent' field
    ('auto_expense' | 'mileage_update') so the resolution handler knows what
    to do once a car is picked/typed."""
    waiting_car_choice = State()
    waiting_new_car_name = State()


class CarManageStates(StatesGroup):
    """/cars вне онбординга — тот же смысл, что у OnboardingStates.
    waiting_car_name/waiting_car_mileage, просто отдельные состояния, чтобы
    не путать 'иду по онбордингу' с 'просто добавляю машину из /cars'."""
    waiting_new_name = State()
    waiting_new_mileage = State()


class BackdateStates(StatesGroup):
    """Только для шага ВЫБОРА даты (после /backdate, кнопка 'Другая дата').
    Сама активная сессия 'задним числом' — это НЕ FSM-состояние (см.
    backdate.py) — она должна сосуществовать с любым другим состоянием
    (уточнение категории, дизамбигуация машины и т.п.), а не блокировать
    обычный ввод трат на время своего действия."""
    waiting_custom_date = State()


class EditStates(StatesGroup):
    """/edit — после выбора конкретной транзакции кнопкой, ждём новую дату
    текстом."""
    waiting_new_date = State()
