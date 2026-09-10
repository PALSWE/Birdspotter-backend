import datetime
import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests
from azure.core.exceptions import AzureError

from services.config import get_sos_api_key, load_local_env
from services.geo import distance_km
from services.sos_client import build_search_body, call_sos_search_by_cursor
from storage.blob_storage import BlobStorage
from storage.file_storage import FileStorage

load_local_env()

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
METADATA_DIR = PROJECT_ROOT / "metadata"
TAXON_FAMILY_SWEDISH_FILENAME = "taxon_family_sv.json"

TODAY_FILENAME = "today.json"
SYNCSTATE_FILENAME = "syncstate.json"
YESTERDAY_FILENAME = "yesterday.json"
NEARBY_OBSERVATION_PERIODS = ("today", "yesterday")
NEARBY_OBSERVATION_FILENAMES = {
    "today": TODAY_FILENAME,
    "yesterday": YESTERDAY_FILENAME,
}

MAX_PAGES = int(os.environ["MAX_PAGES"])
OVERLAP_SECONDS = 2

SYNC_OUTCOME_SUCCESS = "success"
SYNC_OUTCOME_FAILURE = "failure"

ERROR_SLU_TIMEOUT = "SLU_TIMEOUT"
ERROR_SLU_AUTH_FAILED = "SLU_AUTH_FAILED"
ERROR_SLU_HTTP_ERROR = "SLU_HTTP_ERROR"
ERROR_SLU_INVALID_RESPONSE = "SLU_INVALID_RESPONSE"
ERROR_SLU_NETWORK_ERROR = "SLU_NETWORK_ERROR"
ERROR_CONFIG_ERROR = "CONFIG_ERROR"
ERROR_STORAGE_ERROR = "STORAGE_ERROR"
ERROR_UNKNOWN_ERROR = "UNKNOWN_ERROR"

SLU_AUTH_FAILED_STATUS_CODES = frozenset({401, 403})

TAXON_EXTENSION_FIELDS = (
    "taxonSortOrder",
    "taxonDyntaxaId",
    "taxonOrder",
    "taxonFamily",
    "taxonGenus",
    "taxonScientificName",
    "taxonRank",
    "taxonFamilySwedish",
)


def load_taxon_family_swedish_names() -> dict:
    path = METADATA_DIR / TAXON_FAMILY_SWEDISH_FILENAME

    with path.open(encoding="utf-8") as file:
        return json.load(file)


TAXON_FAMILY_SWEDISH_NAMES = load_taxon_family_swedish_names()


def create_storage():
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")

    if connection_string:
        return BlobStorage(
            connection_string=connection_string,
            observations_container_name=os.environ["OBSERVATIONS_CONTAINER_NAME"],
            metadata_container_name=os.environ["METADATA_CONTAINER_NAME"],
        )

    return FileStorage(DATA_DIR)


storage = create_storage()


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def with_taxon_extension_fields(record: dict) -> dict:
    enriched_record = dict(record)

    for field in TAXON_EXTENSION_FIELDS:
        enriched_record.setdefault(field, None)

    enriched_record["taxonFamilySwedish"] = taxon_family_swedish_name(
        enriched_record.get("taxonFamily")
    )

    return enriched_record


def taxon_family_swedish_name(taxon_family: Optional[str]) -> Optional[str]:
    if not taxon_family:
        return None

    return TAXON_FAMILY_SWEDISH_NAMES.get(taxon_family)


def has_taxon_extension_fields(record: dict) -> bool:
    return all(field in record for field in TAXON_EXTENSION_FIELDS)


def cached_records_have_taxon_extension_fields(existing_today: dict) -> bool:
    records = existing_today.get("records") or []

    return all(
        isinstance(record, dict) and has_taxon_extension_fields(record)
        for record in records
    )


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def today_range_local() -> tuple[str, str]:
    today = datetime.date.today()
    return f"{today.isoformat()}T00:00:00", f"{today.isoformat()}T23:59:59"


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")

    try:
        return datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def apply_overlap(value: str) -> str:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return value

    return (parsed - datetime.timedelta(seconds=OVERLAP_SECONDS)).isoformat()


