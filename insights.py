"""
insights.py
v1.7 - чат-вопросы по уже накопленным данным ("сколько потратил на корм в
июне?", "какой пробег у матиза за август?", "когда менял масло на опеле?",
"сколько мороженого я купил?", "сколько раз я покупал мороженое?", "средний
чек в кафе?", "на что я больше всего трачу в продуктах?", "трачу больше,
чем в прошлом месяце?"), плюс уточняющие вопросы без повтора темы ("Сколько
на сахар в июле?" -> "А в августе?" — см. _get_previous_question).

Архитектура: LLM (groq_client.parse_question) только РАЗБИРАЕТ вопрос в
структурированный запрос — категорию/машину/период. Сам ответ считает наш
код по данным из Sheets, без всякой фантазии модели в цифрах. Единственное
исключение — breakdown: там ЕЩЁ ОДИН LLM-вызов (cluster_items) группирует
похожие описания трат в товары, но суммы по группам всё равно считает код.

Разрешение года для месяца без явного года — правило "самый недавний
прошедший такой месяц": если месяц уже был в этом году — берём этот год,
если ещё не наступил — прошлый год. Явно названный год всегда побеждает.

Все обращения к db.* (Supabase) и к groq_client.*, которые сами по себе
синхронные, обёрнуты в asyncio.to_thread — без этого они блокировали бы
event loop бота целиком на время запроса (тормозило бы ВСЕХ пользователей
разом, не только того, кто спросил).

Changelog:
- v1.7: _get_previous_question/_remember_question — память последнего
        разобранного вопроса в Redis, 10 минут (QUESTION_MEMORY_TTL_SECONDS).
        Запоминаются только УСПЕШНО разобранные вопросы (intent != unknown,
        parsed is not None) — не подставляем мусорный контекст под следующий
        вопрос.
- v1.6: три новых способа спросить про деньги.
        (1) intent=average ("средний чек в кафе?") — сумма/число_покупок за
        период, переиспользует новый _compute_money.
        (2) intent=breakdown ("на что я больше всего трачу в продуктах?")
        — разбивка ОДНОЙ категории на товары через groq_client.cluster_items
        + группировку сумм в коде.
        (3) compare_previous ("трачу больше, чем в прошлом месяце?") —
        применяется поверх spending/income, считает текущий И предыдущий
        период (resolve_previous_period) и показывает разницу/%. all_time
        сравнивать не с чем — отдельное сообщение вместо тихой ошибки.
        _answer_money отрефакторен на общее ядро _compute_money (сумма,
        число_записей, ошибка) — average/comparison его переиспользуют.
- v1.5: intent=count ("сколько раз я покупал мороженое?") — считает
        количество ЗАПИСЕЙ (транзакций), не сумму денег (spending) и не
        сумму поля Количество (quantity, "сколько мороженого"). Проще всех
        трёх — не зависит от того, распознано ли физическое количество в
        принципе, каждая подходящая запись = одна покупка.
- v1.4: _answer_last_date получила _guess_auto_keyword() — детерминированный
        фоллбэк на СЫРОМ тексте вопроса (те же основы, что
        auto_expense.FUEL_KEYWORDS/ACTION_KEYWORDS/PARTS_KEYWORDS
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
from datetime import datetime, timedelta, timezone
from calendar import monthrange
import asyncio
import json
import logging
import re

import redis.asyncio as redis_asyncio

import supabase_client as db
import cars
import auto_expense
import groq_client
from config import REDIS_URL
from report import period_start
from sheets_transactions import get_transactions_in_range, to_float, NoGoogleAccount
from parser import extract_quantity

# Память последнего РАЗОБРАННОГО вопроса пользователя — для уточняющих
# вопросов без повтора темы ("Сколько на сахар в июле?" -> "А в августе?").
# Тот же клиент/паттерн, что уже использует narrative_report.py.
_redis = redis_asyncio.from_url(REDIS_URL)
QUESTION_MEMORY_TTL_SECONDS = 600  # 10 минут


async def _get_previous_question(user_id: int) -> dict | None:
    try:
        raw = await _redis.get(f"last_question:{user_id}")
    except Exception:
        logging.exception("_get_previous_question: Redis read failed")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def _remember_question(user_id: int, text: str, parsed: dict) -> None:
    try:
        await _redis.set(
            f"last_question:{user_id}",
            json.dumps({"text": text, "parsed": parsed}, ensure_ascii=False),
            ex=QUESTION_MEMORY_TTL_SECONDS,
        )
    except Exception:
        logging.exception("_remember_question: Redis write failed")


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


_CALENDAR_MONTH_MARKERS = (
    "в этом месяце", "за этот месяц", "этот месяц", "в этом календарном",
    "за календарный месяц",
)


def _coerce_period_type(text: str, period_type: str, month: int | None) -> str:
    """«В этом месяце» — календарь 1…конец, не отрезок от дня зарплаты."""
    lowered = (text or "").lower()
    if any(m in lowered for m in _CALENDAR_MONTH_MARKERS):
        return "calendar_month"
    if period_type == "this_month":
        return "calendar_month"
    return period_type or "current_period"


def _format_period_label(label: str, since, until) -> str:
    if not since and not until:
        return label
    end = until or datetime.now(timezone.utc)
    if since is None:
        return f"{label} (по {_format_short_date(end)})"
    return f"{label} ({_format_short_date(since)}–{_format_short_date(end)})"


def _format_short_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d.%m")
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m")
    except ValueError:
        return str(value)


def resolve_period(period_type: str, month: int | None, year: int | None,
                   month_start_day: int) -> tuple[datetime | None, datetime | None, str]:
    """Возвращает (since, until, human_label). until=None — открытый диапазон
    (до сейчас)."""
    if period_type in ("specific_month", "calendar_month", "this_month"):
        now = datetime.now(timezone.utc)
        actual_month = month or now.month
        actual_year = year or (resolve_year_for_month(actual_month, year) if month else now.year)
        if period_type in ("calendar_month", "this_month") and not month:
            actual_month, actual_year = now.month, now.year
        elif month:
            actual_year = resolve_year_for_month(month, year)
            actual_month = month
        since, until = month_bounds(actual_year, actual_month)
        label = f"{MONTH_NAMES.get(actual_month, actual_month)} {actual_year}"
        return since, until, label

    if period_type == "current_period":
        since = period_start(month_start_day)
        return since, None, "текущий период"

    # all_time или что-то неожиданное — без ограничений
    return None, None, "всё время"


def resolve_previous_period(period_type: str, month: int | None, year: int | None,
                            month_start_day: int) -> tuple[datetime | None, datetime | None, str | None]:
    """Период, ПРЕДШЕСТВУЮЩИЙ тому, что вернул бы resolve_period с теми же
    аргументами — для intent'ов с compare_previous. Третий элемент — None,
    если сравнивать не с чем (all_time), вызывающий код должен это
    проверить и не звать _answer_comparison в этом случае."""
    if period_type in ("specific_month", "calendar_month", "this_month"):
        now = datetime.now(timezone.utc)
        actual_month = month or now.month
        actual_year = year or now.year
        if month:
            actual_year = resolve_year_for_month(month, year)
            actual_month = month
        elif period_type not in ("calendar_month", "this_month"):
            return None, None, None
        prev_month = actual_month - 1 or 12
        prev_year = actual_year if actual_month > 1 else actual_year - 1
        since, until = month_bounds(prev_year, prev_month)
        label = f"{MONTH_NAMES.get(prev_month, prev_month)} {prev_year}"
        return since, until, label

    if period_type == "current_period":
        since = period_start(month_start_day)
        prev_month = since.month - 1 or 12
        prev_year = since.year if since.month > 1 else since.year - 1
        prev_since = since.replace(year=prev_year, month=prev_month)
        prev_until = since - timedelta(seconds=1)
        return prev_since, prev_until, "прошлый период"

    return None, None, None


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

    previous = await _get_previous_question(user_id)
    try:
        parsed = await asyncio.to_thread(
            groq_client.parse_question, text, categories, car_names, previous
        )
    except groq_client.GroqUnavailable:
        # Отдельно от "не понял вопрос" ниже: здесь Groq вообще не ответил
        # (сеть/лимиты/сбой сервиса) — дело не в формулировке вопроса, так
        # что просить переформулировать было бы нечестно.
        return (
            "Сейчас не могу разобрать вопрос — сервис Groq недоступен. "
            "Запиши трату или открой /report — это считается без него."
        )
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

    # Запоминаем только УСПЕШНО разобранные вопросы — на 10 минут, для
    # следующего уточняющего ("а в августе?"). Неудачный разбор/unknown не
    # запоминаем: подставлять мусорный контекст под следующий вопрос хуже,
    # чем не подставлять никакой.
    await _remember_question(user_id, text, parsed)

    period_type = _coerce_period_type(text, parsed.get("period_type", "current_period"),
                                      parsed.get("month"))

    since, until, label = resolve_period(
        period_type,
        parsed.get("month"), parsed.get("year"),
        user.get("month_start", 1),
    )
    label = _format_period_label(label, since, until)

    # last_date без названного месяца — вся история. Месяц/«в этом месяце»
    # названы — уважаем диапазон, а не игнорируем его.
    if intent == "last_date":
        use_range = period_type in ("specific_month", "calendar_month", "this_month") or parsed.get("month")
        return await _answer_last_date(
            user_id, account, parsed.get("car_name"),
            parsed.get("category"), parsed.get("item"), text,
            since=since if use_range else None,
            until=until if use_range else None,
            label=label if use_range else None,
        )

    if intent == "mileage":
        return await _answer_mileage(account, parsed.get("car_name"), since, until, label)

    if intent == "quantity":
        return await _answer_quantity(user_id, parsed.get("category"), parsed.get("item"),
                                      since, until, label)

    if intent == "count":
        return await _answer_count(user_id, parsed.get("category"), parsed.get("item"),
                                   since, until, label)

    if intent == "breakdown":
        return await _answer_breakdown(user_id, parsed.get("category"), since, until, label,
                                       user.get("currency", "RUB"))

    # compare_previous применим только к деньгам (spending/income) — вопрос
    # про количество/среднее "больше, чем в прошлом" не просили, не гадаем.
    if intent in ("spending", "income") and parsed.get("compare_previous"):
        prev_since, prev_until, prev_label = resolve_previous_period(
            period_type,
            parsed.get("month"), parsed.get("year"),
            user.get("month_start", 1),
        )
        if prev_label:
            prev_label = _format_period_label(prev_label, prev_since, prev_until)
        if prev_label is None:
            return "Не с чем сравнивать — «за всё время» не имеет предыдущего периода. Уточни конкретный месяц или период."
        return await _answer_comparison(user_id, intent, parsed.get("category"), parsed.get("item"),
                                        since, until, label, prev_since, prev_until, prev_label,
                                        user.get("currency", "RUB"))

    if intent == "average":
        return await _answer_average(user_id, parsed.get("category"), parsed.get("item"),
                                     since, until, label, user.get("currency", "RUB"))

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
    matching = _filter_by_topic(expense_rows, category, item)

    if not matching:
        return f"Не нашёл покупок «{keyword}» за {label}."

    parsed_rows: list[tuple[float, str]] = []
    without_qty = 0
    for r in matching:
        qty, unit = _quantity_of_row(r)
        if qty is None:
            without_qty += 1
        else:
            parsed_rows.append((qty, unit))

    if not parsed_rows:
        word = _plural_ru(len(matching), "запись", "записи", "записей")
        return (
            f"За {label} нашёл {len(matching)} {word} «{keyword}», но ни в одной "
            f"не указано количество (старые записи до этой фичи, либо количество "
            f"не удалось распознать при вводе) — точно посчитать не могу."
        )

    by_unit: dict[str, float] = {}
    for qty, unit in parsed_rows:
        by_unit[unit] = by_unit.get(unit, 0) + qty

    parts = [f"{amount:g} {unit}" for unit, amount in by_unit.items()]
    result = f"За {label} «{keyword}»: " + ", ".join(parts) + "."

    if without_qty:
        word = _plural_ru(without_qty, "запись", "записи", "записей")
        result += f" (ещё {without_qty} {word} без указанного количества — не учтены)"

    return result


async def _answer_count(user_id: int, category: str | None, item: str | None,
                        since, until, label: str) -> str:
    """"Сколько раз я покупал мороженое?" — считает КОЛИЧЕСТВО ЗАПИСЕЙ
    (транзакций), а не сумму денег (intent=spending) и не сумму поля
    Количество (intent=quantity, "сколько мороженого"). В отличие от
    quantity, тут Количество/Единица вообще не нужны — каждая подходящая
    запись == одна покупка, независимо от того, было ли в ней распознано
    физическое количество."""
    keyword = item or category
    if not keyword:
        return "Не понял, что именно считать — назови товар или категорию."

    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    expense_rows = [r for r in rows if r["Тип"] == "expense"]
    matching = _filter_by_topic(expense_rows, category, item)

    if not matching:
        return f"Не нашёл покупок «{keyword}» за {label}."

    word = _plural_ru(len(matching), "раз", "раза", "раз")
    return f"За {label} «{keyword}»: {len(matching)} {word}."


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
    for kw in auto_expense.ACTION_KEYWORDS + auto_expense.PARTS_KEYWORDS:
        stripped = kw.strip()
        if stripped and stripped in lowered:
            return stripped
    return None


_REPAIR_QUESTION = ("менял", "меняли", "менялась", "менялись", "замен", "ремонт")


def _looks_like_repair_question(text: str) -> bool:
    lowered = (text or "").lower()
    return any(w in lowered for w in _REPAIR_QUESTION)


def _looks_like_repair_row(comment: str, type_or_cat: str) -> bool:
    blob = f"{comment} {type_or_cat}".lower()
    return "замен" in blob or "ремонт" in blob


def _mentions_car(text: str, car_name: str) -> bool:
    if not text or not car_name:
        return False
    return cars.match_car_name(text, [{"Машина": car_name}]) == car_name


def _collect_car_events(car_name: str, keyword: str | None, auto_event: dict | None,
                        auto_count: int, tx_rows: list, question_text: str) -> list[dict]:
    """Лист Авто + Транзакции: покупка запчасти и «замена» часто живут
    в разных листах. «Когда менялись» должно видеть оба и предпочесть замену."""
    events: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(date_val, text, kind):
        key = (str(date_val)[:10], (text or "").strip().lower())
        if not date_val or key in seen:
            return
        seen.add(key)
        events.append({"date": date_val, "text": text or "", "kind": kind or ""})

    if auto_event:
        add(auto_event.get("Дата"), auto_event.get("Описание"), auto_event.get("Тип"))

    for r in tx_rows or []:
        comment = str(r.get("Комментарий") or "")
        cat = str(r.get("Категория") or "")
        if keyword and not (
            _item_matches(keyword, comment) or _category_matches(keyword, cat)
        ):
            continue
        if not _mentions_car(comment, car_name) and not _mentions_car(cat, car_name):
            # авто-категория без имени машины в комментарии — всё равно берём,
            # если категория уже Топливо/Ремонт/Запчасти и keyword совпал
            if cat not in (
                auto_expense.TYPE_FUEL, auto_expense.TYPE_REPAIR,
                auto_expense.TYPE_PARTS, auto_expense.TYPE_OTHER,
            ):
                continue
        add(r.get("Дата и время"), comment, cat)

    events.sort(key=lambda e: str(e["date"]), reverse=True)
    if keyword and _looks_like_repair_question(question_text):
        repairs = [e for e in events if _looks_like_repair_row(e["text"], e["kind"])]
        if repairs:
            return repairs
    return events


async def _answer_last_date(user_id: int, account: dict | None, car_name: str | None,
                            category: str | None, item: str | None,
                            question_text: str = "",
                            since=None, until=None, label: str | None = None) -> str:
    """Ищет дату САМОГО ПОСЛЕДНЕГО подходящего события.
    Месяц не назван — вся история. Назван — только этот диапазон.
    car_name задан -> лист Авто, иначе общие Транзакции."""
    keyword = item or category
    when = f" за {label}" if label else ""

    if car_name:
        if not account:
            return "Google Drive не подключён — пройди заново /start."
        if not keyword:
            keyword = _guess_auto_keyword(question_text)
        try:
            event, count = await cars.get_last_auto_event(
                account, car_name, keyword, since=since, until=until
            )
            tx_rows = await get_transactions_in_range(user_id, since, until)
        except NoGoogleAccount:
            return "Google Drive не подключён — пройди заново /start."
        except Exception:
            return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

        events = _collect_car_events(
            car_name, keyword, event, count, tx_rows, question_text
        )
        if not events:
            subject = f" «{keyword}»" if keyword else ""
            return f"Не нашёл записей{subject} по «{car_name}»{when}."
        if keyword:
            latest = events[0]
            date_part = (
                f"Последний раз {keyword} на «{car_name}»{when}: "
                f"{_format_date(latest['date'])}"
            )
            if latest.get("kind"):
                date_part += f" ({latest['kind']})"
            if len(events) > 1:
                word = _plural_ru(len(events), "раз", "раза", "раз")
                return f"{date_part} (всего {len(events)} {word})."
            return f"{date_part}."
        latest = events[0]
        return (
            f"Не понял, что именно спрашиваешь про «{car_name}» — вот "
            f"последняя запись по машине вообще: {latest.get('kind') or 'запись'} "
            f"({latest.get('text') or ''}), {_format_date(latest['date'])}. "
            f"Уточни конкретнее, например «когда меняли масло на {car_name}?»."
        )

    if not keyword:
        return "Не понял, про что именно спрашиваешь — назови товар или категорию."

    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    # Уже отсортированы по дате desc (get_transactions_in_range) — первое
    # совпадение и есть самое последнее.
    matching = _filter_by_topic(rows, category, item)
    if not matching:
        matching = [
            r for r in rows
            if _item_matches(keyword, str(r.get("Комментарий", "")))
            or _category_matches(keyword, str(r.get("Категория", "")))
        ]
    if not matching:
        return f"Не нашёл трат «{keyword}»{when}."
    date_part = f"Последний раз «{keyword}»{when}: {_format_date(matching[0]['Дата и время'])}"
    if len(matching) > 1:
        word = _plural_ru(len(matching), "раз", "раза", "раз")
        return f"{date_part} (всего {len(matching)} {word})."
    return f"{date_part}."


_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_SHORT_ENDINGS = {
    "", "а", "я", "у", "ю", "е", "о", "и", "ы", "й",
    "ой", "ое", "ая", "ые", "ие", "ых", "их", "ам", "ям",
    "ом", "ем", "ов", "ев", "ах", "ях", "ами", "ями",
}


def _token_matches_word(token: str, word: str) -> bool:
    """Целое слово или основа с коротким окончанием. «рис» ≠ «рикотты»,
    «кофе» ≠ «кофейного», «мороженое» = «мороженых», «столовая» = «столовой»."""
    if word == token:
        return True
    if len(token) < 4:
        return False
    if word.startswith(token) and word[len(token):] in _SHORT_ENDINGS:
        return True
    if len(token) >= 5:
        stem_len = max(4, int(len(token) * 0.7))
        stem = token[:stem_len]
        if word.startswith(stem) and len(word) - len(stem) <= 4:
            return True
    return False


def _item_matches(item: str, text: str) -> bool:
    item_lower = item.lower().strip()
    text_lower = text.lower()
    if not item_lower:
        return False
    tokens = _WORD_RE.findall(item_lower)
    if not tokens:
        return False
    words = _WORD_RE.findall(text_lower)
    if not words:
        return False
    return all(any(_token_matches_word(tok, w) for w in words) for tok in tokens)


def _resolve_category(query: str | None, known: list[str]) -> str | None:
    """LLM часто отрезает составное имя: вопрос «за столовую», в таблице
    категория «Столовая, кафе». Точное равенство тогда пустое, хотя данные
    есть. Сначала точное имя без регистра, потом кусок между запятыми,
    потом та же основа слова, что у товаров в комментарии."""
    if not query:
        return None
    q = query.lower().strip()
    for name in known:
        if name.lower().strip() == q:
            return name
    hits = [name for name in known if _category_matches(q, name)]
    if not hits:
        return None
    return max(hits, key=len)


def _category_matches(query: str, category_name: str) -> bool:
    name = category_name.lower().strip()
    if not query or not name:
        return False
    if _item_matches(query, name) or _item_matches(name, query):
        return True
    for part in name.replace("/", ",").split(","):
        part = part.strip()
        if part and (_item_matches(query, part) or _item_matches(part, query)):
            return True
    return False


def _filter_by_topic(rows: list, category: str | None, item: str | None) -> list:
    """Товар в комментарии важнее категории. Если категории как точного
    имени нет — не сдаёмся: ищем слово в имени категории и в комментарии
    («столовая» → «Столовая, кафе» и «Обед в столовой»)."""
    if item:
        return [r for r in rows if _item_matches(item, str(r.get("Комментарий", "")))]
    if not category:
        return rows
    known = []
    seen: set[str] = set()
    for r in rows:
        name = str(r.get("Категория") or "")
        if name and name not in seen:
            seen.add(name)
            known.append(name)
    resolved = _resolve_category(category, known)
    if resolved:
        return [r for r in rows if r.get("Категория") == resolved]
    return [
        r for r in rows
        if _item_matches(category, str(r.get("Комментарий", "")))
        or _category_matches(category, str(r.get("Категория", "")))
    ]


def _quantity_of_row(row: dict) -> tuple[float | None, str]:
    """Сначала колонка Количество, иначе тот же разбор, что при записи,
    по комментарию («3 мороженых»). Пустой комментарий без цифры — None."""
    raw = str(row.get("Количество", "")).strip()
    if raw:
        unit = str(row.get("Единица", "")).strip() or "шт"
        return to_float(raw), unit
    comment = str(row.get("Комментарий", "") or "")
    qty, unit, _rest = extract_quantity(comment)
    if qty is None:
        return None, "шт"
    return qty, unit or "шт"


def _money_subject(category: str | None, item: str | None) -> str:
    if item:
        return f" на «{item}»"
    if category:
        return f" на «{category}»"
    return ""


async def _compute_money(user_id: int, intent: str, category: str | None, item: str | None,
                         since, until) -> tuple[float | None, int, str | None]:
    """Общее ядро для _answer_money/_answer_average/_answer_comparison —
    считает (сумма, число_записей, ошибка). ошибка не None, если что-то
    пошло не так на уровне доступа к Sheets — тогда сумму/число игнорировать
    и просто вернуть ошибку пользователю как есть."""
    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return None, 0, "Google Drive не подключён — пройди заново /start."
    except Exception:
        return None, 0, "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    tx_type = "income" if intent == "income" else "expense"
    filtered = [r for r in rows if r["Тип"] == tx_type]

    # "item" (конкретный товар) приоритетнее "category" — если товар назван,
    # ищем именно его в описании траты, а не суммируем всю угаданную
    # категорию целиком (иначе "сколько на мороженое" отвечало бы суммой
    # по всей категории "Продукты", как это было до фикса).
    filtered = _filter_by_topic(filtered, category, item)

    total = sum(to_float(r["Сумма"]) for r in filtered)
    return total, len(filtered), None


async def _answer_money(user_id: int, intent: str, category: str | None, item: str | None,
                        since, until, label: str, currency: str) -> str:
    total, count, error = await _compute_money(user_id, intent, category, item, since, until)
    if error:
        return error

    verb = "доход" if intent == "income" else "расход"
    subject = _money_subject(category, item)

    if count == 0:
        return f"За {label}{subject} {verb}ов не нашёл."
    return f"За {label}{subject}: {verb} {total:g} {currency} ({count} записей)."


async def _answer_average(user_id: int, category: str | None, item: str | None,
                          since, until, label: str, currency: str) -> str:
    """"Сколько в среднем трачу на кофе?" / "средний чек в кафе?" — сумма
    делённая на число ПОКУПОК за период (не на число дней — "средний чек"
    по смыслу это "средняя сумма ОДНОЙ покупки", не дневная трата)."""
    total, count, error = await _compute_money(user_id, "spending", category, item, since, until)
    if error:
        return error

    subject = _money_subject(category, item)
    if count == 0:
        return f"За {label}{subject} трат не нашёл — нечего усреднять."

    avg = total / count
    return (
        f"За {label}{subject}: в среднем {avg:g} {currency} за запись "
        f"({count} записей, всего {total:g} {currency})."
    )


async def _answer_comparison(user_id: int, intent: str, category: str | None, item: str | None,
                             since, until, label: str,
                             prev_since, prev_until, prev_label: str, currency: str) -> str:
    """"В этом месяце я трачу больше, чем в прошлом?" — тот же intent
    spending/income, но считаем ДВАЖДЫ (текущий и предыдущий период) и
    показываем разницу. all_time сравнивать не с чем — отдельная проверка
    в вызывающем коде (_answer_question_inner) до сюда не доходит."""
    cur_total, cur_count, error = await _compute_money(user_id, intent, category, item, since, until)
    if error:
        return error
    prev_total, prev_count, error = await _compute_money(
        user_id, intent, category, item, prev_since, prev_until
    )
    if error:
        return error

    verb = "доход" if intent == "income" else "расход"
    subject = _money_subject(category, item)
    diff = cur_total - prev_total

    if prev_total == 0:
        pct_text = "" if diff == 0 else " (в прошлом периоде записей не было)"
    else:
        pct = diff / prev_total * 100
        pct_text = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
    sign = "+" if diff >= 0 else ""

    return (
        f"{label}{subject}: {verb} {cur_total:g} {currency} ({cur_count} записей).\n"
        f"{prev_label}{subject}: {verb} {prev_total:g} {currency} ({prev_count} записей).\n"
        f"Разница: {sign}{diff:g} {currency}{pct_text}."
    )


async def _answer_breakdown(user_id: int, category: str | None,
                            since, until, label: str, currency: str) -> str:
    """"На что я больше всего трачу в продуктах?" — разбивка ОДНОЙ
    категории на товары. Комментарий — свободный текст ("молоко"/"молоко
    2.5%"/"молочко" — три разные строки), поэтому группировку по смыслу
    делает groq_client.cluster_items (отдельный LLM-вызов), а суммы и
    сортировку — наш код, как и везде."""
    if not category:
        return "Уточни категорию — разбивка по товарам считается ВНУТРИ одной категории."

    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    expense_rows = [r for r in rows if r["Тип"] == "expense"]
    matching = _filter_by_topic(expense_rows, category, None)
    if not matching:
        return f"Нет трат в категории «{category}» за {label}."

    comments = [str(r.get("Комментарий", "")).strip() or "без описания" for r in matching]
    unique_comments = sorted(set(comments))

    try:
        grouping = await asyncio.to_thread(groq_client.cluster_items, unique_comments)
    except Exception:
        logging.exception("_answer_breakdown: cluster_items failed")
        grouping = None
    if not grouping:
        # Фоллбэк — без группировки, по буквальному тексту комментария.
        # Хуже (не сольёт "молоко"/"молоко 2.5%"), но не роняет ответ.
        grouping = {c: c for c in unique_comments}

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row, comment in zip(matching, comments):
        group = grouping.get(comment, comment)
        totals[group] = totals.get(group, 0) + to_float(row["Сумма"])
        counts[group] = counts.get(group, 0) + 1

    top = sorted(totals.items(), key=lambda kv: -kv[1])[:8]
    lines = [f"Разбивка «{category}» за {label}:"]
    for name, total in top:
        cnt = counts[name]
        word = _plural_ru(cnt, "раз", "раза", "раз")
        lines.append(f"• {name}: {total:g} {currency} ({cnt} {word})")

    if len(unique_comments) > groq_client.MAX_CLUSTER_ITEMS:
        lines.append(
            f"\n(⚠️ уникальных описаний больше {groq_client.MAX_CLUSTER_ITEMS} — "
            f"часть могла попасть в группу «как есть», без объединения)"
        )

    return "\n".join(lines)


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
