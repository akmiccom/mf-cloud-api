from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv
import os

# ============================================================
# Money Forward Cloud OAuth settings
# ============================================================

AUTHORIZATION_URL = "https://api.biz.moneyforward.com/authorize"
TOKEN_URL = "https://api.biz.moneyforward.com/token"

# 接続確認用スコープ
# SCOPES = ["mfc/admin/tenant.read"]
SCOPES = [
    "mfc/admin/tenant.read",
    "mfc/accounting/offices.read",
    "mfc/accounting/accounts.read",
    "mfc/accounting/journal.read",
]

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.json"

# アクセストークンの期限直前ではなく、少し早めに更新する
TOKEN_EXPIRY_MARGIN_SECONDS = 60

REQUEST_TIMEOUT_SECONDS = 30


class OAuthError(RuntimeError):
    """OAuth認証またはAPI呼び出しに失敗した場合の例外。"""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str


def load_settings() -> Settings:
    """環境変数からOAuth設定を読み込む。"""
    load_dotenv(BASE_DIR / ".env")

    client_id = os.getenv("MF_CLIENT_ID", "").strip()
    client_secret = os.getenv("MF_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("MF_REDIRECT_URI", "").strip()

    missing = []

    if not client_id:
        missing.append("MF_CLIENT_ID")

    if not client_secret:
        missing.append("MF_CLIENT_SECRET")

    if not redirect_uri:
        missing.append("MF_REDIRECT_URI")

    if missing:
        raise OAuthError(".envに必要な設定がありません: " + ", ".join(missing))

    parsed_redirect = urlparse(redirect_uri)

    if parsed_redirect.scheme != "http":
        raise OAuthError(
            "ローカル接続確認では、MF_REDIRECT_URIを"
            "'http://localhost:8000/callback'にしてください。"
        )

    if parsed_redirect.hostname not in {"localhost", "127.0.0.1"}:
        raise OAuthError(
            "このプログラムはlocalhost用です。"
            "MF_REDIRECT_URIを"
            "'http://localhost:8000/callback'にしてください。"
        )

    if parsed_redirect.path != "/callback":
        raise OAuthError("MF_REDIRECT_URIのパスは'/callback'にしてください。")

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )


def load_token() -> dict[str, Any] | None:
    """token.jsonを読み込む。存在しない場合はNoneを返す。"""
    if not TOKEN_FILE.exists():
        return None

    try:
        with TOKEN_FILE.open("r", encoding="utf-8") as file:
            token = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"token.jsonを読み込めませんでした: {exc}") from exc

    if not isinstance(token, dict):
        raise OAuthError("token.jsonの形式が不正です。")

    return token


def save_token(token: dict[str, Any]) -> None:
    """トークンと取得時刻・有効期限をtoken.jsonへ保存する。"""
    saved_token = dict(token)

    now = int(time.time())
    expires_in = int(saved_token.get("expires_in", 3600))

    saved_token["obtained_at"] = now
    saved_token["expires_at"] = now + expires_in

    temporary_file = TOKEN_FILE.with_suffix(".json.tmp")

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                saved_token,
                file,
                ensure_ascii=False,
                indent=2,
            )

        temporary_file.replace(TOKEN_FILE)
    except OSError as exc:
        raise OAuthError(f"トークンを保存できませんでした: {exc}") from exc

    print(f"トークンを保存しました: {TOKEN_FILE}")


def is_access_token_valid(token: dict[str, Any]) -> bool:
    """アクセストークンがまだ有効か確認する。"""
    access_token = token.get("access_token")
    expires_at = token.get("expires_at")

    if not access_token or not expires_at:
        return False

    try:
        expiry = int(expires_at)
    except (TypeError, ValueError):
        return False

    return time.time() < expiry - TOKEN_EXPIRY_MARGIN_SECONDS


def response_error_message(response: requests.Response) -> str:
    """APIエラーを表示しやすい文字列へ変換する。"""
    try:
        payload = response.json()
        detail = json.dumps(payload, ensure_ascii=False, indent=2)
    except ValueError:
        detail = response.text.strip() or "(response body is empty)"

    request_id = response.headers.get("X-Request-Id") or response.headers.get(
        "x-request-id"
    )

    message = (
        f"HTTP {response.status_code}\n" f"URL: {response.url}\n" f"Response:\n{detail}"
    )

    if request_id:
        message += f"\nRequest ID: {request_id}"

    return message


