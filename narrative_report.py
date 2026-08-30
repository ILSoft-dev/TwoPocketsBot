"""
narrative_report.py
v1.1 - связный текстовый отчёт со сравнением периода с самим собой ("эта
категория выросла на 30%"), а не generic-советами про экономию. Общий
код для /report (текущий период vs предыдущий) и годового отчёта 1 января
(прошлый год vs год до него).

Порог "заметного" изменения и минимальная сумма для упоминания — чтобы не
шуметь про случайные мелкие траты, где любое небольшое отклонение даёт
большой % (например, "Хозтовары" выросли с 3 до 9 BYN — формально +200%,
но абсолютно незначимо).

Changelog:
- v1.1: числа по-прежнему считает только Python (_totals_by_category,
        _format_comparison — детерминированно, без LLM). Сверху добавлена
        необязательная обёртка в связный текст через
        groq_client.narrate_period_comparison — модели передаются уже
        готовые цифры, сама она ничего не считает и не может придумать то,
        чего нет (см. docstring этой функции в groq_client.py). Если Groq
        недоступен/ошибся — тихий откат на прежний список буллетов
        (_format_comparison), а не пустой ответ. Также: раньше файл не был
        нигде подключён (report.py/main.py его не импортировали) и вдобавок
        ссылался на несуществующую get_transactions_in_range_for_account —
        оба момента исправлены здесь и в sheets_transactions.py.
"""
import asyncio
import logging
from datetime import datetime, timezone

import redis.asyncio as redis_asyncio

import supabase_client as db
import groq_client
from config import REDIS_URL
from sheets_transactions import (
    get_transactions_in_range,
    get_transactions_in_range_for_account,
    to_float,
    NoGoogleAccount,
)
from period_utils import previous_period_bounds

logger = logging.getLogger(__name__)

SIGNIFICANT_CHANGE_PCT = 20
MIN_AMOUNT_FOR_NOTE = 10
MAX_HIGHLIGHTS = 5

_redis = redis_asyncio.from_url(REDIS_URL)
_ANNUAL_GUARD_TTL_SECONDS = 40 * 24 * 60 * 60  # 40 дней — с запасом до следующего 1 января


