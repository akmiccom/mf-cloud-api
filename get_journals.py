from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable

import requests

from mf_auth import OAuthError
from mf_client import MoneyForwardClient
from mf_endpoints import JOURNALS_URL, OFFICES_URL
from backup_support import BackupError, api_total_count, utc_now, validate_total


OUTPUT_DIR = Path(__file__).resolve().parent / "output"
MAX_PAGES = 1000
JOURNAL_LIST_KEYS = ("journals", "items", "data")
PAGINATION_KEYS = (
    "has_next",
    "has_next_page",
    "is_last",
    "is_last_page",
    "next_page",
    "total_pages",
    "page_count",
    "total_count",
    "total_entries",
)
JOURNAL_BRANCH_COLUMNS = (
    "journal_index",
    "journal_number",
    "journal_transaction_date",
    "journal_id",
    "journal_entered_by",
    "journal_is_realized",
    "journal_journal_type",
    "journal_memo",
    "journal_transaction_id",
    "journal_voucher_file_ids",
    "journal_tags",
    "journal_term_period",
    "journal_create_time",
    "journal_update_time",
    "branch_index",
    "remark",
    "debitor_account_name",
    "debitor_sub_account_name",
    "debitor_value",
    "debitor_tax_name",
    "debitor_tax_long_name",
    "debitor_tax_value",
    "debitor_department_name",
    "debitor_trade_partner_name",
    "debitor_account_id",
    "debitor_sub_account_id",
    "debitor_tax_id",
    "debitor_department_id",
    "debitor_trade_partner_code",
    "debitor_invoice_kind",
    "creditor_account_name",
    "creditor_sub_account_name",
    "creditor_value",
    "creditor_tax_name",
    "creditor_tax_long_name",
    "creditor_tax_value",
    "creditor_department_name",
    "creditor_trade_partner_name",
    "creditor_account_id",
    "creditor_sub_account_id",
    "creditor_tax_id",
    "creditor_department_id",
    "creditor_trade_partner_code",
    "creditor_invoice_kind",
)
BRANCH_SIDE_FIELDS = (
    "account_name",
    "sub_account_name",
    "value",
    "tax_name",
    "tax_long_name",
    "tax_value",
    "department_name",
    "trade_partner_name",
    "account_id",
    "sub_account_id",
    "tax_id",
    "department_id",
    "trade_partner_code",
    "invoice_kind",
)


class JournalFetchError(BackupError, OAuthError):
    """Safe validation failure compatible with the existing OAuthError callers."""


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
    parser.add_argument(
        "--page",
        type=int,
        help="取得するページ（省略時は1）",
    )
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument("--overwrite", action="store_true",
                        help="明示的に既存出力先を再利用する（既存ファイルを上書き）")
    parser.add_argument(
        "--all-pages",
        action="store_true",
        help=f"1ページ目から全ページを取得（最大{MAX_PAGES}ページ）",
    )
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
    for key in JOURNAL_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    return []


