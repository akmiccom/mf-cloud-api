from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient
from mf_endpoints import JOURNALS_URL, OFFICES_URL


OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def parse_date(value: str) -> str:
    """YYYY-MM-DD形式の日付を検証する。"""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "日付はYYYY-MM-DD形式で指定してください。"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "MFクラウド会計の仕訳を取得し、JSONとCSVで保存します。"
            "日付を省略した場合は現在の会計期間を使用します。"
        )
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        help="取得開始日（YYYY-MM-DD）。省略時は現在の会計期間の開始日",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        help="取得終了日（YYYY-MM-DD）。省略時は現在の会計期間の終了日",
    )
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="出力先フォルダ。省略時はプロジェクト内のoutput",
    )
    return parser


def select_current_accounting_period(
    office: dict[str, Any],
) -> tuple[str, str, int | None]:
    """今日を含む会計期間を選択する。"""
    periods = office.get("accounting_periods", [])

    if not isinstance(periods, list) or not periods:
        raise OAuthError("会計期間を取得できませんでした。")

    today = date.today()
    valid_periods: list[tuple[date, date, int | None]] = []

    for period in periods:
        if not isinstance(period, dict):
            continue

        start_value = period.get("start_date")
        end_value = period.get("end_date")
        fiscal_year = period.get("fiscal_year")

        if not start_value or not end_value:
            continue

        try:
            start = date.fromisoformat(str(start_value))
            end = date.fromisoformat(str(end_value))
        except ValueError:
            continue

        valid_periods.append((start, end, fiscal_year))

        if start <= today <= end:
            return start.isoformat(), end.isoformat(), fiscal_year

    if not valid_periods:
        raise OAuthError("有効な会計期間がありません。")

    latest = max(valid_periods, key=lambda item: item[1])
    return latest[0].isoformat(), latest[1].isoformat(), latest[2]


def resolve_date_range(
    client: MoneyForwardClient,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str, int | None, bool]:
    """引数または現在の会計期間から取得範囲を決定する。"""
    if start_date and end_date:
        return start_date, end_date, None, False

    if start_date or end_date:
        raise OAuthError(
            "--start-dateと--end-dateは、両方指定するか両方省略してください。"
        )

    office = client.get(OFFICES_URL)
    resolved_start, resolved_end, fiscal_year = (
        select_current_accounting_period(office)
    )
    return resolved_start, resolved_end, fiscal_year, True


def find_journal_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    APIレスポンス内の仕訳配列を取得する。

    レスポンス差異に備えて、よくあるキーを順に確認する。
    """
    for key in ("journals", "items", "data"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    return []


def scalar_value(value: Any) -> Any:
    """CSVセルへ安全に格納できる値へ変換する。"""
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return value

    return json.dumps(value, ensure_ascii=False)


def flatten_journals(result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    仕訳JSONをCSV用の行へ変換する。

    仕訳内に明細配列がある場合は、1明細を1行に展開する。
    明細配列のキー名の差異にもある程度対応する。
    """
    journals = find_journal_list(result)
    rows: list[dict[str, Any]] = []

    detail_keys = (
        "details",
        "journal_details",
        "entries",
        "lines",
        "items",
    )

    for journal_index, journal in enumerate(journals, start=1):
        details: list[dict[str, Any]] = []

        for key in detail_keys:
            candidate = journal.get(key)
            if isinstance(candidate, list):
                details = [
                    item for item in candidate if isinstance(item, dict)
                ]
                break

        journal_base = {
            f"journal_{key}": scalar_value(value)
            for key, value in journal.items()
            if key not in detail_keys
        }
        journal_base["journal_index"] = journal_index

        if not details:
            rows.append(journal_base)
            continue

        for detail_index, detail in enumerate(details, start=1):
            row = dict(journal_base)
            row["detail_index"] = detail_index

            for key, value in detail.items():
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        row[f"detail_{key}_{sub_key}"] = scalar_value(
                            sub_value
                        )
                else:
                    row[f"detail_{key}"] = scalar_value(value)

            rows.append(row)

    return rows


def save_json(
    result: dict[str, Any],
    output_path: Path,
) -> None:
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_csv(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    if not rows:
        output_path.write_text("", encoding="utf-8-sig")
        return

    fieldnames: list[str] = []
    seen: set[str] = set()

    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with output_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = build_parser().parse_args()

    if args.page < 1:
        print("エラー: pageは1以上にしてください。", file=sys.stderr)
        return 2

    if not 1 <= args.per_page <= 100:
        print("エラー: per-pageは1～100にしてください。", file=sys.stderr)
        return 2

    try:
        client = MoneyForwardClient()

        start_date, end_date, fiscal_year, used_default = resolve_date_range(
            client,
            args.start_date,
            args.end_date,
        )

        if start_date > end_date:
            print(
                "エラー: start-dateはend-date以前にしてください。",
                file=sys.stderr,
            )
            return 2

        result = client.get(
            JOURNALS_URL,
            params={
                "start_date": start_date,
                "end_date": end_date,
                "page": args.page,
                "per_page": args.per_page,
            },
        )

        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        base_name = (
            f"journals_{start_date}_{end_date}"
            f"_page{args.page}"
        )
        json_path = output_dir / f"{base_name}.json"
        csv_path = output_dir / f"{base_name}.csv"

        rows = flatten_journals(result)

        save_json(result, json_path)
        save_csv(rows, csv_path)

        print("=" * 60)
        print("仕訳一覧の取得と保存に成功しました。")
        print("=" * 60)

        if used_default:
            fiscal_year_text = (
                f"（fiscal_year: {fiscal_year}）"
                if fiscal_year is not None
                else ""
            )
            print(f"使用期間: {start_date} ～ {end_date} {fiscal_year_text}")
            print("期間指定: 現在の会計期間を自動選択")
        else:
            print(f"使用期間: {start_date} ～ {end_date}")
            print("期間指定: コマンドライン引数")

        print(f"CSV行数: {len(rows)}")
        print(f"JSON保存先: {json_path}")
        print(f"CSV保存先 : {csv_path}")

        if not rows:
            print(
                "警告: 仕訳配列を検出できなかったため、"
                "CSVは空で保存されました。"
            )

        return 0

    except OAuthError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ファイル保存エラー: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"通信エラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n処理を中断しました。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())