def _totals_by_category(rows: list[dict], tx_type: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for r in rows:
        if r["Тип"] != tx_type:
            continue
        cat = r["Категория"]
        totals[cat] = totals.get(cat, 0) + to_float(r["Сумма"])
    return totals


def _format_comparison(current: dict[str, float], previous: dict[str, float],
                       currency: str) -> list[str]:
    ARROW = "\u2192"
    changes = []
    for cat in set(current) | set(previous):
        cur = current.get(cat, 0)
        prev = previous.get(cat, 0)
        if max(cur, prev) < MIN_AMOUNT_FOR_NOTE:
            continue
        if prev == 0:
            if cur > 0:
                changes.append((cat, cur, prev, None))
            continue
        pct = (cur - prev) / prev * 100
        if abs(pct) >= SIGNIFICANT_CHANGE_PCT:
            changes.append((cat, cur, prev, pct))

    changes.sort(key=lambda c: abs(c[3]) if c[3] is not None else 999, reverse=True)

    lines = []
    for cat, cur, prev, pct in changes[:MAX_HIGHLIGHTS]:
        if pct is None:
            lines.append(f"\u2022 \u00ab{cat}\u00bb \u2014 новая категория трат: {cur:g} {currency}.")
        else:
            direction = "выросла" if pct > 0 else "снизилась"
            lines.append(
                f"\u2022 \u00ab{cat}\u00bb {direction} на {abs(pct):.0f}% "
                f"({prev:g} {ARROW} {cur:g} {currency})."
            )
    return lines


async def build_narrative_for_user(user_id: int, since: datetime, until, label: str,
                                   currency: str, period_type: str,
                                   heading: str = "\U0001F4C8 Сравнение с прошлым периодом:") -> str:
    try:
        prev_since, prev_until = previous_period_bounds(since, until, period_type)
        current_rows = await get_transactions_in_range(user_id, since, until)
        previous_rows = await get_transactions_in_range(user_id, prev_since, prev_until)
    except NoGoogleAccount:
        return ""
    except Exception:
        logger.exception("build_narrative_for_user: failed to fetch rows")
        return ""

    if not previous_rows:
        # Сравнивать не с чем — либо это самый первый отчётный период у
        # аккаунта, либо в прошлом периоде вообще не было записей. И то, и
        # другое — не повод объявлять "новой категорией" вообще всё подряд
        # (см. _format_comparison: prev==0 трактуется как "новая категория").
        # По сути это и даёт эффект "интересное появляется через пару
        # периодов, когда есть с чем сравнивать" — без отдельного счётчика
        # месяцев, само собой из наличия данных.
        return ""

    current_expense = _totals_by_category(current_rows, "expense")
    previous_expense = _totals_by_category(previous_rows, "expense")
    lines = _format_comparison(current_expense, previous_expense, currency)
    if not lines:
        return ""

    fallback_text = heading + "\n" + "\n".join(lines)

    current_income = sum(_totals_by_category(current_rows, "income").values())
    previous_income = sum(_totals_by_category(previous_rows, "income").values())
    current_total_expense = sum(current_expense.values())
    previous_total_expense = sum(previous_expense.values())

    data_summary = (
        f"Период: {label}\n"
        f"Доход сейчас: {current_income:g} {currency} (было: {previous_income:g} {currency})\n"
        f"Расход сейчас: {current_total_expense:g} {currency} (было: {previous_total_expense:g} {currency})\n"
        f"Заметные изменения по категориям:\n" + "\n".join(lines)
    )
    try:
        return await asyncio.to_thread(groq_client.narrate_period_comparison, data_summary)
    except Exception:
        logger.warning("build_narrative_for_user: LLM narration failed, falling back to bullet list", exc_info=True)
        return fallback_text


# --------------------------------------------------------- годовой отчёт ----
def _is_january_first() -> bool:
    now = datetime.now(timezone.utc)
    return now.month == 1 and now.day == 1


async def _already_sent_this_year(user_id: int, year: int) -> bool:
    return bool(await _redis.get(f"annual_report_sent:{user_id}:{year}"))


async def _mark_sent_this_year(user_id: int, year: int) -> None:
    await _redis.set(
        f"annual_report_sent:{user_id}:{year}", "1", ex=_ANNUAL_GUARD_TTL_SECONDS,
    )


async def run_annual_report_sweep(bot) -> int:
    if not _is_january_first():
        return 0

    now = datetime.now(timezone.utc)
    finished_year = now.year - 1
    sent = 0

    for account in db.list_google_connected_users():
        try:
            if await _already_sent_this_year(account["id"], now.year):
                continue
        except Exception:
            pass

        since = datetime(finished_year, 1, 1, tzinfo=timezone.utc)
        until = datetime(finished_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        prev_since = datetime(finished_year - 1, 1, 1, tzinfo=timezone.utc)
        prev_until = datetime(finished_year - 1, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

        try:
            current_rows = await get_transactions_in_range_for_account(account, since, until)
            previous_rows = await get_transactions_in_range_for_account(account, prev_since, prev_until)
        except Exception:
            continue

        if not current_rows:
            continue

        owner = db.get_user_by_id(account["id"])
        currency = owner.get("currency", "RUB") if owner else "RUB"

        current = _totals_by_category(current_rows, "expense")
        previous = _totals_by_category(previous_rows, "expense")
        total_expense = sum(current.values())
        total_prev_expense = sum(previous.values())

        header = f"\U0001F386 Итоги {finished_year} года\n"
        fallback_lines = [f"Всего расходов: {total_expense:g} {currency}"]
        if total_prev_expense:
            delta_pct = (total_expense - total_prev_expense) / total_prev_expense * 100
            direction = "больше" if delta_pct > 0 else "меньше"
            fallback_lines.append(
                f"Это {direction} на {abs(delta_pct):.0f}%, чем в {finished_year - 1} "
                f"году ({total_prev_expense:g} {currency})."
            )
        comparison_lines = _format_comparison(current, previous, currency)
        if comparison_lines:
            fallback_lines.append("\nЗаметные изменения по категориям:")
            fallback_lines.extend(comparison_lines)
        fallback_body = "\n".join(fallback_lines)

        body = fallback_body
        if total_prev_expense:  # есть с чем сравнивать — пробуем LLM-нарратив
            data_summary = (
                f"Год: {finished_year} (предыдущий: {finished_year - 1})\n"
                f"Расход в {finished_year}: {total_expense:g} {currency} "
                f"(в {finished_year - 1}: {total_prev_expense:g} {currency})\n"
                f"Заметные изменения по категориям:\n" + "\n".join(comparison_lines or ["(нет заметных)"])
            )
            try:
                body = await asyncio.to_thread(groq_client.narrate_period_comparison, data_summary)
            except Exception:
                logger.warning("run_annual_report_sweep: LLM narration failed for account %s, using plain text",
                             account["id"], exc_info=True)
                body = fallback_body

        text = header + "\n" + body
        recipients = db.get_spreadsheet_recipients(account["id"])
        for tg_id in recipients:
            try:
                await bot.send_message(tg_id, text)
                sent += 1
            except Exception:
                pass

        try:
            await _mark_sent_this_year(account["id"], now.year)
        except Exception:
            pass

    return sent
