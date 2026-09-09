"""Тесты без сети: forced_category, вопросы, rename, undo, баланс, дубли."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import forced_category, looks_like_question
import auto_expense
from tx_logic import (
    apply_soft_delete_last,
    balance_on_hand,
    find_recent_duplicate,
    rename_category_in_rows,
    report_from_rows,
    sum_all_time,
    STATUS_ACTIVE,
    STATUS_DELETED,
    parse_user_date,
    parse_user_amount,
    filter_by_who,
    sort_by_date_desc,
    compute_period_notes,
    note_category_shape,
    note_hidden_visits,
    note_batch_logging_sessions,
    note_big_purchases_share_of_income,
)


def test_forced_category():
    assert forced_category("бензин 40р") == "Топливо"
    assert forced_category("ремонт авто 200р") == "Ремонт/ТО"
    assert forced_category("ремонт квартиры 200р") is None
    assert forced_category("такси до вокзала") == "Транспорт"
    assert forced_category("кофе") is None


def test_classify_auto_type():
    c = auto_expense.classify_auto_type
    assert c("Бензин Опель") == "Топливо"
    assert c("Заправка дизель") == "Топливо"
    # действие важнее детали — "замена рычагов" это Ремонт/ТО, не Запчасти
    assert c("Замена рычагов daewoo matiz") == "Ремонт/ТО"
    assert c("Ремонт подвески") == "Ремонт/ТО"
    assert c("Техосмотр") == "Ремонт/ТО"
    assert c("Шиномонтаж") == "Ремонт/ТО"
    # деталь/жидкость без слова-действия — Запчасти
    assert c("Передний рычаг матиз") == "Запчасти"
    assert c("Наконечники рулевой тяги") == "Запчасти"
    assert c("Антифриз") == "Запчасти"
    assert c("Тормозные колодки") == "Запчасти"
    # фоллбэк без явных ключевых слов
    assert c("Вонючка в машину") == "Прочее"
    assert c("Чехлы в салон") == "Прочее"
    assert c("Омывайка") == "Прочее"
    # регрессия: "шин" — подстрока "маШИНа/маШИНу", раньше ложно триггерило Ремонт
    assert c("Купил новую машину") == "Прочее"
    assert c("Шторка в машину") == "Прочее"


def test_looks_like_question():
    assert looks_like_question("сколько потратил на кофе?")
    assert looks_like_question("когда менял масло")
    assert not looks_like_question("кофе 150р")


def test_rename_category():
    rows = [
        {"ID": "r1", "Категория": "Продукты", "Статус": STATUS_ACTIVE},
        {"ID": "r2", "Категория": "Авто", "Статус": STATUS_ACTIVE},
    ]
    out, n = rename_category_in_rows(rows, "Продукты", "Еда")
    assert n == 1
    assert out[0]["Категория"] == "Еда"
    assert out[1]["Категория"] == "Авто"
    assert rows[0]["Категория"] == "Продукты"  # вход не мутируем


def test_soft_undo():
    rows = [
        {"ID": "a", "Кто": "ann", "Дата и время": "2026-01-01T10:00:00+00:00",
         "Статус": STATUS_ACTIVE, "Сумма": 10, "Тип": "expense", "Категория": "Кофе"},
        {"ID": "b", "Кто": "ann", "Дата и время": "2026-01-02T10:00:00+00:00",
         "Статус": STATUS_ACTIVE, "Сумма": 20, "Тип": "expense", "Категория": "Кофе"},
        {"ID": "c", "Кто": "bob", "Дата и время": "2026-01-03T10:00:00+00:00",
         "Статус": STATUS_ACTIVE, "Сумма": 30, "Тип": "expense", "Категория": "Кофе"},
    ]
    deleted, new_rows = apply_soft_delete_last(rows, "ann")
    assert deleted["ID"] == "b"
    assert deleted["Статус"] == STATUS_DELETED
    assert new_rows[1]["Статус"] == STATUS_DELETED
    assert new_rows[0]["Статус"] == STATUS_ACTIVE
    assert rows[1]["Статус"] == STATUS_ACTIVE  # вход не мутируем

    none, same = apply_soft_delete_last(rows, "nobody")
    assert none is None
    assert len(same) == 3


def test_cash_on_hand():
    assert balance_on_hand(1000, 200, 50) == 1150
    assert balance_on_hand(None, 200, 50) is None
    rows = [
        {"Статус": STATUS_ACTIVE, "Тип": "income", "Сумма": 100, "Категория": "Зарплата"},
        {"Статус": STATUS_ACTIVE, "Тип": "expense", "Сумма": 30, "Категория": "Кофе"},
        {"Статус": STATUS_DELETED, "Тип": "expense", "Сумма": 999, "Категория": "Кофе"},
    ]
    data = report_from_rows(rows)
    assert data["income"] == 100
    assert data["expense"] == 30
    all_income, all_expense = sum_all_time(rows)
    assert all_expense == 30  # удалённая строка не считается
    assert balance_on_hand(500, all_income, all_expense) == 570


def test_duplicate_window():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rows = [{
        "Статус": STATUS_ACTIVE, "Кто": "ann", "Сумма": 40,
        "Комментарий": "бензин", "Дата и время": "2026-01-01T11:59:00+00:00",
        "ID": "r1",
    }]
    assert find_recent_duplicate(rows, "ann", 40, "бензин", now=now)
    assert find_recent_duplicate(rows, "ann", 40, "бензин", now=now, window_seconds=10) is None


def test_parse_user_date():
    from datetime import date
    today = date(2026, 9, 9)
    assert parse_user_date("25.08", today=today) == date(2026, 8, 25)
    assert parse_user_date("25.08.2026", today=today) == date(2026, 8, 25)
    assert parse_user_date("25.08.26", today=today) == date(2026, 8, 25)
    assert parse_user_date("не дата", today=today) is None
    # будущая дата (даже без явного года) — честно отклоняется, не гадаем про год
    assert parse_user_date("15.09", today=today) is None
    assert parse_user_date("25.12", today=date(2026, 1, 5)) is None
    # с явным годом — ок, даже если это "прошлый год" относительно today
    assert parse_user_date("25.12.25", today=date(2026, 1, 5)) == date(2025, 12, 25)
    # дальше двух лет назад — отклоняется
    assert parse_user_date("01.01.20", today=today) is None


def test_parse_user_amount():
    assert parse_user_amount("150") == 150.0
    assert parse_user_amount("34.99") == 34.99
    assert parse_user_amount("34,99") == 34.99  # запятая как разделитель
    assert parse_user_amount("1 500") == 1500.0  # пробел-разделитель тысяч
    assert parse_user_amount("0") is None
    assert parse_user_amount("-50") is None
    assert parse_user_amount("не число") is None
    assert parse_user_amount("") is None


def test_filter_by_who_and_sort():
    rows = [
        {"ID": "a", "Кто": "ilya", "Дата и время": "2026-08-01T10:00:00+00:00"},
        {"ID": "b", "Кто": "anna", "Дата и время": "2026-08-02T10:00:00+00:00"},
        {"ID": "c", "Кто": "ilya", "Дата и время": "2026-08-03T10:00:00+00:00"},
    ]
    own = filter_by_who(rows, "ilya")
    assert [r["ID"] for r in own] == ["a", "c"]
    assert [r["ID"] for r in sort_by_date_desc(own)] == ["c", "a"]


def _tx(dt, cat, amount, tx_type="expense", who="ilya"):
    return {"Дата и время": dt, "Категория": cat, "Сумма": amount, "Тип": tx_type, "Кто": who}


def test_period_notes_category_shape():
    rows = [
        _tx("2026-08-12T08:00:00+00:00", "Детское питание", 358),
        _tx("2026-08-30T09:00:00+00:00", "Детское питание", 227),
    ]
    for i in range(20):
        rows.append(_tx(f"2026-08-{(i % 28) + 1:02d}T10:{i:02d}:00+00:00", "Продукты", 5 + i))
    notes = note_category_shape(rows, "₽")
    assert any("Детское питание" in n and "почти целиком" in n for n in notes)
    assert any("Продукты" in n and "без явного лидера" in n for n in notes)


def test_period_notes_hidden_visits_vs_batch_session():
    rows = [
        # одна категория почти подряд — "скрытый визит"
        _tx("2026-08-18T11:26:00+00:00", "Продукты", 3.33),
        _tx("2026-08-18T11:26:30+00:00", "Продукты", 2.38),
        _tx("2026-08-18T11:27:00+00:00", "Продукты", 4.68),
        _tx("2026-08-18T11:27:30+00:00", "Продукты", 3.00),
        # разные категории почти подряд — "сессия пакетного ввода", НЕ визит
        _tx("2026-08-04T15:42:00+00:00", "Досуг", 5.3),
        _tx("2026-08-04T15:42:30+00:00", "Продукты", 2.17),
        _tx("2026-08-04T15:43:00+00:00", "Детское питание", 226.64),
        _tx("2026-08-04T15:43:30+00:00", "Лекарства", 81),
    ]
    visits = note_hidden_visits(rows, "₽", min_total=5)
    assert len(visits) == 1 and "18.08" in visits[0] and "Продукты" in visits[0]

    batch = note_batch_logging_sessions(rows, min_sessions=1)
    assert batch is not None and "1 сессия" in batch  # склонение: 1 -> "сессия", не "сессий"


def test_period_notes_big_purchases_share():
    rows = [_tx("2026-08-12T08:00:00+00:00", "Хозтовары", 800)]
    note = note_big_purchases_share_of_income(rows, income_total=1000, currency="₽")
    assert note is not None and "80%" in note
    # при огромном доходе доля незначима — заметки быть не должно
    assert note_big_purchases_share_of_income(rows, income_total=1_000_000, currency="₽") is None


def test_period_notes_silence_on_thin_data():
    rows = [_tx("2026-08-01T10:00:00+00:00", "Продукты", 15)]
    notes = compute_period_notes(rows, income_total=1000, currency="₽", period_days=28)
    assert notes == []  # почти пустые данные -> молчание, не натянутые заметки


def test_ru_plural_boundaries():
    from tx_logic import _ru_plural
    cases = {1: "покупка", 2: "покупки", 4: "покупки", 5: "покупок",
             11: "покупок", 12: "покупок", 21: "покупка", 25: "покупок"}
    for n, expected in cases.items():
        assert _ru_plural(n, "покупка", "покупки", "покупок") == expected, n


if __name__ == "__main__":
    test_forced_category()
    test_classify_auto_type()
    test_looks_like_question()
    test_rename_category()
    test_soft_undo()
    test_cash_on_hand()
    test_duplicate_window()
    test_parse_user_date()
    test_parse_user_amount()
    test_filter_by_who_and_sort()
    test_period_notes_category_shape()
    test_period_notes_hidden_visits_vs_batch_session()
    test_period_notes_big_purchases_share()
    test_period_notes_silence_on_thin_data()
    test_ru_plural_boundaries()
    print("All logic tests passed.")
