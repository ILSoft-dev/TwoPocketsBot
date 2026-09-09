"""Чистая логика транзакций — без Google/Supabase/Telegram.

Нужна и боту, и тестам: фильтр периода, баланс «на руках»,
поиск дубля, мягкое undo, переименование категории в памяти,
конвертация строк Sheets <-> зеркало в Supabase (tx_mirror).
"""
from datetime import datetime, timezone


STATUS_ACTIVE = "Активна"
STATUS_DELETED = "Удалена"


def parse_dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    cleaned = "".join(ch for ch in str(value).replace(",", ".") if ch.isdigit() or ch in ".-")
    return float(cleaned) if cleaned else 0.0


def filter_active_in_range(rows: list[dict], since: datetime | None,
                           until: datetime | None,
                           date_key: str = "Дата и время") -> list[dict]:
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    result = []
    for r in rows:
        if r.get("Статус") != STATUS_ACTIVE:
            continue
        dt = parse_dt(r.get(date_key))
        if dt is None:
            continue
        if since is not None and dt < since:
            continue
        if until is not None and dt > until:
            continue
        result.append(r)
    result.sort(key=lambda r: str(r.get(date_key) or ""), reverse=True)
    return result


def report_from_rows(rows: list[dict]) -> dict:
    """Принимает и уже отфильтрованные (filter_active_in_range), и сырые
    строки — фильтрует по STATUS_ACTIVE сама, на всякий случай, чтобы
    удалённая (/undo) запись никогда не могла просочиться в отчёт, даже
    если вызывающий код забыл отфильтровать заранее."""
    active = [r for r in rows if r.get("Статус") == STATUS_ACTIVE]
    income = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "income")
    expense = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "expense")
    by_category: dict[str, float] = {}
    for r in active:
        if r.get("Тип") == "expense":
            cat = r.get("Категория") or "Разное"
            by_category[cat] = by_category.get(cat, 0) + to_float(r.get("Сумма"))
    top5 = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]
    return {"income": income, "expense": expense, "balance": income - expense, "top5": top5}


def sum_all_time(all_rows: list[dict]) -> tuple[float, float]:
    """(общий доход, общий расход) по ВСЕМ активным записям — нужно для
    баланса «на руках», который не привязан к текущему отчётному периоду."""
    active = [r for r in all_rows if r.get("Статус") == STATUS_ACTIVE]
    income = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "income")
    expense = sum(to_float(r.get("Сумма")) for r in active if r.get("Тип") == "expense")
    return income, expense


def balance_on_hand(cash_on_hand, all_income: float, all_expense: float) -> float | None:
    """Начальный остаток с онбординга + все доходы − все расходы с начала учёта."""
    if cash_on_hand is None or cash_on_hand == "":
        return None
    try:
        start = float(cash_on_hand)
    except (TypeError, ValueError):
        return None
    return start + all_income - all_expense


