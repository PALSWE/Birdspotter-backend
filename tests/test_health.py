import datetime
import json
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("MAX_PAGES", "10")
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
os.environ.pop("HEALTH_SYNC_STALE_SECONDS", None)
os.environ.pop("APP_VERSION", None)

import azure.functions as func
import requests
from azure.core.exceptions import AzureError

from services import health, sync_engine

NOW = datetime.datetime(2026, 9, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
STALE_AFTER_SECONDS = 900


def iso_seconds_ago(seconds_ago: int) -> str:
    return (NOW - datetime.timedelta(seconds=seconds_ago)).isoformat()


def make_request() -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url="http://localhost/api/health",
        headers={},
        params={},
        route_params={},
        body=b"",
    )


def successful_syncstate(**overrides) -> dict:
    syncstate = {
        "schemaVersion": 1,
        "lastRunStartedAt": iso_seconds_ago(120),
        "lastSuccessfulRunCompletedAt": iso_seconds_ago(60),
        "latestSourceModifiedAt": iso_seconds_ago(90),
        "lastAttempt": {
            "attemptedAt": iso_seconds_ago(60),
            "outcome": sync_engine.SYNC_OUTCOME_SUCCESS,
            "errorCode": None,
        },
    }
    syncstate.update(overrides)
    return syncstate


class HealthPayloadTests(unittest.TestCase):
    def build(
        self,
        syncstate,
        stale_after_seconds: int = STALE_AFTER_SECONDS,
    ) -> dict:
        return health.build_health_payload(
            syncstate,
            now=NOW,
            stale_after_seconds=stale_after_seconds,
        )

    def test_fresh_function_and_fresh_successful_sync_is_ok(self):
        payload = self.build(successful_syncstate())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["checks"],
            {"function": "ok", "slu": "ok", "data": "fresh"},
        )
        self.assertEqual(payload["reasonCode"], "OK")
        self.assertEqual(payload["syncAgeSeconds"], 60)
        self.assertEqual(payload["lastSuccessfulSyncAt"], iso_seconds_ago(60))
        self.assertEqual(payload["lastAttemptAt"], iso_seconds_ago(60))
        self.assertEqual(payload["latestSourceModifiedAt"], iso_seconds_ago(90))
        self.assertEqual(payload["version"], "v1.3")
        self.assertEqual(payload["version"], health.DEFAULT_APP_VERSION)

    def test_failed_last_attempt_is_degraded_and_keeps_last_success(self):
        payload = self.build(
            successful_syncstate(
                lastAttempt={
                    "attemptedAt": iso_seconds_ago(30),
                    "outcome": sync_engine.SYNC_OUTCOME_FAILURE,
                    "errorCode": sync_engine.ERROR_SLU_TIMEOUT,
                }
            )
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["checks"]["function"], "ok")
        self.assertEqual(payload["checks"]["slu"], "failing")
        self.assertEqual(payload["checks"]["data"], "fresh")
        self.assertEqual(payload["reasonCode"], sync_engine.ERROR_SLU_TIMEOUT)
        self.assertEqual(payload["lastAttemptAt"], iso_seconds_ago(30))
        self.assertEqual(payload["lastSuccessfulSyncAt"], iso_seconds_ago(60))
        self.assertEqual(payload["syncAgeSeconds"], 60)

    def test_stale_successful_sync_is_degraded(self):
        payload = self.build(
            successful_syncstate(lastSuccessfulRunCompletedAt=iso_seconds_ago(1200))
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["checks"]["data"], "stale")
        self.assertEqual(payload["checks"]["slu"], "ok")
        self.assertEqual(payload["reasonCode"], "SYNC_STALE")
        self.assertEqual(payload["syncAgeSeconds"], 1200)

    def test_missing_state_is_degraded_but_not_an_error(self):
        for syncstate in (None, {}, []):
            with self.subTest(syncstate=syncstate):
                payload = self.build(syncstate)

                self.assertEqual(payload["status"], "degraded")
                self.assertEqual(payload["checks"]["function"], "ok")
                self.assertEqual(payload["checks"]["slu"], "unknown")
                self.assertEqual(payload["checks"]["data"], "missing")
                self.assertEqual(payload["reasonCode"], "NO_SUCCESSFUL_SYNC")
                self.assertIsNone(payload["syncAgeSeconds"])
                self.assertIsNone(payload["lastSuccessfulSyncAt"])
                self.assertIsNone(payload["lastAttemptAt"])
                self.assertIsNone(payload["latestSourceModifiedAt"])

    def test_state_without_last_attempt_reports_unknown_slu(self):
        syncstate = successful_syncstate()
        del syncstate["lastAttempt"]

        payload = self.build(syncstate)

        self.assertEqual(payload["checks"]["slu"], "unknown")
        self.assertEqual(payload["status"], "ok")

    def test_unparseable_success_timestamp_is_degraded(self):
        payload = self.build(
            successful_syncstate(lastSuccessfulRunCompletedAt="not-a-timestamp")
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["checks"]["data"], "unknown")
        self.assertEqual(payload["reasonCode"], "INVALID_STATE")

    def test_future_success_timestamp_is_clamped_to_zero(self):
        payload = self.build(
            successful_syncstate(
                lastSuccessfulRunCompletedAt=iso_seconds_ago(-120),
            )
        )

        self.assertEqual(payload["syncAgeSeconds"], 0)
        self.assertEqual(payload["checks"]["data"], "fresh")

    def test_failed_attempt_without_error_code_falls_back_to_unknown(self):
        payload = self.build(
            successful_syncstate(
                lastAttempt={
                    "attemptedAt": iso_seconds_ago(10),
                    "outcome": sync_engine.SYNC_OUTCOME_FAILURE,
                }
            )
        )

        self.assertEqual(payload["reasonCode"], sync_engine.ERROR_UNKNOWN_ERROR)


