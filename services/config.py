import logging
import os
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

PROJECT_ROOT = Path(__file__).parent.parent
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"

SOS_API_KEY_ENV = "SOS_API_KEY"
KEY_VAULT_URL_ENV = "KEY_VAULT_URL"
SOS_API_KEY_SECRET_NAME_ENV = "SOS_API_KEY_SECRET_NAME"
DEFAULT_SOS_API_KEY_SECRET_NAME = "sos-api-key"

_secret_clients: dict[str, Any] = {}


def is_running_in_azure() -> bool:
    return bool(
        os.environ.get("WEBSITE_INSTANCE_ID") or os.environ.get("WEBSITE_SITE_NAME")
    )


def load_local_env() -> None:
    if is_running_in_azure() or not LOCAL_ENV_FILE.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        logging.warning(
            "python-dotenv is not installed; local .env file was not loaded."
        )
        return

    load_dotenv(LOCAL_ENV_FILE, override=False)


def get_sos_api_key() -> str:
    load_local_env()

    if is_running_in_azure():
        return _get_sos_api_key_from_key_vault()

    api_key = os.environ.get(SOS_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{SOS_API_KEY_ENV} is missing. Add it to .env for local development."
        )

    return api_key


def _get_sos_api_key_from_key_vault() -> str:
    vault_url = os.environ.get(KEY_VAULT_URL_ENV)
    if not vault_url:
        logging.error(
            "Azure environment detected, but %s app setting is missing.",
            KEY_VAULT_URL_ENV,
        )
        raise RuntimeError(f"{KEY_VAULT_URL_ENV} app setting is missing.")

    secret_name = os.environ.get(
        SOS_API_KEY_SECRET_NAME_ENV,
        DEFAULT_SOS_API_KEY_SECRET_NAME,
    )

    try:
        secret = _get_secret_client(vault_url).get_secret(secret_name)
    except ResourceNotFoundError as exc:
        logging.exception(
            "Key Vault secret %s was not found in %s.",
            secret_name,
            vault_url,
        )
        raise RuntimeError(f"Key Vault secret {secret_name} was not found.") from exc
    except ClientAuthenticationError as exc:
        logging.exception(
            "Managed Identity authentication failed when reading Key Vault secret %s.",
            secret_name,
        )
        raise RuntimeError(
            "Managed Identity could not authenticate to Azure Key Vault."
        ) from exc
    except HttpResponseError as exc:
        logging.exception(
            "Key Vault request failed when reading secret %s. status_code=%s",
            secret_name,
            exc.status_code,
        )
        raise RuntimeError("Azure Key Vault rejected the secret request.") from exc
    except AzureError as exc:
        logging.exception(
            "Azure SDK failed when reading Key Vault secret %s.",
            secret_name,
        )
        raise RuntimeError("Azure Key Vault secret could not be read.") from exc

    if not secret.value:
        logging.error("Key Vault secret %s is empty.", secret_name)
        raise RuntimeError(f"Key Vault secret {secret_name} is empty.")

    return secret.value


def _get_secret_client(vault_url: str) -> Any:
    client = _secret_clients.get(vault_url)
    if client is not None:
        return client

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        logging.exception("Azure Key Vault dependencies are missing.")
        raise RuntimeError(
            "Azure Key Vault dependencies are missing. Install azure-identity and "
            "azure-keyvault-secrets."
        ) from exc

    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_url, credential=credential)
    _secret_clients[vault_url] = client

    return client
