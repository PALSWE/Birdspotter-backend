import json

import azure.functions as func
import requests

from services.sync_engine import (
    read_syncstate_json,
    read_today_json,
    run_today_sync,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="sync-today-local", methods=["GET"])
def sync_today_local(req: func.HttpRequest) -> func.HttpResponse:
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

    return func.HttpResponse(
        json.dumps(result, ensure_ascii=False, indent=2),
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="observations/today", methods=["GET"])
def get_today_observations(req: func.HttpRequest) -> func.HttpResponse:
    content = read_today_json()

    if content is None:
        return func.HttpResponse(
            json.dumps(
                {
                    "success": False,
                    "error": "today.json does not exist. Run /api/sync-today-local first.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            status_code=404,
            mimetype="application/json",
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
        return func.HttpResponse(
            json.dumps(
                {
                    "success": False,
                    "error": "syncstate.json does not exist. Run /api/sync-today-local first.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            status_code=404,
            mimetype="application/json",
        )

    return func.HttpResponse(
        content,
        status_code=200,
        mimetype="application/json",
    )
