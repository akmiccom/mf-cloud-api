from __future__ import annotations

import json
import sys

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient


JOURNALS_URL = (
    "https://api-accounting.moneyforward.com/api/v3/journals"
)


def main() -> int:
    try:
        client = MoneyForwardClient()

        result = client.get(
            JOURNALS_URL,
            params={
                "start_date": "2025-11-01",
                "end_date": "2026-10-31",
                "page": 1,
                "per_page": 100,
            },
        )

        print()
        print("=" * 60)
        print("仕訳一覧の取得に成功しました。")
        print("=" * 60)
        print(json.dumps(result, ensure_ascii=False, indent=2))

        return 0

    except OAuthError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    except requests.Timeout:
        print(
            "エラー: APIへの接続がタイムアウトしました。",
            file=sys.stderr,
        )
        return 1

    except requests.RequestException as exc:
        print(f"通信エラー: {exc}", file=sys.stderr)
        return 1

    except KeyboardInterrupt:
        print("\n処理を中断しました。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())