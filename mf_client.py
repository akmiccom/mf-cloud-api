from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from mf_auth import (
    OAuthError, REQUEST_TIMEOUT_SECONDS, get_valid_token, load_settings,
    load_token, refresh_access_token, response_error_message,
)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_GET_RETRIES = 3


def retry_delay(response: requests.Response | None, retry_index: int) -> float:
    """Retry-After（秒またはHTTP-date）を優先し、なければ1/2/4秒待つ。"""
    if response is not None and response.status_code == 429:
        value = response.headers.get("Retry-After")
        if value:
            try:
                seconds = float(value)
                if math.isfinite(seconds) and seconds >= 0:
                    return seconds
            except ValueError:
                pass
            try:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** retry_index)


class MoneyForwardClient:
    """OAuth通信。再試行は読み取りGETだけに限定する。"""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.last_http_status: int | None = None

    def _get_access_token(self) -> str:
        token = get_valid_token(self.settings)
        if not token.get("access_token"):
            raise OAuthError("access_tokenを取得できませんでした。")
        return str(token["access_token"])

    def request(
        self, method: str, url: str, *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        retries = 0
        refreshed = False
        self.last_http_status = None
        access_token = self._get_access_token()
        while True:
            try:
                response = requests.request(
                    method=method, url=url,
                    headers={"Authorization": f"Bearer {access_token}",
                             "Accept": "application/json",
                             "Content-Type": "application/json"},
                    params=params, json=json_data, timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.last_http_status = None
                if method != "GET" or retries >= MAX_GET_RETRIES:
                    raise OAuthError(f"通信失敗: {type(exc).__name__}（再試行上限）") from None
                time.sleep(retry_delay(None, retries))
                retries += 1
                continue
            except requests.RequestException as exc:
                raise OAuthError(f"通信失敗: {type(exc).__name__}") from None

            self.last_http_status = response.status_code
            if response.status_code == 401 and method == "GET" and not refreshed:
                token = load_token()
                if token and token.get("refresh_token"):
                    refreshed = True
                    try:
                        updated = refresh_access_token(self.settings, token)
                    except (OAuthError, requests.RequestException, ValueError):
                        raise OAuthError("HTTP 401: トークン更新に失敗しました。") from None
                    access_token = str(updated["access_token"])
                    continue

            if method == "GET" and response.status_code in RETRY_STATUSES:
                if retries < MAX_GET_RETRIES:
                    delay = retry_delay(response, retries)
                    print(f"HTTP {response.status_code}: {delay:g}秒後にGET再試行 "
                          f"({retries + 1}/{MAX_GET_RETRIES})", file=sys.stderr)
                    time.sleep(delay)
                    retries += 1
                    continue

            if not response.ok:
                raise OAuthError("MFクラウドAPI呼び出し失敗: " + response_error_message(response))
            if not response.content:
                return {}
            try:
                result = response.json()
            except ValueError:
                raise OAuthError("APIレスポンスをJSONとして読み込めませんでした。") from None
            if not isinstance(result, dict):
                raise OAuthError("APIレスポンスのトップレベルがobjectではありません。")
            return result

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", url, params=params)

    def post(self, url: str, *, json_data: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", url, json_data=json_data)
