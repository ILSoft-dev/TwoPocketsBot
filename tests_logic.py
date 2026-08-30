"""Тесты без сети: forced_category, вопросы, rename, undo, баланс, дубли."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import forced_category, looks_like_question
from tx_logic import (
    apply_soft_delete_last,
    balance_on_hand,
    find_recent_duplicate,
    rename_category_in_rows,
    report_from_rows,
    sum_all_time,
    STATUS_ACTIVE,
    STATUS_DELETED,
)


def test_forced_category():
    assert forced_category("бензин 40р") == "Авто"
    assert forced_category("ремонт авто 200р") == "Авто"
    assert forced_category("ремонт квартиры 200р") is None
    assert forced_category("такси до вокзала") == "Транспорт"
    assert forced_category("кофе") is None


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


if __name__ == "__main__":
    test_forced_category()
    test_looks_like_question()
    test_rename_category()
    test_soft_undo()
    test_cash_on_hand()
    test_duplicate_window()
    print("All logic tests passed.")
