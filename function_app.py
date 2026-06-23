import gzip
import json
import logging
from typing import Any

import azure.functions as func
import requests

from services.sync_engine import (
    NEARBY_OBSERVATION_PERIODS,
    get_nearby_observations,
    read_syncstate_json,
    read_today_json,
    read_yesterday_json,
    run_today_sync,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

GZIP_MIN_BYTES = 1024


def accepts_gzip(req: func.HttpRequest) -> bool:
    accept_encoding = req.headers.get("Accept-Encoding", "")

    for encoding in accept_encoding.split(","):
        parts = [part.strip() for part in encoding.split(";")]
        if not parts or parts[0].lower() != "gzip":
            continue

        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0

        return quality > 0

    return False


def json_response(
    req: func.HttpRequest,
    payload: Any,
    status_code: int = 200,
    compress: bool = True,
) -> func.HttpResponse:
    if isinstance(payload, bytes):
        body = payload
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Vary": "Accept-Encoding",
    }

    if compress and len(body) > GZIP_MIN_BYTES and accepts_gzip(req):
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"

    headers["Content-Length"] = str(len(body))

    return func.HttpResponse(
        body=body,
        status_code=status_code,
        headers=headers,
    )


@app.route(
    route="observations/yesterday", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS
)
def get_yesterday_observations(req: func.HttpRequest) -> func.HttpResponse:
    content = read_yesterday_json()

    if content is None:
        return json_response(
            req,
            {
                "success": False,
                "error": "yesterday.json does not exist.",
            },
            status_code=404,
        )

    return json_response(req, content)


@app.route(route="sync-today", methods=["GET"])
def sync_today(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = run_today_sync()
    except RuntimeError as exc:
        return func.HttpResponse(str(exc), status_code=500)
    except requests.HTTPError as exc:
        return func.HttpResponse(
            exc.response.text,
            status_code=exc.response.status_code,
            mimetype="application/json",
        )

    return json_response(req, result, compress=False)


@app.timer_trigger(
    schedule="0 */5 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def sync_today_timer(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("sync_today_timer is past due.")

    try:
        result = run_today_sync()
        logging.info(
            "sync_today_timer completed. mode=%s storage=%s recordsFetched=%s recordsWritten=%s",
            result.get("mode"),
            result.get("storage"),
            result.get("recordsFetched"),
            result.get("recordsWritten"),
        )
    except Exception:
        logging.exception("sync_today_timer failed.")
        raise


@app.route(
    route="observations/today", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS
)
def get_today_observations(req: func.HttpRequest) -> func.HttpResponse:
    content = read_today_json()

    if content is None:
        return json_response(
            req,
            {
                "success": False,
                "error": "today.json does not exist. Run /api/sync-today first.",
            },
            status_code=404,
        )

    return json_response(req, content)


@app.route(
    route="observations/nearby", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS
)
def get_nearby(req: func.HttpRequest) -> func.HttpResponse:
    period = req.params.get("period", "today")

    if period not in NEARBY_OBSERVATION_PERIODS:
        return json_response(
            req,
            {
                "success": False,
                "error": "Invalid period. Accepted values are: today, yesterday.",
            },
            status_code=400,
        )

    try:
        lat = float(req.params["lat"])
        lon = float(req.params["lon"])
        radius_km = float(req.params["radiusKm"])
        max_results = (
            int(req.params["maxResults"]) if req.params.get("maxResults") else None
        )
    except KeyError:
        return json_response(
            req,
            {
                "success": False,
                "error": "Required query parameters: lat, lon, radiusKm.",
            },
            status_code=400,
        )
    except ValueError:
        return json_response(
            req,
            {
                "success": False,
                "error": "lat, lon, radiusKm and maxResults must be numeric.",
            },
            status_code=400,
        )

    if radius_km <= 0:
        return json_response(
            req,
            {
                "success": False,
                "error": "radiusKm must be greater than zero.",
            },
            status_code=400,
        )

    result = get_nearby_observations(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        period=period,
        max_results=max_results,
    )

    if result is None:
        filename = f"{period}.json"
        return json_response(
            req,
            {
                "success": False,
                "error": f"{filename} does not exist. Run /api/sync-today first.",
            },
            status_code=404,
        )

    return json_response(req, result)


@app.route(route="syncstate", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_syncstate(req: func.HttpRequest) -> func.HttpResponse:
    content = read_syncstate_json()

    if content is None:
        return json_response(
            req,
            {
                "success": False,
                "error": "syncstate.json does not exist. Run /api/sync-today first.",
            },
            status_code=404,
        )

    try:
        syncstate = json.loads(content)
    except json.JSONDecodeError:
        return json_response(
            req,
            {
                "success": False,
                "error": "syncstate.json contains invalid JSON.",
            },
            status_code=500,
        )

    latest_source_modified_at = syncstate.get("latestSourceModifiedAt")

    if not latest_source_modified_at:
        return json_response(
            req,
            {
                "success": False,
                "error": "syncstate.json does not contain latestSourceModifiedAt.",
            },
            status_code=500,
        )

    logging.info(
        "Returning latestSourceModifiedAt=%s",
        latest_source_modified_at,
    )

    return json_response(
        req,
        {
            "success": True,
            "latestSourceModifiedAt": latest_source_modified_at,
        }
    )