def fetch_journal_page(
    client: MoneyForwardClient,
    start_date: str,
    end_date: str,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """指定したページの仕訳を取得する。"""
    return client.get(
        JOURNALS_URL,
        params={
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "per_page": per_page,
        },
    )


def _pagination_metadata(result: dict[str, Any]) -> dict[str, Any] | None:
    """ページネーション情報として認識できるレスポンス部分を返す。"""
    for key in ("metadata", "pagination", "paging", "meta"):
        value = result.get(key)
        if isinstance(value, dict) and any(
            field in value for field in PAGINATION_KEYS
        ):
            return value

    if any(field in result for field in PAGINATION_KEYS):
        return result

    return None


def _pagination_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not (
        isinstance(value, int) or (isinstance(value, str) and value.isdecimal())
    ) or int(value) < 0:
        raise BackupError(f"paginationの{name}が非負整数ではありません。")
    return int(value)


def _has_more_pages(
    result: dict[str, Any], current_page: int, per_page: int,
) -> bool | None:
    """不正・矛盾したpaginationは成功扱いにしない。"""
    metadata = _pagination_metadata(result)
    if metadata is None:
        return None
    page_value = metadata.get("current_page", metadata.get("page_number", metadata.get("page", current_page)))
    metadata_page = _pagination_integer(page_value, "current_page")
    if metadata_page != current_page:
        raise BackupError("paginationの現在ページが要求ページと一致しません。")
    decisions: list[bool] = []
    for key in ("has_next_page", "has_next", "is_last_page", "is_last"):
        if key in metadata:
            value = metadata[key]
            if not isinstance(value, bool):
                raise BackupError(f"paginationの{key}がbooleanではありません。")
            decisions.append(not value if key.startswith("is_last") else value)
    if "next_page" in metadata:
        value = metadata["next_page"]
        if value is None or value is False or value == "" or value == 0:
            decisions.append(False)
        else:
            next_page = _pagination_integer(value, "next_page")
            if next_page != current_page + 1:
                raise BackupError("paginationのnext_pageが連続していません。")
            decisions.append(True)
    for key in ("total_pages", "page_count"):
        if key in metadata:
            total_pages = _pagination_integer(metadata[key], key)
            if (total_pages > 0 and current_page > total_pages) or (
                total_pages == 0 and find_journal_list(result)
            ):
                raise BackupError("paginationの総ページ数が応答内容と矛盾しています。")
            decisions.append(current_page < total_pages)
    if decisions:
        if len(set(decisions)) != 1:
            raise BackupError("paginationの次ページ情報が矛盾しています。")
        return decisions[0]
    total = api_total_count(result)
    return current_page * per_page < total if total is not None else None

def fetch_all_journals(
    client: MoneyForwardClient,
    start_date: str,
    end_date: str,
    per_page: int,
    *,
    max_pages: int = MAX_PAGES,
    on_page: Callable[[int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """RAW保存callbackの後に検証し、API metadataと独自metadataを分離する。"""
    if max_pages < 1:
        raise ValueError("max_pagesは1以上でなければなりません。")

    all_journals: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1

    while page <= max_pages:
        result = fetch_journal_page(
            client,
            start_date,
            end_date,
            page,
            per_page,
        )
        if on_page is not None:
            on_page(page, result)
        raw_list = next((result[key] for key in JOURNAL_LIST_KEYS
                         if isinstance(result.get(key), list)), None)
        if raw_list is None or any(not isinstance(item, dict) for item in raw_list):
            raise BackupError("仕訳配列が存在しないか、object以外の要素を含んでいます。")
        page_journals = find_journal_list(result)
        all_journals.extend(page_journals)
        page_total = api_total_count(result)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise BackupError("API total_countが取得途中で変化しました。再取得が必要です。")
            expected_total = page_total
        print(
            f"ページ {page}: {len(page_journals)}件 "
            f"(累計 {len(all_journals)}件)"
        )

        has_more = _has_more_pages(result, page, per_page)
        if not page_journals or has_more is False:
            if not page_journals and has_more is True:
                raise BackupError("空ページですがAPIは次ページありと報告しています。")
            break

        if page == max_pages:
            if has_more is True or len(page_journals) >= per_page:
                raise JournalFetchError(
                    f"安全上限の{max_pages}ページに達したため、"
                    "全ページ取得を中止しました。完了扱いにはしません。"
                )
            break

        if has_more is None and len(page_journals) < per_page:
            break

        page += 1

    validate_total(len(all_journals), expected_total)
    return {"journals": all_journals, "backup_metadata": {
        "retrieved_at": utc_now(), "start_date": start_date, "end_date": end_date,
        "retrieved_pages": page, "retrieved_journal_count": len(all_journals),
        "api_total_count": expected_total, "total_count_verified": expected_total is not None,
        "complete": True,
    }}


def detect_duplicate_journals(
    journals: list[dict[str, Any]],
) -> dict[str, int]:
    """仕訳IDの重複について、2回目以降の出現数を返す。"""
    identifiers = [
        str(identifier)
        for journal in journals
        if (identifier := journal.get("id", journal.get("journal_id")))
        is not None
        and not isinstance(identifier, (dict, list))
    ]
    counts = Counter(identifiers)
    return {
        identifier: count - 1
        for identifier, count in counts.items()
        if count > 1
    }


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


def flatten_journal_branches(
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """仕訳とbranchesを1 branch 1行のCSV用レコードへ展開する。"""
    rows: list[dict[str, Any]] = []
    journal_fields = {
        "number": "number",
        "transaction_date": "transaction_date",
        "id": "id",
        "entered_by": "entered_by",
        "is_realized": "is_realized",
        "journal_type": "journal_type",
        "memo": "memo",
        "transaction_id": "transaction_id",
        "voucher_file_ids": "voucher_file_ids",
        "tags": "tags",
        "term_period": "term_period",
        "create_time": "create_time",
        "update_time": "update_time",
    }

    for journal_index, journal in enumerate(
        find_journal_list(result),
        start=1,
    ):
        branches = journal.get("branches")
        if not isinstance(branches, list):
            continue

        for branch_index, branch in enumerate(branches, start=1):
            if not isinstance(branch, dict):
                continue

            row: dict[str, Any] = {
                "journal_index": journal_index,
                "branch_index": branch_index,
            }
            for output_name, source_name in journal_fields.items():
                value = journal.get(source_name)
                row[f"journal_{output_name}"] = (
                    scalar_value(value)
                    if output_name in {"voucher_file_ids", "tags"}
                    else "" if value is None else value
                )

            remark = branch.get("remark")
            row["remark"] = "" if remark is None else remark

            for side in ("debitor", "creditor"):
                side_data = branch.get(side)
                if not isinstance(side_data, dict):
                    side_data = {}
                for field in BRANCH_SIDE_FIELDS:
                    value = side_data.get(field)
                    row[f"{side}_{field}"] = "" if value is None else value

            rows.append(row)

    return rows


def save_json(
    result: dict[str, Any],
    output_path: Path,
    *, overwrite: bool = False,
) -> None:
    with output_path.open("w" if overwrite else "x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)


def save_csv(
    rows: list[dict[str, Any]],
    output_path: Path,
    *, overwrite: bool = False,
) -> None:
    if not rows:
        with output_path.open("w" if overwrite else "x", encoding="utf-8-sig"):
            pass
        return

    fieldnames: list[str] = []
    seen: set[str] = set()

    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with output_path.open(
        "w" if overwrite else "x",
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


def save_expanded_csv(
    rows: list[dict[str, Any]],
    output_path: Path,
    *, overwrite: bool = False,
) -> None:
    """固定列順のbranch展開CSVをUTF-8 BOM付きで保存する。"""
    with output_path.open(
        "w" if overwrite else "x",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=JOURNAL_BRANCH_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    # Import here to keep the existing conversion/fetch helpers independently usable.
    from mf_backup import run_journals_cli
    args = build_parser().parse_args()
    if args.all_pages and args.page is not None:
        print("エラー: --all-pagesと--pageは同時に指定できません。", file=sys.stderr)
        return 2
    page = args.page if args.page is not None else 1
    if page < 1 or not 1 <= args.per_page <= 10000:
        print("エラー: pageは1以上、per-pageは1～10000にしてください。", file=sys.stderr)
        return 2
    if bool(args.start_date) != bool(args.end_date):
        print("エラー: 開始日と終了日を両方指定してください。", file=sys.stderr)
        return 2
    if args.start_date and args.start_date > args.end_date:
        print("エラー: start-dateはend-date以前にしてください。", file=sys.stderr)
        return 2
    return run_journals_cli(args, MoneyForwardClient)


if __name__ == "__main__":
    raise SystemExit(main())