def find_recent_duplicate(rows: list[dict], who: str, amount: float, comment: str,
                          now: datetime | None = None, window_seconds: int = 120) -> dict | None:
    """Идемпотентность: повтор той же траты за короткое окно не пишет вторую
    строку (двойной тап, повторно доставленный апдейт Telegram и т.п.)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    comment = (comment or "").strip()
    for r in rows:
        if r.get("Статус") != STATUS_ACTIVE:
            continue
        if (r.get("Кто") or "") != who:
            continue
        if abs(to_float(r.get("Сумма")) - amount) > 1e-9:
            continue
        if (r.get("Комментарий") or "").strip() != comment:
            continue
        dt = parse_dt(r.get("Дата и время"))
        if dt is None:
            continue
        if abs((now - dt).total_seconds()) <= window_seconds:
            return r
    return None


def apply_soft_delete_last(rows: list[dict], who: str) -> tuple[dict | None, list[dict]]:
    """Возвращает (удалённая_строка, новый_список). Не мутирует вход."""
    candidates = [r for r in rows if r.get("Статус") == STATUS_ACTIVE and r.get("Кто") == who]
    if not candidates:
        return None, list(rows)
    candidates = sorted(candidates, key=lambda r: str(r.get("Дата и время") or ""), reverse=True)
    last = dict(candidates[0])
    new_rows = []
    for r in rows:
        if r.get("ID") == last.get("ID"):
            updated = dict(r)
            updated["Статус"] = STATUS_DELETED
            new_rows.append(updated)
            last = updated
        else:
            new_rows.append(r)
    return last, new_rows


def rename_category_in_rows(rows: list[dict], old_name: str, new_name: str) -> tuple[list[dict], int]:
    changed = 0
    out = []
    for r in rows:
        row = dict(r)
        if row.get("Категория") == old_name:
            row["Категория"] = new_name
            changed += 1
        out.append(row)
    return out, changed


def sheet_row_to_mirror(owner_user_id: int, row: dict) -> dict:
    return {
        "sheet_row_id": row.get("ID"),
        "owner_user_id": owner_user_id,
        "who": row.get("Кто") or "",
        "type": row.get("Тип") or "",
        "category": row.get("Категория") or "",
        "amount": to_float(row.get("Сумма")),
        "date_time": row.get("Дата и время"),
        "source": row.get("Источник") or "",
        "comment": row.get("Комментарий") or "",
        "status": row.get("Статус") or STATUS_ACTIVE,
        "quantity": row.get("Количество") if row.get("Количество") not in (None, "") else None,
        "unit": row.get("Единица") or "",
    }


def mirror_to_sheet_row(m: dict) -> dict:
    return {
        "ID": m.get("sheet_row_id"),
        "Дата и время": m.get("date_time"),
        "Кто": m.get("who") or "",
        "Тип": m.get("type") or "",
        "Категория": m.get("category") or "",
        "Сумма": m.get("amount"),
        "Источник": m.get("source") or "",
        "Комментарий": m.get("comment") or "",
        "Статус": m.get("status") or STATUS_ACTIVE,
        "Количество": m.get("quantity") if m.get("quantity") is not None else "",
        "Единица": m.get("unit") or "",
    }


def filter_by_who(rows: list[dict], who: str) -> list[dict]:
    return [r for r in rows if r.get("Кто") == who]


def sort_by_date_desc(rows: list[dict], date_key: str = "Дата и время") -> list[dict]:
    return sorted(rows, key=lambda r: str(r.get(date_key) or ""), reverse=True)


def parse_user_date(text: str, today=None, max_days_back: int = 730):
    """Общий парсер дат из пользовательского ввода — ДД.ММ или ДД.ММ.ГГ(ГГ).
    Используется и /backdate, и /edit (см. backdate.py, edit.py), чтобы не
    держать два чуть разных парсера дат в одном проекте. Отклоняет даты в
    будущем и дальше max_days_back дней в прошлом (почти наверняка
    опечатка, а не осознанный ввод).

    ВАЖНО про формат ДД.ММ без года: год берётся текущий, БЕЗ автоматического
    переноса на прошлый год, даже если результат попал в будущее (например,
    "15.09" в сентябре при today=09.09 — это будущая дата, отклоняем, а не
    молча трактуем как "15 сентября прошлого года"). Для дат вблизи границы
    года ("хочу занести декабрьскую покупку в январе") просто укажи год
    явно: "28.12.25" вместо "28.12" — это чуть менее удобно, зато никогда
    не подставит не тот год без ведома пользователя."""
    today = today or datetime.now(timezone.utc).date()
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            parsed = parsed.replace(year=today.year)
        if parsed > today or (today - parsed).days > max_days_back:
            return None
        return parsed
    return None


# ------------------------------------------------- structural period notes ---
# "Заметки", а не советы — про ФОРМУ трат, никогда не про их содержание
# (см. обсуждение в чате: "трать меньше на мороженое" — оценочно и рискует
# промахнуться мимо контекста; "8 крупных покупок = 49% бюджета" — просто
# факт про структуру). Каждая функция ниже возвращает пусто, если порог
# значимости не пройден — молчание по умолчанию, не шум ради шума.

def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Простое склонение количественных существительных для заметок ниже
    (1 покупка / 2 покупки / 5 покупок, с исключением 11-14 → many)."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    n1 = n_abs % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def note_category_shape(rows: list[dict], currency: str,
                        min_category_total: float = 50,
                        concentrated_threshold: float = 0.5,
                        spread_threshold: float = 0.15) -> list[str]:
    """Топ-3 категории по сумме: одна крупная покупка тащит всю категорию
    (концентрированная — предсказуемый разовый расход) или много мелких
    без явного лидера (размазанная — фоновый регулярный расход). Одно и
    то же место в топе может означать совершенно разное поведение."""
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        if r.get("Тип") != "expense":
            continue
        cat = r.get("Категория") or "Разное"
        by_cat.setdefault(cat, []).append(to_float(r.get("Сумма")))

    totals = {cat: sum(amts) for cat, amts in by_cat.items()}
    top = sorted(totals.items(), key=lambda x: -x[1])[:3]

    notes = []
    for cat, total in top:
        amts = by_cat[cat]
        if total < min_category_total or len(amts) < 2:
            continue
        biggest = max(amts)
        pct = biggest / total
        if pct >= concentrated_threshold:
            notes.append(
                f"«{cat}» — почти целиком одна покупка ({biggest:g}{currency}, "
                f"{pct * 100:.0f}% суммы категории), не растущий фон расходов"
            )
        elif pct <= spread_threshold and len(amts) >= 5:
            notes.append(
                f"«{cat}» — {len(amts)} мелких покупок без явного лидера "
                f"(самая крупная — только {pct * 100:.0f}% суммы категории)"
            )
    return notes


def _cluster_by_time(rows: list[dict], window_seconds: int) -> list[list[dict]]:
    """Общая часть note_hidden_visits/note_batch_logging_sessions — группирует
    строки, идущие подряд с разницей не больше window_seconds, в кластеры."""
    sorted_rows = sorted(rows, key=lambda r: str(r.get("Дата и время") or ""))
    clusters: list[list[dict]] = []
    cluster: list[dict] = []
    for r in sorted_rows:
        if not cluster:
            cluster = [r]
            continue
        prev_t = parse_dt(cluster[-1].get("Дата и время"))
        cur_t = parse_dt(r.get("Дата и время"))
        if prev_t and cur_t and 0 <= (cur_t - prev_t).total_seconds() <= window_seconds:
            cluster.append(r)
        else:
            clusters.append(cluster)
            cluster = [r]
    if cluster:
        clusters.append(cluster)
    return clusters


def note_hidden_visits(rows: list[dict], currency: str,
                       window_seconds: int = 600, min_items: int = 3,
                       min_total: float = 10) -> list[str]:
    """Несколько покупок ОДНОЙ категории, вбитых почти подряд — вероятно
    один визит/поход, распределённый по отдельным строкам. По одной каждая
    незаметна, вместе — заметная сумма, которая иначе спрятана в списке."""
    expense_rows = [r for r in rows if r.get("Тип") == "expense"]
    notes = []
    for cluster in _cluster_by_time(expense_rows, window_seconds):
        if len(cluster) < min_items:
            continue
        cats = {r.get("Категория") for r in cluster}
        if len(cats) != 1:
            continue  # смешанные категории — это сессия пакетного ввода, см. ниже
        total = sum(to_float(r.get("Сумма")) for r in cluster)
        if total < min_total:
            continue
        cat = cats.pop()
        dt = parse_dt(cluster[0].get("Дата и время"))
        date_str = dt.strftime("%d.%m") if dt else "?"
        purchase_word = _ru_plural(len(cluster), "покупка", "покупки", "покупок")
        notes.append(
            f"{date_str}: {len(cluster)} {purchase_word} «{cat}» одним визитом — "
            f"вместе {total:g}{currency}, по отдельности не видно"
        )
    return notes


def note_frequency(rows: list[dict], period_days: int,
                   min_occurrences: int = 4, max_avg_gap_days: float = 10) -> list[str]:
    """Регулярность категории — не сумма, а КАК ЧАСТО. Там, где сумма
    маленькая и незаметная, сама регулярность иногда узнаваемее — не
    "сколько ушло", а "как часто это вообще происходит"."""
    if period_days <= 0:
        return []
    by_cat: dict[str, int] = {}
    for r in rows:
        if r.get("Тип") != "expense":
            continue
        cat = r.get("Категория") or "Разное"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    notes = []
    for cat, count in by_cat.items():
        if count < min_occurrences:
            continue
        avg_gap = period_days / count
        if avg_gap <= max_avg_gap_days:
            notes.append(f"«{cat}» — регулярно, примерно раз в {avg_gap:.1f} дня ({count} раз за период)")
    return notes


def note_big_purchases_share_of_income(rows: list[dict], income_total: float, currency: str,
                                       big_threshold: float = 100,
                                       share_threshold: float = 0.4) -> str | None:
    """Крупные покупки относительно ДОХОДА периода, не расхода — часто
    нагляднее, потому что напрямую отвечает "на что хватило бы заработанного
    в этом же периоде", а не абстрактный процент от общей суммы трат."""
    if income_total <= 0:
        return None
    big = [r for r in rows if r.get("Тип") == "expense" and to_float(r.get("Сумма")) >= big_threshold]
    if not big:
        return None
    big_total = sum(to_float(r.get("Сумма")) for r in big)
    share = big_total / income_total
    if share < share_threshold:
        return None
    purchase_word = _ru_plural(len(big), "крупная покупка", "крупные покупки", "крупных покупок")
    return (
        f"{len(big)} {purchase_word} (от {big_threshold:g}{currency}) — {big_total:g}{currency}, "
        f"это {share * 100:.0f}% всего дохода периода"
    )


