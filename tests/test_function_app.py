import gzip
import json
import os
import unittest
from typing import Optional

import azure.functions as func

os.environ.setdefault("MAX_PAGES", "10")
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)

from function_app import GZIP_MIN_BYTES, json_response


def make_request(accept_encoding: Optional[str] = None) -> func.HttpRequest:
    headers = {}
    if accept_encoding is not None:
        headers["Accept-Encoding"] = accept_encoding

    return func.HttpRequest(
        method="GET",
        url="http://localhost/api/test",
        headers=headers,
        params={},
        route_params={},
        body=b"",
    )


class JsonResponseTests(unittest.TestCase):
    def test_without_gzip_accept_encoding_returns_uncompressed_json(self):
        payload = {"records": ["x" * GZIP_MIN_BYTES]}
        response = json_response(make_request(), payload)
        expected_body = json.dumps(payload, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )

        self.assertEqual(response.get_body(), expected_body)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(
            response.headers.get("Content-Type"),
            "application/json; charset=utf-8",
        )
        self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(
            response.headers.get("Content-Length"),
            str(len(expected_body)),
        )

    def test_with_gzip_and_large_payload_returns_decompressible_gzip(self):
        payload = {"records": ["x" * GZIP_MIN_BYTES]}
        response = json_response(make_request("gzip, br"), payload)
        expected_body = json.dumps(payload, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )

        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(response.get_body()), expected_body)
        self.assertEqual(
            response.headers.get("Content-Length"),
            str(len(response.get_body())),
        )

    def test_gzip_preserves_pre_serialized_json_exactly(self):
        payload = '{"records":["' + ("x" * GZIP_MIN_BYTES) + '"]}'
        response = json_response(make_request("gzip"), payload)

        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(response.get_body()), payload.encode("utf-8"))

    def test_small_payload_is_not_compressed_even_when_gzip_is_accepted(self):
        payload = {"success": True}
        response = json_response(make_request("gzip"), payload)
        expected_body = json.dumps(payload, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )

        self.assertEqual(response.get_body(), expected_body)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(
            response.headers.get("Content-Length"),
            str(len(expected_body)),
        )

    def test_compression_can_be_disabled_for_large_payload(self):
        payload = {"records": ["x" * GZIP_MIN_BYTES]}
        response = json_response(make_request("gzip"), payload, compress=False)
        expected_body = json.dumps(payload, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )

        self.assertEqual(response.get_body(), expected_body)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        self.assertEqual(response.headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(
            response.headers.get("Content-Length"),
            str(len(expected_body)),
        )


if __name__ == "__main__":
    unittest.main()