class HealthErrorClassificationTests(unittest.TestCase):
    def test_sync_error_classification_codes(self):
        response_401 = requests.Response()
        response_401.status_code = 401
        response_403 = requests.Response()
        response_403.status_code = 403
        response_500 = requests.Response()
        response_500.status_code = 500

        cases = [
            (requests.exceptions.Timeout(), sync_engine.ERROR_SLU_TIMEOUT),
            (requests.exceptions.ReadTimeout(), sync_engine.ERROR_SLU_TIMEOUT),
            (
                requests.exceptions.HTTPError(response=response_401),
                sync_engine.ERROR_SLU_AUTH_FAILED,
            ),
            (
                requests.exceptions.HTTPError(response=response_403),
                sync_engine.ERROR_SLU_AUTH_FAILED,
            ),
            (
                requests.exceptions.HTTPError(response=response_500),
                sync_engine.ERROR_SLU_HTTP_ERROR,
            ),
            (
                requests.exceptions.ConnectionError(),
                sync_engine.ERROR_SLU_NETWORK_ERROR,
            ),
            (
                requests.exceptions.RequestException(),
                sync_engine.ERROR_SLU_NETWORK_ERROR,
            ),
            (RuntimeError("missing"), sync_engine.ERROR_CONFIG_ERROR),
            (KeyError("MAX_PAGES"), sync_engine.ERROR_CONFIG_ERROR),
            (ValueError("bad int"), sync_engine.ERROR_CONFIG_ERROR),
            (AzureError("storage down"), sync_engine.ERROR_STORAGE_ERROR),
            (
                json.JSONDecodeError("bad", "{}", 0),
                sync_engine.ERROR_SLU_INVALID_RESPONSE,
            ),
            (Exception("boom"), sync_engine.ERROR_UNKNOWN_ERROR),
        ]

        for exc, expected_code in cases:
            with self.subTest(exc=type(exc).__name__, expected=expected_code):
                self.assertEqual(sync_engine.classify_sync_error(exc), expected_code)

    def test_unparseable_slu_response_is_not_classified_as_storage_error(self):
        exc = json.JSONDecodeError("Expecting value", "<html>502</html>", 0)

        error_code = sync_engine.classify_sync_error(exc)

        self.assertEqual(error_code, "SLU_INVALID_RESPONSE")
        self.assertEqual(error_code, sync_engine.ERROR_SLU_INVALID_RESPONSE)
        self.assertNotEqual(error_code, sync_engine.ERROR_STORAGE_ERROR)