def note_batch_logging_sessions(rows: list[dict], window_seconds: int = 600,
                                min_items: int = 4, min_sessions: int = 3) -> str | None:
    """НЕ про деньги — про привычку пользования ботом: несколько РАЗНЫХ
    категорий вбито почти подряд, похоже на "наверстал ввод за несколько
    дней сразу", а не на реальный визит одной покупки (см. note_hidden_visits
    выше — там ровно обратный критерий, одна категория в кластере)."""
    sessions = 0
    for cluster in _cluster_by_time(rows, window_seconds):
        if len(cluster) >= min_items and len({r.get("Категория") for r in cluster}) > 1:
            sessions += 1

    if sessions < min_sessions:
        return None
    session_word = _ru_plural(sessions, "сессия", "сессии", "сессий")
    return (
        f"Часть записей вносится не в моменте, а пачками ({sessions} {session_word} за период, "
        f"когда сразу несколько разных покупок вбито подряд) — если дата покупки важна "
        f"отдельно от даты ввода, это стоит иметь в виду"
    )


def compute_period_notes(rows: list[dict], income_total: float, currency: str,
                         period_days: int) -> list[str]:
    """Собирает все 5 заметок разом — каждая проходит СВОЙ порог значимости
    независимо (см. docstring каждой note_* функции выше). Молчание по
    умолчанию: если ни одна не набрала порог, список пустой, а не пять
    натянутых пунктов ради заполнения места."""
    notes: list[str] = []
    notes.extend(note_category_shape(rows, currency))
    notes.extend(note_hidden_visits(rows, currency))
    notes.extend(note_frequency(rows, period_days))
    big_note = note_big_purchases_share_of_income(rows, income_total, currency)
    if big_note:
        notes.append(big_note)
    batch_note = note_batch_logging_sessions(rows)
    if batch_note:
        notes.append(batch_note)
    return notes


