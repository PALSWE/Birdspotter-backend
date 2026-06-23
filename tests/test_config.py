import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.core.exceptions import ResourceNotFoundError

from services import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        config._secret_clients.clear()
        self.local_env_file_patcher = patch.object(
            config,
            "LOCAL_ENV_FILE",
            Path("/tmp/birdspotter-test-missing.env"),
        )
        self.local_env_file_patcher.start()

    def tearDown(self):
        self.local_env_file_patcher.stop()

    def test_local_development_uses_sos_api_key_environment_variable(self):
        with patch.dict(os.environ, {"SOS_API_KEY": "local-key"}, clear=True):
            self.assertEqual(config.get_sos_api_key(), "local-key")

    def test_local_development_requires_sos_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SOS_API_KEY is missing"):
                config.get_sos_api_key()

    def test_azure_requires_key_vault_url_app_setting(self):
        with patch.dict(os.environ, {"WEBSITE_SITE_NAME": "birdspotter"}, clear=True):
            with self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "KEY_VAULT_URL"):
                    config.get_sos_api_key()

    def test_azure_reads_sos_api_key_from_key_vault(self):
        client = Mock()
        client.get_secret.return_value = SimpleNamespace(value="vault-key")

        with (
            patch.dict(
                os.environ,
                {
                    "WEBSITE_SITE_NAME": "birdspotter",
                    "KEY_VAULT_URL": "https://kv-birdspotter.vault.azure.net/",
                },
                clear=True,
            ),
            patch.object(config, "_get_secret_client", return_value=client),
        ):
            self.assertEqual(config.get_sos_api_key(), "vault-key")
            client.get_secret.assert_called_once_with(
                config.DEFAULT_SOS_API_KEY_SECRET_NAME
            )

    def test_azure_reports_missing_key_vault_secret(self):
        client = Mock()
        client.get_secret.side_effect = ResourceNotFoundError(message="missing")

        with (
            patch.dict(
                os.environ,
                {
                    "WEBSITE_SITE_NAME": "birdspotter",
                    "KEY_VAULT_URL": "https://kv-birdspotter.vault.azure.net/",
                },
                clear=True,
            ),
            patch.object(config, "_get_secret_client", return_value=client),
        ):
            with self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "sos-api-key was not found"):
                    config.get_sos_api_key()
