from __future__ import annotations

from typing import Any

import requests

from mf_auth import (
    OAuthError,
    REQUEST_TIMEOUT_SECONDS,
    get_valid_token,
    load_settings,
    response_error_message,
)


class MoneyForwardClient:
    """Money Forward Cloud APIの共通クライアント。"""

    def __init__(self) -> None:
        self.settings = load_settings()

    def _get_access_token(self) -> str:
        """有効なアクセストークンを取得する。"""
        token = get_valid_token(self.settings)

        access_token = token.get("access_token")

        if not access_token:
            raise OAuthError("access_tokenを取得できませんでした。")

        return str(access_token)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """MFクラウドAPIへ共通形式でリクエストする。"""
        access_token = self._get_access_token()

        response = requests.request(
            method=method,
            url=url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params=params,
            json=json_data,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        if response.status_code == 401:
            raise OAuthError(
                "認証に失敗しました。"
                "アクセストークンが期限切れまたは無効な可能性があります。\n"
                + response_error_message(response)
            )

        if response.status_code == 403:
            raise OAuthError(
                "APIを呼び出す権限がありません。"
                "scopeまたはMFクラウド上の権限を確認してください。\n"
                + response_error_message(response)
            )

        if not response.ok:
            raise OAuthError(
                "MFクラウドAPIの呼び出しに失敗しました。\n"
                + response_error_message(response)
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise OAuthError(
                "APIレスポンスをJSONとして読み込めませんでした。\n"
                f"Response: {response.text}"
            ) from exc

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(method="GET", url=url, params=params)

    def post(
        self,
        url: str,
        *,
        json_data: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(method="POST", url=url, json_data=json_data)
