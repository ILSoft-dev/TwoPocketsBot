"""
google_api.py
v1.1 - "refresh the Google access token on 401, retry exactly once" +
retry-with-backoff for transient Google-side errors (429/500/502/503/504).

This was the actual root cause of "траты уходят в тишину" / "ошибка
авторизации при вызове истории": access tokens expire after ~1 hour, and
NONE of sheets_transactions.py / cars.py / car_stats.py / fluid_tracker.py /
reminders.py ever refreshed them — every Sheets API call made after the
first hour post-connection raised sheets_client.GoogleAuthError, uncaught,
crashing the update silently from the user's point of view.

Retrying the whole surrounding function (instead of just the one failed
call) would risk duplicate writes if an earlier call in a multi-step
operation (e.g. save_auto_expense's 2-3 appends) already succeeded before a
later one hit the 401 — so the refresh+retry happens at the level of each
individual Sheets API call, not the function around it (same principle
PixKeep's _upload_all already used for exactly this reason).

Changelog:
- v1.1: Google's Sheets API occasionally returns 503/500/502/429 for a few
  seconds under its own load — this used to propagate straight up as an
  uncaught RuntimeError, and (combined with aiohttp's ~5-minute default
  timeout, see sheets_client.new_session()) could leave a user staring at
  "не получилось обратиться к Google Диску" after a ~3-minute hang, even
  though a simple retry a second later would have succeeded. Now retried
  with backoff (config.GOOGLE_TRANSIENT_RETRY_DELAYS) before giving up.
"""
import asyncio
import logging

import supabase_client as db
from google_oauth import refresh_access_token
from sheets_client import GoogleAuthError, TransientSheetsError
from config import GOOGLE_TRANSIENT_RETRY_DELAYS

logger = logging.getLogger(__name__)


class TokenBox:
    """Mutable holder for one multi-step operation's access token. If a
    mid-operation call hits 401, refresh() updates it in place — subsequent
    calls within the same operation (e.g. the second/third append_row in
    save_auto_expense) automatically reuse the refreshed token instead of
    each independently refreshing again."""

    def __init__(self, account: dict):
        self.account = account
        self.access_token = account["google_access_token"]
        self._refreshed = False

    async def refresh(self) -> str:
        if not self._refreshed:
            new = await refresh_access_token(self.account["google_refresh_token"])
            self.access_token = new["access_token"]
            db.update_google_access_token(
                self.account["id"], new["access_token"], new["refresh_token"]
            )
            self._refreshed = True
        return self.access_token


async def call(box: TokenBox, operation):
    """`operation` is an async callable taking one argument (the access
    token). Tries with box's current token; on GoogleAuthError, refreshes
    (via box, so it's shared across the rest of the same operation) and
    retries exactly once. On TransientSheetsError (Google's side briefly
    unavailable), retries the same call with backoff — auth refresh and
    transient retry are independent: a transient error can happen before
    OR after a token refresh, and either is handled."""

    async def attempt_with_auth_retry():
        try:
            return await operation(box.access_token)
        except GoogleAuthError:
            token = await box.refresh()
            return await operation(token)

    last_error: Exception | None = None
    for i, delay in enumerate(GOOGLE_TRANSIENT_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await attempt_with_auth_retry()
        except TransientSheetsError as e:
            last_error = e
            logger.warning(
                "Google Sheets API transient error, attempt %d/%d: %s",
                i + 1, len(GOOGLE_TRANSIENT_RETRY_DELAYS), e,
            )
            continue

    raise last_error
