#!/usr/bin/env python3
"""Unit tests for antigravity_proxy token handling, parsing, and server logic."""
import os
import sys
import time
import json
import unittest
from datetime import datetime, timezone, timedelta

# Import target module
import antigravity_proxy as proxy


class TestProxyTokenManagement(unittest.TestCase):

    def test_parse_expiry_formats(self):
        now = time.time()

        # 1. Float seconds
        self.assertAlmostEqual(proxy._parse_expiry(now), now, places=2)

        # 2. Integer seconds
        self.assertEqual(proxy._parse_expiry(1726900000), 1726900000.0)

        # 3. Milliseconds timestamp (> 1e11)
        ms = 1726900000123
        self.assertAlmostEqual(proxy._parse_expiry(ms), 1726900000.123, places=3)

        # 4. Numeric string seconds
        self.assertEqual(proxy._parse_expiry("1726900000"), 1726900000.0)

        # 5. Numeric string milliseconds
        self.assertAlmostEqual(proxy._parse_expiry("1726900000123"), 1726900000.123, places=3)

        # 6. RFC3339 with 'Z'
        rfc_z = "2026-09-21T12:00:00Z"
        expected = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(proxy._parse_expiry(rfc_z), expected)

        # 7. RFC3339 with nanoseconds and 'Z'
        rfc_ns = "2026-09-21T12:00:00.123456789Z"
        expected_sub = datetime(2026, 9, 21, 12, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(proxy._parse_expiry(rfc_ns), expected_sub, places=5)

        # 8. RFC3339 with offset '+00:00'
        rfc_offset = "2026-09-21T12:00:00+00:00"
        self.assertEqual(proxy._parse_expiry(rfc_offset), expected)

        # 9. Invalid or empty
        self.assertEqual(proxy._parse_expiry(""), 0.0)
        self.assertEqual(proxy._parse_expiry(None), 0.0)
        self.assertEqual(proxy._parse_expiry("not-a-date"), 0.0)

    def test_token_disk_io_and_dir_creation(self):
        test_dir = os.path.join(os.path.dirname(__file__), "test_tmp_token_dir")
        test_file = os.path.join(test_dir, "nested", "test-token.json")
        try:
            # Override TOKEN_FILE via env
            os.environ["ANTIGRAVITY_TOKEN_PATH"] = test_file

            data = {
                "auth_method": "oauth",
                "token": {
                    "access_token": "ya29.test_access_token",
                    "refresh_token": "1//test_refresh_token",
                    "token_type": "Bearer",
                    "expiry": "2026-09-21T15:00:00Z",
                }
            }

            proxy._write_token_to_disk(data)
            self.assertTrue(os.path.isfile(test_file))

            read_data = proxy._read_token_from_disk()
            self.assertIsNotNone(read_data)
            self.assertEqual(read_data["token"]["access_token"], "ya29.test_access_token")
            self.assertEqual(read_data["token"]["refresh_token"], "1//test_refresh_token")

        finally:
            if "ANTIGRAVITY_TOKEN_PATH" in os.environ:
                del os.environ["ANTIGRAVITY_TOKEN_PATH"]
            if os.path.isfile(test_file):
                os.remove(test_file)
            if os.path.isdir(os.path.dirname(test_file)):
                os.rmdir(os.path.dirname(test_file))
            if os.path.isdir(test_dir):
                os.rmdir(test_dir)

    def test_model_map_frontier_models(self):
        required_models = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-high",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.1-pro",
            "claude-sonnet-4.6",
            "claude-opus-4.6",
        ]
        for m in required_models:
            self.assertIn(m, proxy.MODEL_MAP, f"Model {m} must be in MODEL_MAP")

    def test_server_endpoints_and_cors(self):
        import threading
        from urllib.request import Request, urlopen

        server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        server.allow_reuse_address = True
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. Test OPTIONS for CORS
            req_opt = Request(f"{base_url}/v1/chat/completions", method="OPTIONS")
            with urlopen(req_opt) as resp:
                self.assertEqual(resp.status, 204)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
                self.assertIn("POST", resp.headers.get("Access-Control-Allow-Methods", ""))

            # 2. Test /health
            req_health = Request(f"{base_url}/health")
            with urlopen(req_health) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "ok")
                self.assertIn("auth", data)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

            # 3. Test /v1/models
            req_models = Request(f"{base_url}/v1/models")
            with urlopen(req_models) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data["object"], "list")
                model_ids = [m["id"] for m in data["data"]]
                self.assertIn("gemini-3.8-flash", model_ids)
                self.assertIn("claude-sonnet-4.6", model_ids)

        finally:
            server.shutdown()
            server.server_close()

    def test_multimodal_content_conversion(self):
        # 1. Plain string
        parts = proxy._content_to_parts("Hello world")
        self.assertEqual(parts, [{"text": "Hello world"}])

        # 2. OpenAI image_url with data URI
        raw_content = [
            {"type": "text", "text": "Describe this image:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="}}
        ]
        parts = proxy._content_to_parts(raw_content)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], {"text": "Describe this image:"})
        self.assertEqual(parts[1], {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgoAAAANSUhEUg=="}})

        # 3. OpenAI input_audio
        audio_content = [
            {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}
        ]
        parts = proxy._content_to_parts(audio_content)
        self.assertEqual(parts, [{"inlineData": {"mimeType": "audio/wav", "data": "UklGRg=="}}])

        # 4. Document / PDF file
        pdf_content = [
            {"type": "file", "file": {"data": "JVBERi0xLjQ=", "mime_type": "application/pdf"}}
        ]
        parts = proxy._content_to_parts(pdf_content)
        self.assertEqual(parts, [{"inlineData": {"mimeType": "application/pdf", "data": "JVBERi0xLjQ="}}])


if __name__ == "__main__":
    unittest.main()

