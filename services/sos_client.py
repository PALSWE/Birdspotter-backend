import requests
from typing import Optional

SOS_ENDPOINT = (
    "https://api.artdatabanken.se/species-observation-system/v1/"
    "Observations/SearchByCursor"
)


def build_search_body(
    start_date: str,
    end_date: str,
    modified_from: Optional[str] = None,
) -> dict:
    body = {
        "occurrenceStatus": "Present",
        "verificationStatus": "BothVerifiedAndNotVerified",
        "taxon": {
            "ids": [4000104],
            "includeUnderlyingTaxa": True,
        },
        "date": {
            "startDate": start_date,
            "endDate": end_date,
            "dateFilterType": "OnlyStartDate",
        },
        "output": {
            "fields": [
                "occurrence.occurrenceId",
                "occurrence.individualCount",
                "occurrence.occurrenceRemarks",
                "occurrence.recordedBy",
                "occurrence.activity.value",
                "occurrence.behavior.value",
                "occurrence.reportedDate",
                "occurrence.url",
                "taxon.id",
                "taxon.vernacularName",
                "taxon.scientificName",
                "taxon.taxonRank",
                "taxon.order",
                "taxon.family",
                "taxon.genus",
                "taxon.attributes.redlistCategory",
                "taxon.attributes.sortOrder",
                "taxon.attributes.dyntaxaTaxonId",
                "location.decimalLatitude",
                "location.decimalLongitude",
                "location.locality",
                "location.municipality.name",
                "location.county.name",
                "event.startDate",
                "modified",
            ]
        },
    }

    if modified_from:
        body["modifiedDate"] = {
            "from": modified_from,
        }

    return body


def call_sos_search_by_cursor(
    api_key: str,
    body: dict,
    *,
    take: int,
    cursor: Optional[str] = None,
) -> dict:
    params = {
        "take": str(take),
        "sortBy": "modified",
        "sortOrder": "asc",
        "validateSearchFilter": "false",
        "sensitiveObservations": "false",
    }

    if cursor:
        params["cursor"] = cursor

    response = requests.post(
        SOS_ENDPOINT,
        headers={
            "X-Api-Version": "1.5",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
        },
        params=params,
        json=body,
        timeout=60,
    )

    response.raise_for_status()
    return response.json()
