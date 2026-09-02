"""
PIN-код: хеш хранится в Supabase (users.pin_hash), активная сессия
(факт того, что юзер уже ввёл верный PIN) — в Redis с TTL 5 минут.
Счётчик неверных попыток — тоже в Redis, с временной блокировкой.
"""
import hashlib
import os
import redis.asyncio as redis

from config import (
    REDIS_URL,
    PIN_SESSION_TTL_SECONDS,
    PIN_MAX_ATTEMPTS,
    PIN_LOCKOUT_SECONDS,
    redis_connection_kwargs,
)

_redis = redis.from_url(REDIS_URL, decode_responses=True, **redis_connection_kwargs())

_SALT = os.getenv("PIN_SALT", "financial-home-static-salt")  # можно вынести в .env


def hash_pin(pin: str) -> str:
    return hashlib.sha256(f"{_SALT}:{pin}".encode()).hexdigest()


def verify_pin(pin: str, pin_hash: str) -> bool:
    return hash_pin(pin) == pin_hash


def _session_key(tg_id: int) -> str:
    return f"pin_session:{tg_id}"


def _attempts_key(tg_id: int) -> str:
    return f"pin_attempts:{tg_id}"


def _lockout_key(tg_id: int) -> str:
    return f"pin_lockout:{tg_id}"


async def has_active_session(tg_id: int) -> bool:
    return bool(await _redis.get(_session_key(tg_id)))


async def open_session(tg_id: int) -> None:
    await _redis.set(_session_key(tg_id), "1", ex=PIN_SESSION_TTL_SECONDS)
    await _redis.delete(_attempts_key(tg_id))


async def is_locked_out(tg_id: int) -> bool:
    return bool(await _redis.get(_lockout_key(tg_id)))


async def register_failed_attempt(tg_id: int) -> int:
    """Возвращает текущее число неудачных попыток подряд."""
    attempts = await _redis.incr(_attempts_key(tg_id))
    await _redis.expire(_attempts_key(tg_id), PIN_LOCKOUT_SECONDS)
    if attempts >= PIN_MAX_ATTEMPTS:
        await _redis.set(_lockout_key(tg_id), "1", ex=PIN_LOCKOUT_SECONDS)
    return attempts
