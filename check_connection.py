from __future__ import annotations

import json
import sys

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient
from mf_endpoints import OFFICES_URL, TENANT_URL


def main() -> int:
    try:
        client = MoneyForwardClient()

        tenant = client.get(TENANT_URL)
        office = client.get(OFFICES_URL)

        print("=" * 60)
        print("MFクラウドAPIへの接続に成功しました。")
        print("=" * 60)

        print("\n[共通事業者情報]")
        print(json.dumps(tenant, ensure_ascii=False, indent=2))

        print("\n[クラウド会計事業者情報]")
        print(json.dumps(office, ensure_ascii=False, indent=2))

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
