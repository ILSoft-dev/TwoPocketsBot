"""
report.py
v1.1 - /report: сводка за отчётный период + кнопки для перехода к прошлым
периодам (не только текущему).

Changelog:
- v1.1: period_start() (используется и в insights.py — НЕ переименовывать,
        НЕ менять сигнатуру) осталась как была. Добавлены period_bounds()/
        period_label() — то же самое, но periods_back шагов назад, с явной
        верхней границей для закрытых периодов. cmd_report теперь всегда
        рисует ряд кнопок (report_periods_keyboard) под текстом отчёта;
        нажатие редактирует то же сообщение под выбранный период —
        get_report_range() (sheets_transactions.py) вместо get_report(),
        чтобы не захватить данные уже следующего периода.
"""
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

import supabase_client as db
import sheets_transactions as tx
import tx_logic
import narrative_report
from keyboards import report_periods_keyboard

router = Router()

# Сколько периодов назад показывать кнопками (0 = текущий + это число - 1
# закрытых прошлых периодов). Не бесконечно — иначе ряд кнопок расползается
# и каждая лишняя история это ещё один потенциальный запрос к Sheets.
REPORT_PERIODS_SHOWN = 6


def period_start(month_start_day: int) -> datetime:
    now = datetime.now(timezone.utc)
    if now.day >= month_start_day:
        start = now.replace(day=month_start_day, hour=0, minute=0, second=0, microsecond=0)
    else:
        # период начался в предыдущем месяце
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        start = now.replace(
            year=prev_year, month=prev_month, day=month_start_day,
            hour=0, minute=0, second=0, microsecond=0,
        )
    return start


def _period_start_n(month_start_day: int, periods_back: int) -> datetime:
    """Начало периода, periods_back шагов назад от текущего (0 = сам
    текущий период, как period_start())."""
    since = period_start(month_start_day)
    for _ in range(periods_back):
        prev_month = since.month - 1 or 12
        prev_year = since.year if since.month > 1 else since.year - 1
        since = since.replace(year=prev_year, month=prev_month)
    return since


def period_bounds(month_start_day: int, periods_back: int) -> tuple[datetime, datetime | None]:
    """(since, until). periods_back=0 -> until=None (текущий период,
    открытый до сейчас, как раньше). periods_back>=1 -> until = конец ТОГО
    периода (начало следующего минус секунда) — период уже закрыт."""
    since = _period_start_n(month_start_day, periods_back)
    if periods_back == 0:
        return since, None
    next_since = _period_start_n(month_start_day, periods_back - 1)
    return since, next_since - timedelta(seconds=1)


def period_label(month_start_day: int, periods_back: int) -> str:
    """Подпись на кнопке. Текущий период — просто "Текущий" (у него нет
    конечной даты, показывать "10.08–?" было бы некрасиво). Закрытые —
    "10.08–10.09.26" (начало этого периода — начало следующего)."""
    if periods_back == 0:
        return "Текущий"
    since = _period_start_n(month_start_day, periods_back)
    next_since = _period_start_n(month_start_day, periods_back - 1)
    return f"{since.strftime('%d.%m')}–{next_since.strftime('%d.%m.%y')}"


def format_report_text(since: datetime, until: datetime | None, data: dict,
                       currency: str, periods_back: int, on_hand: float | None) -> str:
    if periods_back == 0:
        header = f"📊 <b>Отчёт с {since.strftime('%d.%m')} (текущий период)</b>\n"
    else:
        end_label = until.strftime("%d.%m.%y") if until else "сейчас"
        header = f"📊 <b>Отчёт {since.strftime('%d.%m')}–{end_label}</b>\n"

    lines = [
        header,
        f"💰 Доход: {data['income']:g} {currency}",
        f"💸 Расход: {data['expense']:g} {currency}",
        f"Остаток за период: {data['balance']:g} {currency}",
    ]
    if on_hand is not None:
        lines.append(f"На руках (с начала учёта): {on_hand:g} {currency}\n")
    else:
        lines.append("")

    if data["top5"]:
        lines.append("Топ категорий расходов:")
        for i, (category, amount) in enumerate(data["top5"], start=1):
            lines.append(f"{i}. {category}: {amount:g} {currency}")
    else:
        lines.append("Расходов за период пока нет.")

    return "\n".join(lines)


def _periods_keyboard(month_start_day: int):
    periods = [
        (n, period_label(month_start_day, n)) for n in range(REPORT_PERIODS_SHOWN)
    ]
    return report_periods_keyboard(periods)


async def _fetch_report_text(user: dict, periods_back: int) -> tuple[str | None, str | None]:
    """Возвращает (текст_отчёта, текст_ошибки) — ровно одно из двух не None."""
    month_start_day = user.get("month_start", 1)
    since, until = period_bounds(month_start_day, periods_back)
    try:
        data = await tx.get_report_range(user["id"], since, until)
        all_income, all_expense = await tx.get_all_time_totals(user["id"])
    except tx.NoGoogleAccount:
        return None, "Google Drive не подключён — пройди заново /start, чтобы подключить."
    except Exception:
        logging.exception("report: unexpected error fetching from Sheets")
        return None, "Не получилось обратиться к Google Диску. Если повторится — переподключи через /start."

    on_hand = tx_logic.balance_on_hand(user.get("cash_on_hand"), all_income, all_expense)
    currency = user.get("currency", "RUB")
    text = format_report_text(since, until, data, currency, periods_back, on_hand)

    try:
        narrative = await narrative_report.build_narrative_for_user(
            user["id"], since, until, label=period_label(month_start_day, periods_back),
            currency=currency, period_type="custom_period",
        )
    except Exception:
        logging.exception("report: narrative comparison failed, showing plain report without it")
        narrative = ""
    if narrative:
        text = text + "\n\n" + narrative

    return text, None


@router.message(Command("report"))
async def cmd_report(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    text, error = await _fetch_report_text(user, periods_back=0)
    if error:
        await message.answer(error)
        return

    await message.answer(text, reply_markup=_periods_keyboard(user.get("month_start", 1)))


@router.callback_query(F.data.startswith("report_period:"))
async def report_period_picked(callback: CallbackQuery):
    periods_back = int(callback.data.split(":", 1)[1])
    user = db.get_user(callback.from_user.id)
    if not user or not user.get("onboarding_done"):
        await callback.message.answer("Сначала пройди настройку: /start")
        await callback.answer()
        return

    text, error = await _fetch_report_text(user, periods_back)
    if error:
        await callback.message.answer(error)
        await callback.answer()
        return

    try:
        await callback.message.edit_text(text, reply_markup=_periods_keyboard(user.get("month_start", 1)))
    except Exception:
        # Тот же период нажали повторно (Telegram не даёт отредактировать
        # сообщение тем же текстом) или другой не критичный сбой — не
        # роняем обработку callback из-за этого.
        pass
    await callback.answer()
