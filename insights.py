"""
insights.py
v1.4 - чат-вопросы по уже накопленным данным ("сколько потратил на корм в
июне?", "какой пробег у матиза за август?", "когда менял масло на опеле?",
"сколько мороженого я купил?").

Архитектура: LLM (groq_client.parse_question) только РАЗБИРАЕТ вопрос в
структурированный запрос — категорию/машину/период. Сам ответ считает наш
код по данным из Sheets, без всякой фантазии модели в цифрах.

Разрешение года для месяца без явного года — правило "самый недавний
прошедший такой месяц": если месяц уже был в этом году — берём этот год,
если ещё не наступил — прошлый год. Явно названный год всегда побеждает.

Changelog:
- v1.4: _answer_last_date получила _guess_auto_keyword() — детерминированный
        фоллбэк на СЫРОМ тексте вопроса (те же основы, что
        auto_expense.FUEL_KEYWORDS/REPAIR_KEYWORDS/MAINTENANCE_KEYWORDS
        используют при категоризации трат), когда LLM не дала item. В
        проде "когда заправлял опель?"/"когда покупал бензин на опель?"
        оба падали в безключевой фоллбэк — для глагольных формулировок
        ("заправлял") LLM вообще не от чего оттолкнуться (нет
        существительного), а для "бензин" не помогло дополнение промпта
        одно (см. groq_client.py v1.5) — нужна независимая от LLM
        подстраховка.
- v1.3: intent=quantity ("сколько мороженого я купил?") — суммирует
        Количество/Единица (см. parser.extract_quantity /
        sheets_transactions.save_transaction), а не Сумму в деньгах.
        Период учитывается как у spending/income (resolve_period), в
        отличие от last_date. Разные единицы у совпавших записей не
        складываются молча в одну кучу — суммируются отдельно по каждой.
        Записи без Количества (старые, до этой колонки) не приравниваются
        к 1 и не пропадают молча — либо честный отказ "нечем посчитать",
        если такое количество совпадений НЕ имеет вообще ни одной записи с
        количеством, либо пометка "N записей без количества не учтены".
- v1.2: _answer_last_date с car_name, но без keyword (LLM не распознала
        item — см. groq_client.py v1.3) больше не выдаёт молча "последнее
        событие по машине вообще" за ответ по существу ("Последний раз
        трату на «Матиз»" — вводило в заблуждение, будто это именно про
        то, что спросили). Теперь честно показывает, ЧТО реально нашлось
        (Тип + Описание), и просит уточнить вопрос.
- v1.1: intent=last_date ("когда...") — ищет дату последнего подходящего
        события по ВСЕЙ истории (period_type сознательно игнорируется, см.
        _answer_last_date). car_name задан -> лист Авто через
        cars.get_last_auto_event, иначе -> общие Транзакции по товару/
        категории, как в _answer_money, но берём первую (самую свежую)
        строку вместо суммы.
"""
from datetime import datetime, timezone
from calendar import monthrange
import asyncio
import logging

import supabase_client as db
import cars
import auto_expense
import groq_client
from report import period_start
from sheets_transactions import get_transactions_in_range, to_float, NoGoogleAccount

MONTH_NAMES = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


def resolve_year_for_month(month: int, explicit_year: int | None) -> int:
    """"В июне?" без года — самый недавний ПРОШЕДШИЙ июнь: этот год, если
    месяц уже наступил, иначе прошлый (этот июнь ещё не начался — значит,
    данных там быть не может, спрашивают про прошлый)."""
    if explicit_year is not None:
        return explicit_year
    now = datetime.now(timezone.utc)
    return now.year if month <= now.month else now.year - 1


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    since = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = monthrange(year, month)[1]
    until = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return since, until


def resolve_period(period_type: str, month: int | None, year: int | None,
                   month_start_day: int) -> tuple[datetime | None, datetime | None, str]:
    """Возвращает (since, until, human_label). until=None — открытый диапазон
    (до сейчас)."""
    if period_type == "specific_month" and month:
        actual_year = resolve_year_for_month(month, year)
        since, until = month_bounds(actual_year, month)
        label = f"{MONTH_NAMES.get(month, month)} {actual_year}"
        return since, until, label

    if period_type == "current_period":
        since = period_start(month_start_day)
        return since, None, "текущий период"

    # all_time или что-то неожиданное — без ограничений
    return None, None, "всё время"


async def answer_question(user_id: int, text: str) -> str:
    """Никогда не поднимает исключение наружу — любой сбой (сеть,
    Supabase, Groq) превращается в вежливое сообщение, а не в тишину."""
    try:
        return await _answer_question_inner(user_id, text)
    except Exception:
        logging.exception("answer_question: unexpected error")
        return "Не получилось ответить на вопрос — попробуй ещё раз позже."