def normalize_record(record: dict) -> dict:
    occurrence = as_dict(record.get("occurrence"))
    taxon = as_dict(record.get("taxon"))
    taxon_attrs = as_dict(taxon.get("attributes"))
    location = as_dict(record.get("location"))
    event = as_dict(record.get("event"))

    occurrence_id = occurrence.get("occurrenceId")
    observed_at = event.get("startDate")
    lat = location.get("decimalLatitude")
    lon = location.get("decimalLongitude")

    activity = as_dict(occurrence.get("activity")).get("value") or as_dict(
        occurrence.get("behavior")
    ).get("value")

    comment = occurrence.get("occurrenceRemarks")
    has_comment = bool(comment and str(comment).strip())
    taxon_family = taxon.get("family")

    fallback_id = (
        f"{taxon.get('vernacularName')}|"
        f"{taxon.get('scientificName')}|"
        f"{observed_at}|{lat}|{lon}"
    )

    return {
        "schemaVersion": 1,
        "id": occurrence_id or fallback_id,
        "sourceOccurrenceId": occurrence_id,
        "taxonId": taxon.get("id"),
        "commonName": taxon.get("vernacularName"),
        "scientificName": taxon.get("scientificName"),
        "taxonSortOrder": taxon_attrs.get("sortOrder"),
        "taxonDyntaxaId": taxon_attrs.get("dyntaxaTaxonId"),
        "taxonOrder": taxon.get("order"),
        "taxonFamily": taxon_family,
        "taxonFamilySwedish": taxon_family_swedish_name(taxon_family),
        "taxonGenus": taxon.get("genus"),
        "taxonScientificName": taxon.get("scientificName"),
        "taxonRank": taxon.get("taxonRank"),
        "observedAt": observed_at,
        "sourceModifiedAt": record.get("modified"),
        "reportedAt": occurrence.get("reportedDate"),
        "ingestedAt": utc_now_iso(),
        "latitude": lat,
        "longitude": lon,
        "locality": location.get("locality"),
        "municipality": as_dict(location.get("municipality")).get("name"),
        "county": as_dict(location.get("county")).get("name"),
        "individualCount": occurrence.get("individualCount"),
        "activity": activity,
        "observerName": occurrence.get("recordedBy"),
        "comment": comment.strip() if isinstance(comment, str) else comment,
        "hasComment": has_comment,
        "redlistCategory": taxon_attrs.get("redlistCategory"),
        "sourceUrl": occurrence.get("url"),
    }


def get_latest_source_modified_at(records: list[dict]) -> Optional[str]:
    latest_dt = None
    latest_value = None

    for record in records:
        value = record.get("sourceModifiedAt")
        parsed = parse_iso_datetime(value)

        if parsed and (latest_dt is None or parsed > latest_dt):
            latest_dt = parsed
            latest_value = value

    return latest_value


def get_full_refresh_reason(
    existing_today: Optional[dict],
    existing_syncstate: Optional[dict],
    start_date: str,
) -> Optional[str]:
    if not existing_today or not existing_syncstate:
        return "missing_cache"

    previous_start_date = (existing_syncstate.get("dateFilter") or {}).get("startDate")

    if previous_start_date != start_date:
        return "new_day"

    if not existing_syncstate.get("latestSourceModifiedAt"):
        return "missing_latest_source_modified_at"

    if not cached_records_have_taxon_extension_fields(existing_today):
        return "missing_taxon_extension_fields"

    return None


def should_do_full_refresh(
    existing_today: Optional[dict],
    existing_syncstate: Optional[dict],
    start_date: str,
) -> bool:
    return (
        get_full_refresh_reason(
            existing_today,
            existing_syncstate,
            start_date,
        )
        is not None
    )


def rotate_day_if_needed(
    existing_today: Optional[dict],
    existing_syncstate: Optional[dict],
    start_date: str,
) -> tuple[Optional[dict], Optional[dict], bool]:
    if not existing_today or not existing_syncstate:
        return existing_today, existing_syncstate, False

    previous_start_date = (existing_syncstate.get("dateFilter") or {}).get("startDate")

    if previous_start_date == start_date:
        return existing_today, existing_syncstate, False

    storage.write_json(
        YESTERDAY_FILENAME,
        existing_today,
        pretty=False,
    )

    return None, None, True


