"""
period_utils.py
v1.0 - общая логика дат/периодов, вынесенная из report.py и insights.py в
отдельный низкоуровневый модуль, от которого зависят оба (а не друг от
друга) — иначе получается циклический импорт: report.py импортирует
narrative_report.py, тот импортирует insights.py, а insights.py импортирует
period_start из report.py. Здесь нет импортов из других модулей проекта —
только datetime/calendar, поэтому от period_utils.py можно зависеть кому
угодно без риска цикла.
"""
from datetime import datetime, timezone
from calendar import monthrange

MONTH_NAMES = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

# Родительный падеж — для дат вида "12 июня 2026" (не "12 июнь"). Отдельно
# от MONTH_NAMES (именительный, для меток периода "за июнь 2026" — там
# нужен именно именительный).
MONTH_NAMES_GENITIVE = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def period_start(month_start_day: int) -> datetime:
    """Начало ТЕКУЩЕГО отчётного периода пользователя (день месяца
    настраивается в /settings, не обязательно 1-е число)."""
    now = datetime.now(timezone.utc)
    if now.day >= month_start_day:
        start = now.replace(day=month_start_day, hour=0, minute=0, second=0, microsecond=0)
    else:
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        start = now.replace(
            year=prev_year, month=prev_month, day=month_start_day,
            hour=0, minute=0, second=0, microsecond=0,
        )
    return start


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

    return None, None, "всё время"


def previous_period_bounds(since: datetime, until, period_type: str) -> tuple[datetime, datetime]:
    """Границы периода СРАЗУ ПЕРЕД primary (для intent=compare_periods и
    narrative_report.py). Для конкретного месяца — точно предыдущий
    календарный месяц (не "минус 30 дней" — иначе съезжает на пару дней из-
    за разной длины месяцев); month/year берём прямо из `since`, т.к. since
    уже разрешённая дата начала того самого месяца."""
    if period_type == "specific_month":
        month, year = since.month, since.year
        prev_month = month - 1 or 12
        prev_year = year if month > 1 else year - 1
        return month_bounds(prev_year, prev_month)

    effective_until = until or datetime.now(timezone.utc)
    length = effective_until - since
    return since - length, since


def format_date_human(iso_value) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return str(iso_value)
    return f"{dt.day} {MONTH_NAMES_GENITIVE.get(dt.month, dt.month)} {dt.year}"