async def _answer_question_inner(user_id: int, text: str) -> str:
    user = await asyncio.to_thread(db.get_user_by_id, user_id)
    if not user:
        return "Не нашёл твой профиль — попробуй /start."

    account = await asyncio.to_thread(db.get_effective_google_account, user_id)
    active_cars = []
    if account:
        try:
            active_cars = await cars.list_active_cars(account)
        except Exception:
            active_cars = []  # не критично для вопросов не про машины

    categories = [c["name"] for c in await asyncio.to_thread(db.get_categories, user_id)]
    car_names = [c["Машина"] for c in active_cars]

    parsed = await asyncio.to_thread(groq_client.parse_question, text, categories, car_names)
    if parsed is None:
        return (
            "Не понял вопрос 🤔 Попробуй переформулировать, например: "
            "«сколько я потратил на продукты в июле?» или "
            "«какой пробег у опеля за август?»."
        )

    intent = parsed.get("intent", "unknown")
    if intent == "unknown":
        return (
            "Не понял, о чём вопрос — про траты, доходы, пробег или "
            "\"когда\"? Попробуй переформулировать."
        )

    # last_date ("когда я в последний раз...") по смыслу не ограничен
    # отчётным периодом — сознательно НЕ вызывает resolve_period, ищет по
    # всей истории, даже если LLM всё равно что-то заполнила в period_type.
    if intent == "last_date":
        return await _answer_last_date(user_id, account, parsed.get("car_name"),
                                       parsed.get("category"), parsed.get("item"), text)

    since, until, label = resolve_period(
        parsed.get("period_type", "current_period"),
        parsed.get("month"), parsed.get("year"),
        user.get("month_start", 1),
    )

    if intent == "mileage":
        return await _answer_mileage(account, parsed.get("car_name"), since, until, label)

    if intent == "quantity":
        return await _answer_quantity(user_id, parsed.get("category"), parsed.get("item"),
                                      since, until, label)

    return await _answer_money(user_id, intent, parsed.get("category"), parsed.get("item"),
                               since, until, label, user.get("currency", "RUB"))


