from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import requests

import mf_client
from mf_auth import OAuthError, Settings


def response(status: int = 200, *, headers=None, body=None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = json.dumps(body or {"items": []}).encode("utf-8")
    result.url = "https://example.invalid/api?client_secret=must-not-log"
    return result


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings("fake-id", "fake-secret", "http://localhost:8000/callback")
        self.settings_patch = patch.object(mf_client, "load_settings", return_value=settings)
        self.token_patch = patch.object(mf_client, "get_valid_token",
                                       return_value={"access_token": "fake-access"})
        self.settings_patch.start()
        self.token_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.token_patch.stop)
        self.client = mf_client.MoneyForwardClient()

    def test_429_prioritizes_retry_after_seconds(self) -> None:
        with (patch.object(mf_client.requests, "request", side_effect=[
                response(429, headers={"Retry-After": "7"}), response()]) as request,
              patch.object(mf_client.time, "sleep") as sleep):
            self.client.get("https://example.invalid", params={"page": 2})
        sleep.assert_called_once_with(7.0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["params"], {"page": 2})
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_retry_after_http_date(self) -> None:
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        item = response(429, headers={"Retry-After": format_datetime(now + timedelta(seconds=9))})
        with patch.object(mf_client, "datetime") as clock:
            clock.now.return_value = now
            self.assertEqual(mf_client.retry_delay(item, 0), 9.0)

    def test_invalid_retry_after_uses_backoff(self) -> None:
        for value in ("invalid", "NaN", "inf", "-1"):
            with self.subTest(value=value):
                self.assertEqual(mf_client.retry_delay(
                    response(429, headers={"Retry-After": value}), 2), 4.0)

    def test_all_transient_statuses_retry(self) -> None:
        for status in (500, 502, 503, 504):
            with (self.subTest(status=status),
                  patch.object(mf_client.requests, "request", side_effect=[response(status), response()]) as request,
                  patch.object(mf_client.time, "sleep") as sleep):
                self.client.get("https://example.invalid")
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(1.0)

    def test_timeout_and_connection_error_retry(self) -> None:
        for error in (requests.Timeout("secret body"), requests.ConnectionError("secret body")):
            with (self.subTest(error=type(error).__name__),
                  patch.object(mf_client.requests, "request", side_effect=[error, response()]),
                  patch.object(mf_client.time, "sleep") as sleep):
                self.assertEqual(self.client.get("https://example.invalid"), {"items": []})
                sleep.assert_called_once_with(1.0)

    def test_retry_limit_and_exponential_delays(self) -> None:
        with (patch.object(mf_client.requests, "request", return_value=response(503)) as request,
              patch.object(mf_client.time, "sleep") as sleep):
            with self.assertRaisesRegex(OAuthError, "HTTP 503"):
                self.client.get("https://example.invalid")
        self.assertEqual(request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 4.0])

    def test_connection_retry_limit_has_no_secret_error_text(self) -> None:
        with (patch.object(mf_client.requests, "request",
                           side_effect=requests.ConnectionError("fake-access fake-secret")) as request,
              patch.object(mf_client.time, "sleep")):
            with self.assertRaises(OAuthError) as caught:
                self.client.get("https://example.invalid")
        self.assertEqual(request.call_count, 4)
        self.assertNotIn("fake-access", str(caught.exception))
        self.assertNotIn("fake-secret", str(caught.exception))

    def test_permanent_errors_do_not_retry_or_expose_body(self) -> None:
        for status in (400, 403, 404):
            with (self.subTest(status=status),
                  patch.object(mf_client.requests, "request", return_value=response(
                      status, body={"client_secret": "fake-secret"})) as request,
                  patch.object(mf_client.time, "sleep") as sleep):
                with self.assertRaises(OAuthError) as caught:
                    self.client.get("https://example.invalid")
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
                self.assertNotIn("fake-secret", str(caught.exception))
                self.assertNotIn("must-not-log", str(caught.exception))

    def test_401_refreshes_then_resends_once(self) -> None:
        with (patch.object(mf_client.requests, "request", side_effect=[response(401), response()]) as request,
              patch.object(mf_client, "load_token", return_value={"refresh_token": "fake-refresh"}),
              patch.object(mf_client, "refresh_access_token",
                           return_value={"access_token": "new-fake-access"}) as refresh):
            self.client.get("https://example.invalid")
        refresh.assert_called_once()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer new-fake-access")

    def test_second_401_is_not_refreshed_again(self) -> None:
        with (patch.object(mf_client.requests, "request", return_value=response(401)) as request,
              patch.object(mf_client, "load_token", return_value={"refresh_token": "fake-refresh"}),
              patch.object(mf_client, "refresh_access_token",
                           return_value={"access_token": "new-fake-access"}) as refresh):
            with self.assertRaisesRegex(OAuthError, "HTTP 401"):
                self.client.get("https://example.invalid")
        self.assertEqual(request.call_count, 2)
        refresh.assert_called_once()

    def test_401_without_refresh_token_fails(self) -> None:
        with (patch.object(mf_client.requests, "request", return_value=response(401)) as request,
              patch.object(mf_client, "load_token", return_value=None)):
            with self.assertRaisesRegex(OAuthError, "HTTP 401"):
                self.client.get("https://example.invalid")
        self.assertEqual(request.call_count, 1)

    def test_post_is_never_retried(self) -> None:
        with (patch.object(mf_client.requests, "request", return_value=response(500)) as request,
              patch.object(mf_client.time, "sleep") as sleep):
            with self.assertRaises(OAuthError):
                self.client.post("https://example.invalid", json_data={})
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_invalid_json_is_not_logged(self) -> None:
        item = response()
        item._content = b"fake-access is not JSON"
        with patch.object(mf_client.requests, "request", return_value=item):
            with self.assertRaises(OAuthError) as caught:
                self.client.get("https://example.invalid")
        self.assertNotIn("fake-access", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
