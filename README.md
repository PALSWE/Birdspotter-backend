# BirdSpotter Backend

Azure Functions backend for BirdSpotter.

## Current version

v1.0.0

## Status

Production-ready backend cache for BirdSpotter.

Features:

- Azure Functions
- Azure Blob Storage
- Full and delta synchronization
- Automatic day rollover
- Geospatial filtering endpoint
- Timer-based synchronization

## Features

- SOS SearchByCursor integration
- Full sync for today's bird observations
- Delta sync based on SOS `modifiedDate`
- Merge/upsert by `occurrenceId`
- Timer-triggered sync every 5 minutes
- Azure Blob Storage cache
  - `observations/today.json`
  - `observations/yesterday.json`
  - `metadata/syncstate.json`
- HTTP endpoints:
  - `GET /api/sync-today`
  - `GET /api/observations/today`
  - `GET /api/observations/yesterday`
  - `GET /api/observations/nearby` which takes parameter for lat, lon and radius
  - `GET /api/syncstate`

## Configuration

### Local development

Local development reads the SOS API key from the `SOS_API_KEY` environment
variable. When the app is not running in Azure, `.env` is loaded automatically.

Create `.env` from `.env.example` and set:

```bash
SOS_API_KEY=<your local SOS API key>
MAX_PAGES=10
```

### Azure Function App

In Azure, the Function App reads the SOS API key from Azure Key Vault using
`DefaultAzureCredential` and the Function App's Managed Identity.

Required Function App app settings:

```bash
KEY_VAULT_URL=https://kv-birdspotter.vault.azure.net/
SOS_API_KEY_SECRET_NAME=sos-api-key
```

`SOS_API_KEY_SECRET_NAME` is optional because the code defaults to `sos-api-key`,
but setting it explicitly makes the production configuration easier to inspect.

Do not keep `SOS_API_KEY` as a production Function App app setting after Key
Vault access has been configured.

Required Azure access:

1. Enable a system-assigned Managed Identity on the Function App.
2. Assign the Function App Managed Identity the `Key Vault Secrets User` role on
   the `kv-birdspotter` Key Vault scope.
3. If the Key Vault uses the legacy access policy model instead of Azure RBAC,
   grant the Managed Identity `Get` permission for secrets, or switch the vault
   to Azure RBAC and use the role assignment above.

Example Azure CLI commands:

```bash
az functionapp identity assign \
  --resource-group <resource-group> \
  --name <function-app-name>

principal_id=$(az functionapp identity show \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --query principalId \
  --output tsv)

vault_id=$(az keyvault show \
  --name kv-birdspotter \
  --query id \
  --output tsv)

az role assignment create \
  --assignee "$principal_id" \
  --role "Key Vault Secrets User" \
  --scope "$vault_id"

az functionapp config appsettings set \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --settings \
    KEY_VAULT_URL=https://kv-birdspotter.vault.azure.net/ \
    SOS_API_KEY_SECRET_NAME=sos-api-key
```

## Infrastructure as Code

Azure infrastructure is described in `infra/main.bicep`, with separate parameter
files for dev and prod. The template creates the Function App, Linux consumption
plan, runtime storage, BirdSpotter data storage, Application Insights, Key Vault,
Managed Identity, and the Key Vault RBAC role assignment.

Before deploying prod, review `infra/prod.parameters.json`. The `nameSuffix`
value is used in globally unique resource names such as Key Vault and runtime
storage. Change it if Azure reports a naming conflict.

Create the prod resource group if it does not already exist:

```bash
az group create \
  --name rg-birdspotter-prod \
  --location swedencentral
```

Run what-if before making changes:

```bash
az deployment group what-if \
  --resource-group rg-birdspotter-prod \
  --template-file infra/main.bicep \
  --parameters infra/prod.parameters.json
```

Create the prod resources:

```bash
az deployment group create \
  --resource-group rg-birdspotter-prod \
  --template-file infra/main.bicep \
  --parameters infra/prod.parameters.json
```

After deployment, add the `sos-api-key` secret value manually in the deployed Key
Vault. The template creates the Key Vault and grants the Function App Managed
Identity `Key Vault Secrets User`, but it does not create or store the secret
value.

The backend code is deployed separately to the Function App after infrastructure
deployment. The identity/RBAC deployment requires the deploying Azure user or
service principal to have permission to create role assignments, such as `Owner`
or `User Access Administrator`.