class HealthResponseTests(unittest.TestCase):
    def test_valid_state_returns_200(self):
        reader = Mock(return_value=successful_syncstate())

        payload, status_code = health.build_health_response(
            syncstate_reader=reader,
            now=NOW,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "ok")

    def test_degraded_state_returns_200(self):
        reader = Mock(
            return_value=successful_syncstate(
                lastSuccessfulRunCompletedAt=iso_seconds_ago(9999)
            )
        )

        payload, status_code = health.build_health_response(
            syncstate_reader=reader,
            now=NOW,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "degraded")

    def test_storage_failure_returns_503_state_unavailable(self):
        reader = Mock(side_effect=AzureError("storage down"))

        with self.assertLogs(level="ERROR"):
            payload, status_code = health.build_health_response(
                syncstate_reader=reader,
                now=NOW,
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["checks"]["function"], "error")
        self.assertEqual(payload["checks"]["slu"], "unknown")
        self.assertEqual(payload["checks"]["data"], "unknown")
        self.assertEqual(payload["reasonCode"], "STATE_UNAVAILABLE")
        self.assertNotIn("storage down", json.dumps(payload))

    def test_configuration_failure_returns_503_config_error(self):
        secret = "super-secret-value"
        reader = Mock(side_effect=RuntimeError(f"KEY_VAULT_URL missing {secret}"))

        with self.assertLogs(level="ERROR"):
            payload, status_code = health.build_health_response(
                syncstate_reader=reader,
                now=NOW,
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reasonCode"], "CONFIG_ERROR")
        self.assertNotIn(secret, json.dumps(payload))

    def test_unexpected_failure_returns_503_without_leaking_details(self):
        reader = Mock(side_effect=Exception("raw internal detail"))

        with self.assertLogs(level="ERROR"):
            payload, status_code = health.build_health_response(
                syncstate_reader=reader,
                now=NOW,
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["reasonCode"], "STATE_UNAVAILABLE")
        self.assertNotIn("raw internal detail", json.dumps(payload))

    def test_health_payload_excludes_secrets_and_unexpected_fields(self):
        secret = "super-secret-api-key-value"
        syncstate = successful_syncstate(
            SOS_API_KEY=secret,
            AzureWebJobsStorage=f"AccountKey={secret}",
            lastAttempt={
                "attemptedAt": iso_seconds_ago(10),
                "outcome": sync_engine.SYNC_OUTCOME_FAILURE,
                "errorCode": sync_engine.ERROR_SLU_AUTH_FAILED,
                "responseBody": secret,
            },
        )

        payload, status_code = health.build_health_response(
            syncstate_reader=Mock(return_value=syncstate),
            now=NOW,
        )

        serialized = json.dumps(payload)

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["reasonCode"], sync_engine.ERROR_SLU_AUTH_FAILED)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("SOS_API_KEY", serialized)
        self.assertNotIn("AzureWebJobsStorage", serialized)
        self.assertNotIn("AccountKey", serialized)
        self.assertNotIn("responseBody", serialized)
        self.assertEqual(
            sorted(payload.keys()),
            sorted(
                [
                    "status",
                    "checks",
                    "lastSuccessfulSyncAt",
                    "lastAttemptAt",
                    "syncAgeSeconds",
                    "latestSourceModifiedAt",
                    "reasonCode",
                    "version",
                ]
            ),
        )


