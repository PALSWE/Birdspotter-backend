# BirdSpotter Backend

Azure Functions backend for BirdSpotter.

## Current version

v0.5.0

## Features

- SOS SearchByCursor integration
- Full sync for today's bird observations
- Delta sync based on SOS `modifiedDate`
- Merge/upsert by `occurrenceId`
- Timer-triggered sync every 5 minutes
- Azure Blob Storage cache
  - `observations/today.json`
  - `metadata/syncstate.json`
- HTTP endpoints:
  - `GET /api/sync-today`
  - `GET /api/observations/today`
  - `GET /api/syncstate`

## Configuration

Create `local.settings.json` from `local.settings.example.json`.

Required settings:

```json
{
  "Values": {
    "SOS_API_KEY": "<your SOS API key>",
    "MAX_PAGES": "10",
    "SOS_TAKE": "2500",
    "AZURE_STORAGE_CONNECTION_STRING": "<storage connection string>",
    "OBSERVATIONS_CONTAINER_NAME": "observations",
    "METADATA_CONTAINER_NAME": "metadata"
  }
}
```
