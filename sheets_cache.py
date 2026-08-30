"""
Короткий Redis-кэш для get_rows().

Не меняет семантику данных: TTL маленький, любая запись в лист
(append_row / update_cell / replace_column_value) сбрасывает ключ.
Если Redis недоступен — читаем Google как раньше, без исключения наружу.
"""
import json
import logging

import redis.asyncio as redis

from config import REDIS_URL, SHEETS_CACHE_TTL_SECONDS, redis_connection_kwargs

logger = logging.getLogger(__name__)

_redis = redis.from_url(REDIS_URL, decode_responses=True, **redis_connection_kwargs())


def _key(spreadsheet_id: str, sheet_name: str) -> str:
    return f"sheets:rows:{spreadsheet_id}:{sheet_name}"


async def get(spreadsheet_id: str, sheet_name: str):
    if SHEETS_CACHE_TTL_SECONDS <= 0:
        return None
    try:
        raw = await _redis.get(_key(spreadsheet_id, sheet_name))
    except Exception:
        logger.exception("sheets_cache.get failed — falling back to live read")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set(spreadsheet_id: str, sheet_name: str, rows: list) -> None:
    if SHEETS_CACHE_TTL_SECONDS <= 0:
        return
    try:
        await _redis.set(
            _key(spreadsheet_id, sheet_name),
            json.dumps(rows, ensure_ascii=False),
            ex=SHEETS_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.exception("sheets_cache.set failed — cache skipped")


async def invalidate(spreadsheet_id: str, sheet_name: str | None = None) -> None:
    try:
        if sheet_name:
            await _redis.delete(_key(spreadsheet_id, sheet_name))
            return
        # На всякий случай: сбросить все листы одной таблицы.
        async for key in _redis.scan_iter(match=f"sheets:rows:{spreadsheet_id}:*"):
            await _redis.delete(key)
    except Exception:
        logger.exception("sheets_cache.invalidate failed")