class StaleThresholdTests(unittest.TestCase):
    def test_default_threshold_is_used_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                health.get_stale_after_seconds(),
                health.DEFAULT_SYNC_STALE_SECONDS,
            )

    def test_configured_threshold_is_used(self):
        with patch.dict(os.environ, {"HEALTH_SYNC_STALE_SECONDS": "30"}, clear=True):
            payload = health.build_health_payload(successful_syncstate(), now=NOW)

            self.assertEqual(payload["checks"]["data"], "stale")
            self.assertEqual(payload["reasonCode"], "SYNC_STALE")

    def test_invalid_threshold_falls_back_to_default(self):
        for invalid_value in ("not-a-number", "0", "-5"):
            with self.subTest(value=invalid_value):
                with patch.dict(
                    os.environ,
                    {"HEALTH_SYNC_STALE_SECONDS": invalid_value},
                    clear=True,
                ):
                    with self.assertLogs(level="WARNING"):
                        self.assertEqual(
                            health.get_stale_after_seconds(),
                            health.DEFAULT_SYNC_STALE_SECONDS,
                        )

    def test_blank_threshold_is_treated_as_unset(self):
        with patch.dict(
            os.environ,
            {"HEALTH_SYNC_STALE_SECONDS": "   "},
            clear=True,
        ):
            self.assertEqual(
                health.get_stale_after_seconds(),
                health.DEFAULT_SYNC_STALE_SECONDS,
            )

    def test_app_version_env_override(self):
        with patch.dict(os.environ, {"APP_VERSION": "v9.9"}, clear=True):
            self.assertEqual(health.get_app_version(), "v9.9")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(health.get_app_version(), health.DEFAULT_APP_VERSION)

    def test_default_app_version_is_v1_3(self):
        self.assertEqual(health.DEFAULT_APP_VERSION, "v1.3")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(health.get_app_version(), "v1.3")

    def test_health_payload_reports_default_app_version(self):
        with patch.dict(os.environ, {}, clear=True):
            payload = health.build_health_payload(successful_syncstate(), now=NOW)

        self.assertEqual(payload["version"], "v1.3")
        self.assertEqual(payload["version"], health.DEFAULT_APP_VERSION)


