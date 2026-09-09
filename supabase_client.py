"""
Обёртка над Supabase для всех операций MVP: пользователи, транзакции,
категории, семейный бюджет.
"""
import logging
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, DEFAULT_CATEGORIES

logger = logging.getLogger(__name__)

db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------- Пользователи ----------

def get_user(tg_id: int):
    res = db.table("users").select("*").eq("tg_id", tg_id).execute()
    return res.data[0] if res.data else None


def get_user_by_id(user_id: int):
    res = db.table("users").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else None


def get_user_by_username(username: str):
    res = db.table("users").select("*").eq("username", username).execute()
    return res.data[0] if res.data else None


def create_user(tg_id: int, username: str | None):
    res = db.table("users").insert({"tg_id": tg_id, "username": username}).execute()
    user = res.data[0]
    seed_default_categories(user["id"])
    return user


def get_or_create_user(tg_id: int, username: str | None):
    user = get_user(tg_id)
    return user if user else create_user(tg_id, username)


def update_user(user_id: int, **fields):
    db.table("users").update(fields).eq("id", user_id).execute()


def finish_onboarding(user_id: int, currency: str, month_start: int, cash_on_hand: float | None):
    update_user(
        user_id,
        currency=currency,
        month_start=month_start,
        cash_on_hand=cash_on_hand,
        onboarding_done=True,
    )


# ---------- Категории ----------

def seed_default_categories(user_id: int):
    rows = [{"user_id": user_id, "name": name, "is_custom": False} for name in DEFAULT_CATEGORIES]
    db.table("categories").upsert(rows, on_conflict="user_id,name").execute()


def get_categories(user_id: int):
    res = db.table("categories").select("*").eq("user_id", user_id).order("id").execute()
    return res.data


def add_category(user_id: int, name: str):
    db.table("categories").insert({"user_id": user_id, "name": name, "is_custom": True}).execute()


def rename_category(user_id: int, old_name: str, new_name: str):
    db.table("categories").update({"name": new_name}).eq("user_id", user_id).eq("name", old_name).execute()
    # Память категоризации должна ехать вместе с переименованием, иначе
    # выученные слова продолжат писать старое имя в новую историю.
    db.table("category_map").update({"category": new_name}).eq("user_id", user_id).eq("category", old_name).execute()
    # Сами транзакции живут в Google Sheets, не здесь (таблица "transactions"
    # ниже в схеме — устаревшая, код её не читает). Строки в листе
    # «Транзакции» обновляет sheets_transactions.rename_category_in_sheet.


# ---------- Обучение категоризации (category_map) ----------

def remember_keyword_category(user_id: int, keyword: str, category: str):
    db.table("category_map").upsert(
        {"user_id": user_id, "keyword": keyword.lower(), "category": category},
        on_conflict="user_id,keyword",
    ).execute()


def lookup_keyword_category(user_id: int, remainder_text: str) -> str | None:
    res = db.table("category_map").select("keyword,category").eq("user_id", user_id).execute()
    lowered = remainder_text.lower()
    # Самое длинное совпадение побеждает: «молоко топленое» не должно
    # перехватываться более коротким «молоко», если оба выучены.
    best = None
    best_len = -1
    for row in res.data or []:
        kw = row.get("keyword") or ""
        if kw and kw in lowered and len(kw) > best_len:
            best = row["category"]
            best_len = len(kw)
    return best


# ---------- Google Sheets (OAuth-токены + личная таблица) ----------

def save_google_tokens(user_id: int, email: str, access_token: str,
                       refresh_token: str, spreadsheet_id: str | None = None):
    fields = {
        "google_email": email,
        "google_access_token": access_token,
        "google_refresh_token": refresh_token,
    }
    if spreadsheet_id is not None:
        fields["google_spreadsheet_id"] = spreadsheet_id
    update_user(user_id, **fields)


def update_google_access_token(user_id: int, access_token: str, refresh_token: str):
    update_user(user_id, google_access_token=access_token, google_refresh_token=refresh_token)


def set_google_spreadsheet_id(user_id: int, spreadsheet_id: str):
    update_user(user_id, google_spreadsheet_id=spreadsheet_id)


