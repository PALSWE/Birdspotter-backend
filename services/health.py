"""Read-only health contract for BirdSpotter.

The health endpoint deliberately does not call SLU. It derives the reported
state from the state written by the timer-triggered sync in
``services.sync_engine``, so a health request never adds external load or new
persistence.

Freshness is based on ``lastSuccessfulRunCompletedAt`` (when the cache was last
successfully refreshed), not on ``latestSourceModifiedAt`` (when the source last
changed), because source observations are naturally old during night hours and
periods of low activity.
"""

import datetime
import logging
import os
from typing import Any, Callable, Optional

from services import sync_engine

HEALTH_SYNC_STALE_SECONDS_ENV = "HEALTH_SYNC_STALE_SECONDS"
DEFAULT_SYNC_STALE_SECONDS = 900
APP_VERSION_ENV = "APP_VERSION"
DEFAULT_APP_VERSION = "v1.3"

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

CHECK_OK = "ok"
CHECK_ERROR = "error"
CHECK_FAILING = "failing"
CHECK_UNKNOWN = "unknown"
CHECK_FRESH = "fresh"
CHECK_STALE = "stale"
CHECK_MISSING = "missing"

REASON_OK = "OK"
REASON_SYNC_STALE = "SYNC_STALE"
REASON_NO_SUCCESSFUL_SYNC = "NO_SUCCESSFUL_SYNC"
REASON_INVALID_STATE = "INVALID_STATE"
REASON_STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
REASON_CONFIG_ERROR = "CONFIG_ERROR"

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503

SyncstateReader = Callable[[], Optional[dict]]


def get_stale_after_seconds() -> int:
    """Return the configured freshness threshold, falling back to the default."""

    raw_value = os.environ.get(HEALTH_SYNC_STALE_SECONDS_ENV)

    if raw_value is None or not str(raw_value).strip():
        return DEFAULT_SYNC_STALE_SECONDS

    try:
        value = int(str(raw_value).strip())
    except ValueError:
        logging.warning(
            "Invalid %s value; using default %s seconds.",
            HEALTH_SYNC_STALE_SECONDS_ENV,
            DEFAULT_SYNC_STALE_SECONDS,
        )
        return DEFAULT_SYNC_STALE_SECONDS

    if value <= 0:
        logging.warning(
            "%s must be greater than zero; using default %s seconds.",
            HEALTH_SYNC_STALE_SECONDS_ENV,
            DEFAULT_SYNC_STALE_SECONDS,
        )
        return DEFAULT_SYNC_STALE_SECONDS

    return value


def get_app_version() -> str:
    version = os.environ.get(APP_VERSION_ENV)

    if not version or not version.strip():
        return DEFAULT_APP_VERSION

    return version.strip()


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def read_syncstate() -> Optional[dict]:
    return sync_engine.storage.read_json(sync_engine.SYNCSTATE_FILENAME)


def _as_aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)

    return value


def compute_sync_age_seconds(
    last_successful_sync_at: Optional[str],
    now: Optional[datetime.datetime] = None,
) -> Optional[int]:
    """Return the age in seconds of the last successful sync, or ``None``."""

    parsed = sync_engine.parse_iso_datetime(last_successful_sync_at)

    if parsed is None:
        return None

    current_time = _as_aware(now or utc_now())
    age_seconds = (current_time - _as_aware(parsed)).total_seconds()

    if age_seconds < 0:
        return 0

    return int(age_seconds)


def _clean_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None

    stripped = value.strip()

    if not stripped:
        return None

    return stripped


def classify_data_freshness(
    last_successful_sync_at: Optional[str],
    sync_age_seconds: Optional[int],
    stale_after_seconds: int,
) -> str:
    if last_successful_sync_at is None:
        return CHECK_MISSING

    if sync_age_seconds is None:
        return CHECK_UNKNOWN

    if sync_age_seconds > stale_after_seconds:
        return CHECK_STALE

    return CHECK_FRESH


def classify_slu_status(last_attempt: Any) -> str:
    if not isinstance(last_attempt, dict):
        return CHECK_UNKNOWN

    outcome = last_attempt.get("outcome")

    if outcome == sync_engine.SYNC_OUTCOME_SUCCESS:
        return CHECK_OK

    if outcome == sync_engine.SYNC_OUTCOME_FAILURE:
        return CHECK_FAILING

    return CHECK_UNKNOWN