def run_today_sync() -> dict:
    started_at = utc_now_iso()

    try:
        return _execute_today_sync(started_at)
    except Exception as exc:
        error_code = classify_sync_error(exc)
        logging.exception("Today sync attempt failed. errorCode=%s", error_code)
        record_failed_sync_attempt(started_at, error_code)
        raise


def _execute_today_sync(started_at: str) -> dict:
    api_key = get_sos_api_key()

    take = int(os.environ.get("SOS_TAKE", "2500"))
    max_pages = MAX_PAGES

    start_date, end_date = today_range_local()

    existing_today = storage.read_json(TODAY_FILENAME)
    existing_syncstate = storage.read_json(SYNCSTATE_FILENAME)
    existing_today, existing_syncstate, day_rotated = rotate_day_if_needed(
        existing_today,
        existing_syncstate,
        start_date,
    )

    full_refresh_reason = get_full_refresh_reason(
        existing_today,
        existing_syncstate,
        start_date,
    )
    full_refresh = full_refresh_reason is not None

    if full_refresh:
        logging.info("Today sync running full refresh. reason=%s", full_refresh_reason)

    modified_from = None
    if not full_refresh:
        latest_source_modified_at = existing_syncstate["latestSourceModifiedAt"]
        modified_from = apply_overlap(latest_source_modified_at)

    body = build_search_body(
        start_date,
        end_date,
        modified_from=modified_from,
    )

    fetched_records = []
    cursor = None
    pages_fetched = 0
    total_count = None

    while True:
        if max_pages is not None and pages_fetched >= max_pages:
            break

        payload = call_sos_search_by_cursor(
            api_key,
            body,
            take=take,
            cursor=cursor,
        )

        pages_fetched += 1
        total_count = payload.get("totalCount", total_count)

        raw_records = payload.get("records") or []
        fetched_records.extend(normalize_record(record) for record in raw_records)

        cursor = payload.get("nextCursor")

        if not cursor or not raw_records:
            break

    existing_records = []
    if not full_refresh:
        existing_records = [
            with_taxon_extension_fields(record)
            for record in existing_today.get("records") or []
            if isinstance(record, dict)
        ]

    records_by_id = {
        record["id"]: record for record in existing_records if record.get("id")
    }

    added_count = 0
    updated_count = 0

    for record in fetched_records:
        record_id = record.get("id")
        if not record_id:
            continue

        if record_id in records_by_id:
            updated_count += 1
        else:
            added_count += 1

        records_by_id[record_id] = record

    merged_records = [
        with_taxon_extension_fields(record) for record in records_by_id.values()
    ]
    latest_source_modified_at = get_latest_source_modified_at(merged_records)
    generated_at = utc_now_iso()

    output = {
        "schemaVersion": 1,
        "period": "today",
        "dateFilter": {
            "startDate": start_date,
            "endDate": end_date,
            "dateFilterType": "OnlyStartDate",
        },
        "generatedAt": generated_at,
        "recordCount": len(merged_records),
        "records": merged_records,
    }

    syncstate = {
        "schemaVersion": 1,
        "lastRunStartedAt": started_at,
        "lastSuccessfulRunCompletedAt": generated_at,
        "lastAttempt": {
            "attemptedAt": started_at,
            "outcome": SYNC_OUTCOME_SUCCESS,
            "errorCode": None,
        },
        "mode": "full" if full_refresh else "delta",
        "fullRefreshReason": full_refresh_reason,
        "period": "today",
        "dayRotated": day_rotated,
        "dateFilter": {
            "startDate": start_date,
            "endDate": end_date,
            "dateFilterType": "OnlyStartDate",
        },
        "modifiedDateFilter": {"from": modified_from} if modified_from else None,
        "pagesFetched": pages_fetched,
        "recordsFetched": len(fetched_records),
        "recordsAdded": added_count,
        "recordsUpdated": updated_count,
        "recordsWritten": len(merged_records),
        "sosTotalCount": total_count,
        "latestSourceModifiedAt": latest_source_modified_at,
        "lastCursor": cursor,
        "completedFullCursorScan": not cursor,
        "maxPages": max_pages,
        "take": take,
        "storage": "blob"
        if os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        else "file",
    }

    storage.write_json(TODAY_FILENAME, output, pretty=False)
    storage.write_json(SYNCSTATE_FILENAME, syncstate, pretty=True)

    return {
        "success": True,
        "mode": "full" if full_refresh else "delta",
        "storage": syncstate["storage"],
        "todayFile": TODAY_FILENAME,
        "syncstateFile": SYNCSTATE_FILENAME,
        "dayRotated": day_rotated,
        "pagesFetched": pages_fetched,
        "recordsFetched": len(fetched_records),
        "recordsAdded": added_count,
        "recordsUpdated": updated_count,
        "recordsWritten": len(merged_records),
        "sosTotalCount": total_count,
        "latestSourceModifiedAt": latest_source_modified_at,
        "completedFullCursorScan": not cursor,
        "remainingCursor": cursor,
    }


