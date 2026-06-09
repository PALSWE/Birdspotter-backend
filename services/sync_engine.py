import datetime
import os
from pathlib import Path
from typing import Optional

from services.geo import distance_km
from services.sos_client import build_search_body, call_sos_search_by_cursor
from storage.blob_storage import BlobStorage
from storage.file_storage import FileStorage

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"

TODAY_FILENAME = "today.json"
SYNCSTATE_FILENAME = "syncstate.json"
YESTERDAY_FILENAME = "yesterday.json"

MAX_PAGES = int(os.environ["MAX_PAGES"])
OVERLAP_SECONDS = 2


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
    occurrence = record.get("occurrence") or {}
    taxon = record.get("taxon") or {}
    taxon_attrs = taxon.get("attributes") or {}
    location = record.get("location") or {}
    event = record.get("event") or {}

    occurrence_id = occurrence.get("occurrenceId")
    observed_at = event.get("startDate")
    lat = location.get("decimalLatitude")
    lon = location.get("decimalLongitude")

    activity = ((occurrence.get("activity") or {}).get("value")) or (
        (occurrence.get("behavior") or {}).get("value")
    )

    comment = occurrence.get("occurrenceRemarks")
    has_comment = bool(comment and str(comment).strip())

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
        "observedAt": observed_at,
        "sourceModifiedAt": record.get("modified"),
        "reportedAt": occurrence.get("reportedDate"),
        "ingestedAt": utc_now_iso(),
        "latitude": lat,
        "longitude": lon,
        "locality": location.get("locality"),
        "municipality": (location.get("municipality") or {}).get("name"),
        "county": (location.get("county") or {}).get("name"),
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


def should_do_full_refresh(
    existing_today: Optional[dict],
    existing_syncstate: Optional[dict],
    start_date: str,
) -> bool:
    if not existing_today or not existing_syncstate:
        return True

    previous_start_date = (existing_syncstate.get("dateFilter") or {}).get("startDate")

    if previous_start_date != start_date:
        return True

    if not existing_syncstate.get("latestSourceModifiedAt"):
        return True

    return False


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
    api_key = os.environ.get("SOS_API_KEY")
    if not api_key:
        raise RuntimeError("SOS_API_KEY is missing")

    take = int(os.environ.get("SOS_TAKE", "2500"))
    max_pages = MAX_PAGES

    start_date, end_date = today_range_local()
    started_at = utc_now_iso()

    existing_today = storage.read_json(TODAY_FILENAME)
    existing_syncstate = storage.read_json(SYNCSTATE_FILENAME)
    existing_today, existing_syncstate, day_rotated = rotate_day_if_needed(
        existing_today,
        existing_syncstate,
        start_date,
    )

    full_refresh = should_do_full_refresh(
        existing_today,
        existing_syncstate,
        start_date,
    )

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
        existing_records = existing_today.get("records") or []

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

    merged_records = list(records_by_id.values())
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
        "mode": "full" if full_refresh else "delta",
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
    max_results: Optional[int] = None,
) -> Optional[dict]:
    today = storage.read_json(TODAY_FILENAME)

    if not today:
        return None

    matching_records = []

    for record in today.get("records") or []:
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
        "source": "today",
        "center": {
            "latitude": lat,
            "longitude": lon,
        },
        "radiusKm": radius_km,
        "recordCount": len(matching_records),
        "records": matching_records,
        "cacheGeneratedAt": today.get("generatedAt"),
        "cacheRecordCount": today.get("recordCount"),
    }