def _attempt_error_code(last_attempt: dict) -> str:
    error_code = _clean_string(last_attempt.get("errorCode"))

    if error_code is None:
        return sync_engine.ERROR_UNKNOWN_ERROR

    return error_code


def _empty_checks(function_check: str, slu_check: str, data_check: str) -> dict:
    return {
        "function": function_check,
        "slu": slu_check,
        "data": data_check,
    }


def build_health_payload(
    syncstate: Optional[dict],
    *,
    now: Optional[datetime.datetime] = None,
    stale_after_seconds: Optional[int] = None,
) -> dict:
    """Build the stable health contract from an already-read syncstate."""

    threshold = (
        stale_after_seconds
        if stale_after_seconds is not None
        else get_stale_after_seconds()
    )

    if not isinstance(syncstate, dict):
        return {
            "status": STATUS_DEGRADED,
            "checks": _empty_checks(CHECK_OK, CHECK_UNKNOWN, CHECK_MISSING),
            "lastSuccessfulSyncAt": None,
            "lastAttemptAt": None,
            "syncAgeSeconds": None,
            "latestSourceModifiedAt": None,
            "reasonCode": REASON_NO_SUCCESSFUL_SYNC,
            "version": get_app_version(),
        }

    raw_last_attempt = syncstate.get("lastAttempt")
    last_attempt = raw_last_attempt if isinstance(raw_last_attempt, dict) else None

    last_successful_sync_at = _clean_string(
        syncstate.get("lastSuccessfulRunCompletedAt")
    )
    latest_source_modified_at = _clean_string(syncstate.get("latestSourceModifiedAt"))
    last_attempt_at = (
        _clean_string(last_attempt.get("attemptedAt")) if last_attempt else None
    )

    sync_age_seconds = compute_sync_age_seconds(last_successful_sync_at, now)
    data_check = classify_data_freshness(
        last_successful_sync_at,
        sync_age_seconds,
        threshold,
    )
    slu_check = classify_slu_status(last_attempt)

    if slu_check == CHECK_FAILING:
        status = STATUS_DEGRADED
        reason_code = _attempt_error_code(last_attempt)
    elif data_check == CHECK_MISSING:
        status = STATUS_DEGRADED
        reason_code = REASON_NO_SUCCESSFUL_SYNC
    elif data_check == CHECK_STALE:
        status = STATUS_DEGRADED
        reason_code = REASON_SYNC_STALE
    elif data_check == CHECK_UNKNOWN:
        status = STATUS_DEGRADED
        reason_code = REASON_INVALID_STATE
    else:
        status = STATUS_OK
        reason_code = REASON_OK

    return {
        "status": status,
        "checks": _empty_checks(CHECK_OK, slu_check, data_check),
        "lastSuccessfulSyncAt": last_successful_sync_at,
        "lastAttemptAt": last_attempt_at,
        "syncAgeSeconds": sync_age_seconds,
        "latestSourceModifiedAt": latest_source_modified_at,
        "reasonCode": reason_code,
        "version": get_app_version(),
    }


def build_error_payload(reason_code: str) -> dict:
    """Build a health payload for the case where state cannot be read."""

    return {
        "status": STATUS_ERROR,
        "checks": _empty_checks(CHECK_ERROR, CHECK_UNKNOWN, CHECK_UNKNOWN),
        "lastSuccessfulSyncAt": None,
        "lastAttemptAt": None,
        "syncAgeSeconds": None,
        "latestSourceModifiedAt": None,
        "reasonCode": reason_code,
        "version": get_app_version(),
    }


def build_health_response(
    *,
    syncstate_reader: Optional[SyncstateReader] = None,
    now: Optional[datetime.datetime] = None,
    stale_after_seconds: Optional[int] = None,
) -> tuple[dict, int]:
    """Return ``(payload, http_status_code)`` and never raise or leak details."""

    reader = syncstate_reader or read_syncstate

    try:
        syncstate = reader()
    except Exception as exc:
        error_code = sync_engine.classify_sync_error(exc)
        logging.exception("Health check could not read syncstate.json.")

        if error_code == sync_engine.ERROR_CONFIG_ERROR:
            reason_code = REASON_CONFIG_ERROR
        else:
            reason_code = REASON_STATE_UNAVAILABLE

        return build_error_payload(reason_code), HTTP_SERVICE_UNAVAILABLE

    payload = build_health_payload(
        syncstate,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )

    return payload, HTTP_OK
