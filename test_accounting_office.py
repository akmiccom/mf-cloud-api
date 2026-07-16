from __future__ import annotations

import json
import sys

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient


OFFICES_URL = (
    "https://api-accounting.moneyforward.com/api/v3/offices"
)


def main() -> int:
    try:
        client = MoneyForwardClient()
        office = client.get(OFFICES_URL)

        print()
        print("=" * 60)
        print("クラウド会計API V3への接続に成功しました。")
        print("=" * 60)
        print(json.dumps(office, ensure_ascii=False, indent=2))

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