def _format_date(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return dt.strftime("%d.%m.%Y")


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование числительного с существительным: 1 запись,
    2-4 записи, 5-20 и 0 записей, 21 запись, 22 записи и т.д."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    n1 = n_abs % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


async def _answer_quantity(user_id: int, category: str | None, item: str | None,
                           since, until, label: str) -> str:
    """"Сколько мороженого я купил?" — суммирует поле Количество (НЕ Сумму
    в деньгах — за это отвечает _answer_money/intent=spending). Единица
    измерения может отличаться от записи к записи (кто-то же не всегда
    покупает одно и то же количество/фасовку) — суммируем ОТДЕЛЬНО по
    каждой встретившейся единице, а не молча складываем шт с литрами.

    Записи без указанного Количества (старые, до появления этих колонок,
    или просто без распознанного количества в тексте) НЕ приравниваем к 1
    и не выбрасываем молча — либо честно говорим, что посчитать нечем
    (если ни одной записи с количеством не нашлось), либо считаем по тем,
    где количество есть, и отдельно упоминаем, сколько записей осталось
    "за бортом" подсчёта."""
    keyword = item or category
    if not keyword:
        return "Не понял, количество ЧЕГО считать — назови товар."

    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    expense_rows = [r for r in rows if r["Тип"] == "expense"]
    if item:
        matching = [r for r in expense_rows if _item_matches(item, str(r.get("Комментарий", "")))]
    else:
        matching = [r for r in expense_rows if r["Категория"] == category]

    if not matching:
        return f"Не нашёл покупок «{keyword}» за {label}."

    with_qty = [r for r in matching if str(r.get("Количество", "")).strip() != ""]
    without_qty = len(matching) - len(with_qty)

    if not with_qty:
        word = _plural_ru(len(matching), "запись", "записи", "записей")
        return (
            f"За {label} нашёл {len(matching)} {word} «{keyword}», но ни в одной "
            f"не указано количество (старые записи до этой фичи, либо количество "
            f"не удалось распознать при вводе) — точно посчитать не могу."
        )

    by_unit: dict[str, float] = {}
    for r in with_qty:
        unit = str(r.get("Единица", "")).strip() or "шт"
        by_unit[unit] = by_unit.get(unit, 0) + to_float(r["Количество"])

    parts = [f"{amount:g} {unit}" for unit, amount in by_unit.items()]
    result = f"За {label} «{keyword}»: " + ", ".join(parts) + "."

    if without_qty:
        word = _plural_ru(without_qty, "запись", "записи", "записей")
        result += f" (ещё {without_qty} {word} без указанного количества — не учтены)"

    return result


def _guess_auto_keyword(text: str) -> str | None:
    """Детерминированный фоллбэк на СЫРОМ тексте вопроса, когда LLM не
    вернула item (ненадёжна для глагольных формулировок вроде "заправлял
    опель?" — там нет существительного вообще). Переиспользует те же
    основы, что auto_expense.py применяет при КАТЕГОРИЗАЦИИ трат — так
    вопрос и запись классифицируются одинаково, независимо от LLM."""
    lowered = f" {text.lower()} "
    if any(kw in lowered for kw in auto_expense.FUEL_KEYWORDS):
        return "бензин"
    if " то " in lowered or "техосмотр" in lowered:
        return "ТО"
    for kw in auto_expense.REPAIR_KEYWORDS + auto_expense.MAINTENANCE_KEYWORDS:
        stripped = kw.strip()
        if stripped and stripped in lowered:
            return stripped
    return None


async def _answer_last_date(user_id: int, account: dict | None, car_name: str | None,
                            category: str | None, item: str | None,
                            question_text: str = "") -> str:
    """Ищет дату САМОГО ПОСЛЕДНЕГО подходящего события за всю историю.
    car_name задан -> ищем в листе Авто (масло/ТО/заправка и т.п. по
    конкретной машине), иначе -> в общих Транзакциях по товару/категории."""
    keyword = item or category

    if car_name:
        if not account:
            return "Google Drive не подключён — пройди заново /start."
        if not keyword:
            keyword = _guess_auto_keyword(question_text)
        try:
            event = await cars.get_last_auto_event(account, car_name, keyword)
        except Exception:
            return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."
        if event is None:
            subject = f" «{keyword}»" if keyword else ""
            return f"Не нашёл записей{subject} по «{car_name}»."
        if keyword:
            return f"Последний раз {keyword} на «{car_name}»: {_format_date(event['Дата'])}."
        # Не разобрал, ЧТО именно спрашивают про машину (item пустой) —
        # не выдаём это за ответ по существу молча: показываем, что реально
        # нашли (последнее событие ЛЮБОГО типа), и просим уточнить.
        return (
            f"Не понял, что именно спрашиваешь про «{car_name}» — вот "
            f"последняя запись по машине вообще: {event['Тип']} "
            f"({event['Описание']}), {_format_date(event['Дата'])}. "
            f"Уточни конкретнее, например «когда меняли масло на {car_name}?»."
        )

    if not keyword:
        return "Не понял, про что именно спрашиваешь — назови товар или категорию."

    try:
        rows = await get_transactions_in_range(user_id, None, None)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    # Уже отсортированы по дате desc (get_transactions_in_range) — первое
    # совпадение и есть самое последнее.
    matching = [
        r for r in rows
        if _item_matches(keyword, str(r.get("Комментарий", ""))) or r["Категория"] == keyword
    ]
    if not matching:
        return f"Не нашёл трат «{keyword}»."
    return f"Последний раз «{keyword}»: {_format_date(matching[0]['Дата и время'])}."


def _item_matches(item: str, text: str) -> bool:
    """Подстрочное совпадение с запасом на падежные окончания ("мороженое"
    в вопросе должно найти "мороженых" в описании траты) — берём основу
    слова (~70% длины, минимум 3 символа), а не всё слово целиком. Та же
    идея, что уже применяли для распознавания жидкостей в fluid_tracker.py."""
    item_lower = item.lower().strip()
    text_lower = text.lower()
    if item_lower in text_lower:
        return True
    stem_len = max(3, int(len(item_lower) * 0.7))
    return item_lower[:stem_len] in text_lower


async def _answer_money(user_id: int, intent: str, category: str | None, item: str | None,
                        since, until, label: str, currency: str) -> str:
    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    tx_type = "income" if intent == "income" else "expense"
    filtered = [r for r in rows if r["Тип"] == tx_type]

    # "item" (конкретный товар) приоритетнее "category" — если товар назван,
    # ищем именно его в описании траты, а не суммируем всю угаданную
    # категорию целиком (иначе "сколько на мороженое" отвечало бы суммой
    # по всей категории "Продукты", как это было до фикса).
    if item:
        filtered = [r for r in filtered if _item_matches(item, str(r.get("Комментарий", "")))]
        subject = f" на «{item}»"
    elif category:
        filtered = [r for r in filtered if r["Категория"] == category]
        subject = f" на «{category}»"
    else:
        subject = ""

    total = sum(to_float(r["Сумма"]) for r in filtered)
    verb = "доход" if intent == "income" else "расход"
    cat_part = subject

    if not filtered:
        return f"За {label}{cat_part} {verb}ов не нашёл."
    return f"За {label}{cat_part}: {verb} {total:g} {currency} ({len(filtered)} записей)."


async def _answer_mileage(account: dict | None, car_name: str | None,
                          since, until, label: str) -> str:
    if not account:
        return "Google Drive не подключён — пройди заново /start."
    if not car_name:
        return "Не понял, про какую машину вопрос — назови её явно."

    try:
        distance = await cars.get_mileage_distance(account, car_name, since, until)
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    if distance is None:
        return f"За {label} нет данных о пробеге «{car_name}»."
    return f"За {label} «{car_name}» проехала {distance:g} км."
