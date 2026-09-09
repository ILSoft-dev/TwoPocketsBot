"""
car_stats.py
v1.0 - narrative car usage/spending statistics.

Two consumers:
- car_stats_command.py — /carstats, on demand, current period.
- run_monthly_stats_sweep() below — cron-triggered (same daily ping as
  reminders.py), fires once per user's OWN reporting period (their
  month_start setting, not necessarily a calendar month), sent to
  everyone who shares that spreadsheet (owner + family members).
"""
from datetime import datetime, timedelta, timezone

import aiohttp

import supabase_client as db
import sheets_client as sc
import cars
import google_api
from report import period_start
from period_utils import is_period_end_today

CATEGORY_LABELS = {
    "Заправка": "на бензин",
    "Ремонт": "на ремонт",
    "ТО": "на техобслуживание",
    "Прочее": "на прочее",
}


def _in_period(row_date, since: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(str(row_date))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= since


async def car_period_stats(account: dict, car_name: str, since: datetime) -> dict:
    """{'distance': float|None, 'total_spent': float, 'by_type': {type: amount}}
    distance is None if there's no mileage data to compute it from at all
    (car just registered, or no mileage ever logged)."""
    box = google_api.TokenBox(account)
    async with sc.new_session() as session:
        async def _get_auto(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_AUTO)
        auto_rows = await google_api.call(box, _get_auto)

        async def _get_mileage(token):
            return await sc.get_rows(session, token, account["google_spreadsheet_id"], sc.SHEET_MILEAGE)
        mileage_rows = await google_api.call(box, _get_mileage)

    car_auto = [r for r in auto_rows if r["Машина"] == car_name and r["Статус"] == "Активна"]
    period_auto = [r for r in car_auto if _in_period(r["Дата"], since)]

    by_type: dict[str, float] = {}
    for r in period_auto:
        by_type[r["Тип"]] = by_type.get(r["Тип"], 0) + sc.to_float(r["Сумма"])
    total_spent = sum(by_type.values())

    car_mileage = [r for r in mileage_rows if r["Машина"] == car_name]
    car_mileage.sort(key=lambda r: str(r["Дата"]))
    period_points = [r for r in car_mileage if _in_period(r["Дата"], since)]
    baseline_points = [r for r in car_mileage if not _in_period(r["Дата"], since)]

    distance = None
    if period_points:
        latest = sc.to_float(period_points[-1]["Пробег"])
        baseline = sc.to_float(baseline_points[-1]["Пробег"]) if baseline_points else sc.to_float(period_points[0]["Пробег"])
        distance = latest - baseline

    return {"distance": distance, "total_spent": total_spent, "by_type": by_type}


def format_stats_text(car_name: str, stats: dict, currency: str, period_label: str = "этот период") -> str:
    distance, total_spent = stats["distance"], stats["total_spent"]

    if distance is not None and total_spent > 0:
        headline = (f"🚗 За {period_label} на «{car_name}» проехали {distance:g} км, "
                    f"потратили {total_spent:g} {currency}.")
    elif distance is not None:
        headline = f"🚗 За {period_label} на «{car_name}» проехали {distance:g} км, трат не было."
    elif total_spent > 0:
        headline = (f"🚗 За {period_label} на «{car_name}» потратили {total_spent:g} {currency}, "
                    "данных о пробеге нет.")
    else:
        headline = f"🚗 За {period_label} на «{car_name}» нет данных ни по пробегу, ни по тратам."

    lines = [headline]
    if stats["by_type"]:
        for t, amount in sorted(stats["by_type"].items(), key=lambda kv: -kv[1]):
            label = CATEGORY_LABELS.get(t, t.lower())
            lines.append(f"• {label}: {amount:g} {currency}")

    return "\n".join(lines)


# --------------------------------------------------------- automatic sweep --

async def run_monthly_stats_sweep(bot) -> int:
    """Called from the cron web route (same daily ping as reminders.py).
    Returns how many messages were sent."""
    sent = 0

    for account in db.list_google_connected_users():
        owner = db.get_user_by_id(account["id"])
        if not owner:
            continue
        month_start_day = owner.get("month_start", 1)
        if not is_period_end_today(month_start_day):
            continue

        active_cars = await cars.list_active_cars(account)
        if not active_cars:
            continue

        since = period_start(month_start_day)
        currency = owner.get("currency", "RUB")
        recipients = db.get_spreadsheet_recipients(account["id"])

        for car in active_cars:
            stats = await car_period_stats(account, car["Машина"], since)
            text = format_stats_text(car["Машина"], stats, currency, period_label="этот месяц")
            for tg_id in recipients:
                try:
                    await bot.send_message(tg_id, text)
                    sent += 1
                except Exception:
                    pass  # заблокировал бота и т.п. — не роняем весь sweep

    return sent