def classify_sync_error(exc: BaseException) -> str:
    if isinstance(exc, requests.exceptions.Timeout):
        return ERROR_SLU_TIMEOUT

    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)

        if status_code in SLU_AUTH_FAILED_STATUS_CODES:
            return ERROR_SLU_AUTH_FAILED

        return ERROR_SLU_HTTP_ERROR

    if isinstance(exc, requests.exceptions.RequestException):
        return ERROR_SLU_NETWORK_ERROR

    if isinstance(exc, json.JSONDecodeError):
        return ERROR_SLU_INVALID_RESPONSE

    if isinstance(exc, AzureError):
        return ERROR_STORAGE_ERROR

    if isinstance(exc, (RuntimeError, KeyError, ValueError)):
        return ERROR_CONFIG_ERROR

    return ERROR_UNKNOWN_ERROR


def record_failed_sync_attempt(attempted_at: str, error_code: str) -> bool:
    """Persist a failed sync attempt without touching previously successful state.

    Only the ``lastAttempt`` block is replaced. Successful fields such as
    ``lastSuccessfulRunCompletedAt`` and ``latestSourceModifiedAt``, and the
    cache files, are left untouched. Returns ``False`` when the state could not
    be updated and never raises.
    """

    try:
        existing_syncstate = storage.read_json(SYNCSTATE_FILENAME)
    except Exception:
        logging.exception(
            "Could not read %s while recording a failed sync attempt.",
            SYNCSTATE_FILENAME,
        )
        return False

    syncstate = dict(existing_syncstate) if isinstance(existing_syncstate, dict) else {}
    syncstate["lastAttempt"] = {
        "attemptedAt": attempted_at,
        "outcome": SYNC_OUTCOME_FAILURE,
        "errorCode": error_code,
    }

    try:
        storage.write_json(SYNCSTATE_FILENAME, syncstate, pretty=True)
    except Exception:
        logging.exception(
            "Could not write failed sync attempt to %s.",
            SYNCSTATE_FILENAME,
        )
        return False

    return True


def read_today_json() -> Optional[str]:
    return storage.read_text(TODAY_FILENAME)


def read_yesterday_json() -> Optional[str]:
    return storage.read_text(YESTERDAY_FILENAME)


def read_syncstate_json() -> Optional[str]:
    return storage.read_text(SYNCSTATE_FILENAME)


def get_nearby_observations(
    lat: float,
    lon: float,
    radius_km: float,
    period: str = "today",
    max_results: Optional[int] = None,
) -> Optional[dict]:
    filename = NEARBY_OBSERVATION_FILENAMES.get(period)
    if filename is None:
        raise ValueError(f"Unsupported observation period: {period}")

    cached_observations = storage.read_json(filename)

    if not cached_observations:
        return None

    matching_records = []

    for record in cached_observations.get("records") or []:
        distance = distance_km(
            lat,
            lon,
            record.get("latitude"),
            record.get("longitude"),
        )

        if distance is None or distance > radius_km:
            continue

        enriched_record = dict(record)
        enriched_record["distanceKm"] = round(distance, 3)
        matching_records.append(enriched_record)

    matching_records.sort(key=lambda item: item["distanceKm"])

    if max_results is not None:
        matching_records = matching_records[:max_results]

    return {
        "schemaVersion": 1,
        "source": period,
        "period": period,
        "center": {
            "latitude": lat,
            "longitude": lon,
        },
        "radiusKm": radius_km,
        "recordCount": len(matching_records),
        "records": matching_records,
        "cacheGeneratedAt": cached_observations.get("generatedAt"),
        "cacheRecordCount": cached_observations.get("recordCount"),
    }