def exchange_authorization_code(
    settings: Settings,
    authorization_code: str,
) -> dict[str, Any]:
    """認可コードをアクセストークンへ交換する。"""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": settings.redirect_uri,
        },
        # CLIENT_SECRET_BASICなのでHTTP Basic認証を使う
        auth=(settings.client_id, settings.client_secret),
        headers={
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    if not response.ok:
        raise OAuthError(
            "認可コードからトークンを取得できませんでした。\n"
            + response_error_message(response)
        )

    token = response.json()

    if not token.get("access_token"):
        raise OAuthError("トークンレスポンスにaccess_tokenがありません。")

    save_token(token)
    return load_token() or token


def refresh_access_token(
    settings: Settings,
    token: dict[str, Any],
) -> dict[str, Any]:
    """リフレッシュトークンでアクセストークンを更新する。"""
    refresh_token = token.get("refresh_token")

    if not refresh_token:
        raise OAuthError(
            "refresh_tokenがありません。ブラウザ認証をやり直してください。"
        )

    print("アクセストークンを更新しています。")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(settings.client_id, settings.client_secret),
        headers={
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    if not response.ok:
        raise OAuthError(
            "アクセストークンを更新できませんでした。\n"
            + response_error_message(response)
        )

    refreshed_token = response.json()

    if not refreshed_token.get("access_token"):
        raise OAuthError("更新レスポンスにaccess_tokenがありません。")

    # サーバーが新しいrefresh_tokenを返さない場合に備える
    if not refreshed_token.get("refresh_token"):
        refreshed_token["refresh_token"] = refresh_token

    save_token(refreshed_token)
    return load_token() or refreshed_token


def build_authorization_url(
    settings: Settings,
    state: str,
) -> str:
    """ブラウザで開く認可URLを生成する。"""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )

    return f"{AUTHORIZATION_URL}?{query}"


def get_callback_server_address(
    redirect_uri: str,
) -> tuple[str, int]:
    parsed = urlparse(redirect_uri)

    hostname = parsed.hostname or "localhost"
    port = parsed.port or 80

    return hostname, port


def authorize_in_browser(settings: Settings) -> dict[str, Any]:
    """
    ローカルHTTPサーバーを起動し、ブラウザ認証結果のcodeを受け取る。
    """
    state = secrets.token_urlsafe(32)
    authorization_result: dict[str, str] = {}
    callback_received = threading.Event()

    expected_path = urlparse(settings.redirect_uri).path

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed_request = urlparse(self.path)

            if parsed_request.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return

            query = parse_qs(parsed_request.query)

            returned_state = query.get("state", [""])[0]
            authorization_code = query.get("code", [""])[0]
            oauth_error = query.get("error", [""])[0]
            error_description = query.get(
                "error_description",
                [""],
            )[0]

            if returned_state != state:
                authorization_result["error"] = (
                    "stateが一致しません。認証要求が改ざんされた可能性があります。"
                )
                status_code = 400
                browser_message = (
                    "認証に失敗しました。"
                    "このタブを閉じて、ターミナルを確認してください。"
                )
            elif oauth_error:
                authorization_result["error"] = f"{oauth_error}: {error_description}"
                status_code = 400
                browser_message = (
                    "認証がキャンセルされたか、失敗しました。"
                    "このタブを閉じてください。"
                )
            elif not authorization_code:
                authorization_result["error"] = (
                    "コールバックURLに認可コードがありません。"
                )
                status_code = 400
                browser_message = (
                    "認可コードを取得できませんでした。" "このタブを閉じてください。"
                )
            else:
                authorization_result["code"] = authorization_code
                status_code = 200
                browser_message = (
                    "マネーフォワード クラウドの認証が完了しました。"
                    "このタブを閉じて、ターミナルへ戻ってください。"
                )

            body = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>Money Forward OAuth</title>
</head>
<body style="font-family: sans-serif; margin: 3rem;">
  <h1>{browser_message}</h1>
</body>
</html>
"""

            encoded_body = body.encode("utf-8")

            self.send_response(status_code)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(encoded_body)),
            )
            self.end_headers()
            self.wfile.write(encoded_body)

            callback_received.set()

        def log_message(
            self,
            format: str,
            *args: object,
        ) -> None:
            # localhostへのアクセスログを非表示にする
            return

    host, port = get_callback_server_address(settings.redirect_uri)

    try:
        server = HTTPServer((host, port), CallbackHandler)
    except OSError as exc:
        raise OAuthError(
            f"ローカルサーバーを起動できませんでした: {exc}\n"
            f"ポート{port}を別のプログラムが使用していないか確認してください。"
        ) from exc

    authorization_url = build_authorization_url(settings, state)

    print("\nブラウザでマネーフォワードの認証画面を開きます。")
    print("自動で開かない場合は、次のURLをブラウザへ貼り付けてください。")
    print(authorization_url)
    print()

    opened = webbrowser.open(authorization_url)

    if not opened:
        print("ブラウザを自動起動できませんでした。上記URLを開いてください。")

    try:
        # コールバックを1回処理するまで待機
        while not callback_received.is_set():
            server.handle_request()
    finally:
        server.server_close()

    if "error" in authorization_result:
        raise OAuthError("ブラウザ認証に失敗しました: " + authorization_result["error"])

    authorization_code = authorization_result.get("code")

    if not authorization_code:
        raise OAuthError("認可コードを取得できませんでした。")

    return exchange_authorization_code(
        settings,
        authorization_code,
    )


def get_valid_token(settings: Settings) -> dict[str, Any]:
    """
    有効なトークンを返す。

    1. token.jsonが有効なら再利用
    2. 期限切れならrefresh_tokenで更新
    3. トークンがなければブラウザ認証
    """
    token = load_token()

    if token and is_access_token_valid(token):
        print("保存済みのアクセストークンを使用します。")
        return token

    if token and token.get("refresh_token"):
        try:
            return refresh_access_token(settings, token)
        except OAuthError as exc:
            print(f"トークン更新に失敗しました: {exc}")
            print("ブラウザ認証をやり直します。")

    return authorize_in_browser(settings)