class FailedAttemptPersistenceTests(unittest.TestCase):
    def test_failed_attempt_preserves_last_successful_sync_state(self):
        previous_success_at = "2026-06-22T08:54:23.603729+00:00"
        previous_source_modified_at = "2026-06-22T10:49:34.237+02:00"
        fake_storage = Mock()
        fake_storage.read_json.return_value = {
            "schemaVersion": 1,
            "lastRunStartedAt": "2026-06-22T08:54:22.627799+00:00",
            "lastSuccessfulRunCompletedAt": previous_success_at,
            "latestSourceModifiedAt": previous_source_modified_at,
            "recordsWritten": 1798,
        }

        with patch.object(sync_engine, "storage", fake_storage):
            written = sync_engine.record_failed_sync_attempt(
                "2026-09-10T12:00:00+00:00",
                sync_engine.ERROR_SLU_TIMEOUT,
            )

        self.assertTrue(written)
        fake_storage.write_json.assert_called_once()

        filename, payload = fake_storage.write_json.call_args.args

        self.assertEqual(filename, sync_engine.SYNCSTATE_FILENAME)
        self.assertEqual(payload["lastSuccessfulRunCompletedAt"], previous_success_at)
        self.assertEqual(payload["latestSourceModifiedAt"], previous_source_modified_at)
        self.assertEqual(payload["recordsWritten"], 1798)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(
            payload["lastAttempt"],
            {
                "attemptedAt": "2026-09-10T12:00:00+00:00",
                "outcome": sync_engine.SYNC_OUTCOME_FAILURE,
                "errorCode": sync_engine.ERROR_SLU_TIMEOUT,
            },
        )

    def test_failed_attempt_on_missing_state_writes_only_last_attempt(self):
        fake_storage = Mock()
        fake_storage.read_json.return_value = None

        with patch.object(sync_engine, "storage", fake_storage):
            written = sync_engine.record_failed_sync_attempt(
                "2026-09-10T12:00:00+00:00",
                sync_engine.ERROR_CONFIG_ERROR,
            )

        self.assertTrue(written)
        _, payload = fake_storage.write_json.call_args.args

        self.assertEqual(sorted(payload.keys()), ["lastAttempt"])
        self.assertNotIn("lastSuccessfulRunCompletedAt", payload)

    def test_write_failure_is_non_fatal(self):
        fake_storage = Mock()
        fake_storage.read_json.return_value = {
            "lastSuccessfulRunCompletedAt": "2026-06-22T08:54:23.603729+00:00"
        }
        fake_storage.write_json.side_effect = AzureError("storage down")

        with patch.object(sync_engine, "storage", fake_storage):
            with self.assertLogs(level="ERROR"):
                written = sync_engine.record_failed_sync_attempt(
                    "2026-09-10T12:00:00+00:00",
                    sync_engine.ERROR_STORAGE_ERROR,
                )

        self.assertFalse(written)

    def test_read_failure_does_not_write(self):
        fake_storage = Mock()
        fake_storage.read_json.side_effect = AzureError("storage down")

        with patch.object(sync_engine, "storage", fake_storage):
            with self.assertLogs(level="ERROR"):
                written = sync_engine.record_failed_sync_attempt(
                    "2026-09-10T12:00:00+00:00",
                    sync_engine.ERROR_STORAGE_ERROR,
                )

        self.assertFalse(written)
        fake_storage.write_json.assert_not_called()

    def test_run_today_sync_records_failed_attempt_and_reraises(self):
        previous_success_at = "2026-06-22T08:54:23.603729+00:00"
        fake_storage = Mock()
        fake_storage.read_json.return_value = {
            "lastSuccessfulRunCompletedAt": previous_success_at,
            "latestSourceModifiedAt": "2026-06-22T10:49:34.237+02:00",
        }

        with (
            patch.object(sync_engine, "storage", fake_storage),
            patch.object(
                sync_engine,
                "get_sos_api_key",
                side_effect=RuntimeError("SOS_API_KEY is missing."),
            ),
            self.assertLogs(level="ERROR"),
        ):
            with self.assertRaises(RuntimeError):
                sync_engine.run_today_sync()

        fake_storage.write_json.assert_called_once()
        filename, payload = fake_storage.write_json.call_args.args

        self.assertEqual(filename, sync_engine.SYNCSTATE_FILENAME)
        self.assertEqual(
            payload["lastAttempt"]["outcome"], sync_engine.SYNC_OUTCOME_FAILURE
        )
        self.assertEqual(
            payload["lastAttempt"]["errorCode"], sync_engine.ERROR_CONFIG_ERROR
        )
        self.assertEqual(payload["lastSuccessfulRunCompletedAt"], previous_success_at)


class HealthEndpointRegistrationTests(unittest.TestCase):
    def test_health_route_is_registered(self):
        import function_app

        functions = {
            function.get_function_name(): function
            for function in function_app.app.get_functions()
        }

        self.assertIn("get_health", functions)

        trigger = functions["get_health"].get_trigger()

        self.assertEqual(trigger.route, "health")
        self.assertEqual(trigger.auth_level, func.AuthLevel.FUNCTION)
        self.assertEqual([method.value for method in trigger.methods], ["GET"])

    def test_health_endpoint_returns_json_response(self):
        import function_app

        expected = {"status": "ok", "reasonCode": "OK"}

        with patch.object(
            function_app,
            "build_health_response",
            return_value=(expected, 200),
        ):
            response = function_app.get_health(make_request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.get_body()), expected)


class SanitizedReaderTests(unittest.TestCase):
    def test_read_syncstate_uses_shared_storage(self):
        fake_storage = Mock()
        fake_storage.read_json.return_value = {"ok": True}

        with patch.object(sync_engine, "storage", fake_storage):
            self.assertEqual(health.read_syncstate(), {"ok": True})

        fake_storage.read_json.assert_called_once_with(sync_engine.SYNCSTATE_FILENAME)


if __name__ == "__main__":
    unittest.main()