def get_google_account(user_id: int) -> dict | None:
    """Raw Google tokens/spreadsheet for exactly this user_id (their own,
    not resolved through family — see get_effective_google_account for that)."""
    res = (
        db.table("users")
        .select("id, google_email, google_access_token, google_refresh_token, google_spreadsheet_id")
        .eq("id", user_id)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    if not row.get("google_access_token"):
        return None
    return row


def get_family_owner_id(family_id: int) -> int | None:
    res = db.table("family").select("owner_user_id").eq("id", family_id).execute()
    return res.data[0]["owner_user_id"] if res.data else None


def get_effective_google_account(user_id: int) -> dict | None:
    """The Google account transactions should actually be written to: the
    user's own account, UNLESS they're in a family — then the family
    owner's account (owner_user_id on the family row), per the "вариант А"
    design: one shared spreadsheet per family, owned by whoever sent the
    /family invite."""
    family_id = get_family_id(user_id)
    if family_id:
        owner_id = get_family_owner_id(family_id)
        if owner_id:
            return get_google_account(owner_id)
    return get_google_account(user_id)


def ping() -> bool:
    """Максимально дешёвый запрос — только чтобы Supabase видел активность
    по API. Free-tier проекты Supabase приостанавливаются после ~7 дней без
    обращений; вызывается периодически из main.py (supabase_keepalive_loop),
    независимо от внешних cron-пингов на /health или /cron/daily."""
    try:
        db.table("users").select("id").limit(1).execute()
        return True
    except Exception:
        logger.warning("Supabase keep-alive ping failed", exc_info=True)
        return False


def list_google_connected_users() -> list[dict]:
    """Every user with a personal spreadsheet — the reminder sweep iterates
    this. Includes non-owner family members too (their own personal sheet
    just won't have any cars registered on it while they're in a family —
    harmless, a few no-op checks, not a correctness issue)."""
    res = (
        db.table("users")
        .select("id, tg_id, google_access_token, google_refresh_token, google_spreadsheet_id")
        .not_.is_("google_spreadsheet_id", "null")
        .execute()
    )
    return res.data or []


def get_spreadsheet_recipients(owner_user_id: int) -> list[int]:
    """Telegram IDs that should see notifications tied to owner_user_id's
    OWN spreadsheet: themselves, plus any family members currently using it
    as their effective account (i.e. a family owned by owner_user_id)."""
    recipients: set[int] = set()

    owner_row = db.table("users").select("tg_id").eq("id", owner_user_id).execute()
    if owner_row.data:
        recipients.add(owner_row.data[0]["tg_id"])

    fam = db.table("family").select("id").eq("owner_user_id", owner_user_id).execute()
    if fam.data:
        family_id = fam.data[0]["id"]
        members = db.table("family_members").select("user_id").eq("family_id", family_id).execute()
        member_ids = [m["user_id"] for m in members.data if m["user_id"] != owner_user_id]
        if member_ids:
            users_res = db.table("users").select("tg_id").in_("id", member_ids).execute()
            for u in users_res.data:
                recipients.add(u["tg_id"])

    return list(recipients)


# ---------- Семейный бюджет ----------

def get_family_id(user_id: int) -> int | None:
    res = db.table("family_members").select("family_id").eq("user_id", user_id).execute()
    return res.data[0]["family_id"] if res.data else None


def create_family_invite(from_user_id: int, to_tg_id: int):
    res = db.table("family_invites").insert(
        {"from_user_id": from_user_id, "to_tg_id": to_tg_id, "status": "pending"}
    ).execute()
    return res.data[0]


def get_pending_invite(invite_id: int):
    res = db.table("family_invites").select("*").eq("id", invite_id).eq("status", "pending").execute()
    return res.data[0] if res.data else None


def accept_family_invite(invite_id: int, accepting_user_id: int):
    invite = get_pending_invite(invite_id)
    if not invite:
        return None

    existing_family_id = get_family_id(invite["from_user_id"])
    if existing_family_id:
        family_id = existing_family_id
    else:
        # Владелец Sheets = тот, кто отправил приглашение (вариант А) —
        # фиксируем это явно в owner_user_id, а не полагаемся на порядок
        # вставки в family_members.
        fam = db.table("family").insert({"owner_user_id": invite["from_user_id"]}).execute()
        family_id = fam.data[0]["id"]
        db.table("family_members").insert({"family_id": family_id, "user_id": invite["from_user_id"]}).execute()

    db.table("family_members").insert({"family_id": family_id, "user_id": accepting_user_id}).execute()
    db.table("family_invites").update({"status": "accepted"}).eq("id", invite_id).execute()
    return family_id


def leave_family(user_id: int):
    family_id = get_family_id(user_id)
    if family_id:
        db.table("family_members").delete().eq("user_id", user_id).eq("family_id", family_id).execute()
    return family_id


# ---------- Зеркало транзакций (tx_mirror) ----------
# Читает/пишет только если таблица tx_mirror создана в Supabase (см.
# schema.sql). Если её нет или Supabase недоступен — все функции тихо
# возвращают пусто/False, а sheets_transactions._all_tx_rows() просто
# продолжает читать Sheets напрямую, как без зеркала вообще. Ничего не
# ломается, если /resync ни разу не понадобился.

def mirror_upsert_row(owner_user_id: int, sheet_row: dict) -> bool:
    try:
        import tx_logic
        payload = tx_logic.sheet_row_to_mirror(owner_user_id, sheet_row)
        if not payload.get("sheet_row_id"):
            return False
        db.table("tx_mirror").upsert(payload, on_conflict="sheet_row_id").execute()
        return True
    except Exception:
        logger.warning("tx_mirror upsert skipped", exc_info=True)
        return False


def mirror_hydrate(owner_user_id: int, rows: list[dict]) -> int:
    """Одним batch-upsert'ом, а не по строке за раз — при первой гидратации
    аккаунта с полугодовой историей это разница между одним запросом и
    сотней последовательных HTTP round-trip'ов в Supabase."""
    if not rows:
        return 0
    try:
        import tx_logic
        payloads = [
            tx_logic.sheet_row_to_mirror(owner_user_id, row) for row in rows
            if row.get("ID")
        ]
        if not payloads:
            return 0
        db.table("tx_mirror").upsert(payloads, on_conflict="sheet_row_id").execute()
        return len(payloads)
    except Exception:
        logger.warning("tx_mirror hydrate skipped", exc_info=True)
        return 0


def mirror_list_rows(owner_user_id: int) -> list[dict]:
    try:
        res = db.table("tx_mirror").select("*").eq("owner_user_id", owner_user_id).execute()
        return res.data or []
    except Exception:
        logger.warning("tx_mirror list skipped", exc_info=True)
        return []


def mirror_mark_deleted(sheet_row_id: str) -> None:
    try:
        db.table("tx_mirror").update({"status": "Удалена"}).eq("sheet_row_id", sheet_row_id).execute()
    except Exception:
        logger.warning("tx_mirror mark deleted skipped", exc_info=True)


def mirror_update_date(sheet_row_id: str, new_date_time: str) -> None:
    """Точечный UPDATE одного поля, не upsert — upsert с частичным payload
    заменил бы всю строку зеркала (занулив категорию/сумму/статус и т.д.),
    потому что Supabase upsert по умолчанию не мёрджит, а перезаписывает.
    Тот же принцип, что и у mirror_mark_deleted выше."""
    try:
        db.table("tx_mirror").update({"date_time": new_date_time}).eq("sheet_row_id", sheet_row_id).execute()
    except Exception:
        logger.warning("tx_mirror update date skipped", exc_info=True)


def mirror_rename_category(owner_user_id: int | None, old_name: str, new_name: str) -> None:
    try:
        q = db.table("tx_mirror").update({"category": new_name}).eq("category", old_name)
        if owner_user_id is not None:
            q = q.eq("owner_user_id", owner_user_id)
        q.execute()
    except Exception:
        logger.warning("tx_mirror rename skipped", exc_info=True)


def mirror_row_count(owner_user_id: int) -> int | None:
    """None значит "не удалось узнать" (таблицы нет / Supabase недоступен) —
    отличается от 0 (таблица есть, но реально пуста), чтобы /resync мог
    сообщить об этом отдельно, а не молча соврать про 0 строк."""
    try:
        res = (
            db.table("tx_mirror")
            .select("sheet_row_id", count="exact")
            .eq("owner_user_id", owner_user_id)
            .execute()
        )
        return res.count if res.count is not None else len(res.data or [])
    except Exception:
        logger.warning("tx_mirror count skipped", exc_info=True)
        return None


def mirror_clear(owner_user_id: int) -> bool:
    """Стирает зеркало для аккаунта — следующее чтение перегидрирует его
    заново из живого Sheets. Используется командой /resync (см. resync.py)
    и как ручной способ поправить расхождение, если что-то пошло не так."""
    try:
        db.table("tx_mirror").delete().eq("owner_user_id", owner_user_id).execute()
        return True
    except Exception:
        logger.warning("tx_mirror clear failed", exc_info=True)
        return False
