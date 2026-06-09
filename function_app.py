import json
import logging

import azure.functions as func
import requests

from services.sync_engine import (
    read_syncstate_json,
    read_today_json,
    run_today_sync,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


def json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, indent=2),
        status_code=status_code,
        mimetype="application/json",
    )


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

    return json_response(result)


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


@app.route(route="observations/today", methods=["GET"])
def get_today_observations(req: func.HttpRequest) -> func.HttpResponse:
    content = read_today_json()

    if content is None:
        return json_response(
            {
                "success": False,
                "error": "today.json does not exist. Run /api/sync-today first.",
            },
            status_code=404,
        )

    return func.HttpResponse(
        content,
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="syncstate", methods=["GET"])
def get_syncstate(req: func.HttpRequest) -> func.HttpResponse:
    content = read_syncstate_json()

    if content is None:
        return json_response(
            {
                "success": False,
                "error": "syncstate.json does not exist. Run /api/sync-today first.",
            },
            status_code=404,
        )

    return func.HttpResponse(
        content,
        status_code=200,
        mimetype="application/json",
    )
