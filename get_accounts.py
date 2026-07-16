from __future__ import annotations

import json
import sys

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient

ACCOUNTS_URL = "https://api-accounting.moneyforward.com/api/v3/accounts"


def main() -> int:
    try:
        client = MoneyForwardClient()

        result = client.get(
            ACCOUNTS_URL,
            params={
                "available": "true",
            },
        )

        accounts = result.get("accounts", [])

        print("=" * 60)
        print("勘定科目の取得に成功しました。")
        print("=" * 60)
        print(f"取得件数: {len(accounts)}")
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2))

        return 0

    except OAuthError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    except requests.RequestException as exc:
        print(f"通信エラー: {exc}", file=sys.stderr)
        return 1

    except KeyboardInterrupt:
        print("\n処理を中断しました。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
