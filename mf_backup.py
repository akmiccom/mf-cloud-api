"""Read-only MF backup workflows shared by the command-line scripts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from backup_support import BackupError, BackupManifest, api_total_count, validate_total
from get_journals import (
    JOURNAL_LIST_KEYS, OUTPUT_DIR, _has_more_pages,
    detect_duplicate_journals, fetch_all_journals, fetch_journal_page,
    find_journal_list, flatten_journal_branches, flatten_journals,
    parse_date, resolve_date_range, save_csv, save_expanded_csv,
)
from mf_client import MoneyForwardClient
from mf_endpoints import JOURNALS_URL, MASTER_ENDPOINTS, REPORT_ENDPOINTS

FISCAL_YEAR_REQUIREMENTS = {
    "sub_accounts": "recommended",
    "trial_balance_bs": "recommended", "trial_balance_pl": "recommended",
    "taxes": "optional", "departments": "optional", "trade_partners": "optional",
}


def request_record(client: Any, params: dict[str, Any], page: int) -> dict[str, Any]:
    # Caller passes only the explicitly supported public query parameters.
    return {"page": page, "parameters": params,
            "http_status": getattr(client, "last_http_status", None)}


def master_records(name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """The six supported endpoints each have their own named object array.

    An empty recognized array is valid; missing/null/wrong types are not zero.
    Do not discover arbitrary arrays or change the RAW response.
    """
    if name not in MASTER_ENDPOINTS or not isinstance(result, dict):
        raise BackupError("未対応のマスター応答構造です。")
    if name not in result:
        raise BackupError(f"{name}: 必須の配列キーがありません。RAWは保存済みです。")
    records = result[name]
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise BackupError(f"{name}: 応答のマスター配列が不正です。RAWは保存済みです。")
    return records


def retrieve_master(client: Any, manifest: BackupManifest, name: str) -> dict[str, Any]:
    """Current official master endpoints return one unpaginated response.

    available is omitted so inactive records are not intentionally excluded.
    account_id is optional on sub_accounts, so the complete list is requested.
    Fail closed on unexpected pagination instead of guessing query parameters.
    """
    entry = manifest.data["endpoints"][name]
    manifest.start(name)
    result = client.get(MASTER_ENDPOINTS[name])
    entry["requests"].append(request_record(client, {}, 1))
    entry["page_count"] = 1
    validation_error = None
    try:
        records = master_records(name, result)
        entry["retrieved_count"] = len(records)
        entry["response_structure_recognized"] = True
    except BackupError as exc:
        validation_error = exc
        entry["retrieved_count"] = None
        entry["response_structure_recognized"] = False
    # Record the recognized count in the same checkpoint as the RAW file.
    manifest.save_json(name, f"master/{name}.json", result)
    if validation_error is not None:
        raise validation_error
    entry["api_total_count"] = api_total_count(result)
    if _has_more_pages(result, 1, max(len(records), 1)) is True:
        raise BackupError(f"{name}: 公式仕様にないpaginationを検出しました。仕様確認が必要です。")
    validate_total(len(records), entry["api_total_count"])
    entry["total_count_verified"] = entry["api_total_count"] is not None
    manifest.succeed(name)
    return result


def select_period(result: dict[str, Any], start: str, end: str,
                  *, entire_year: bool) -> dict[str, Any]:
    """Do not infer fiscal_year from the calendar year of the requested start."""
    candidates = []
    for period in result.get("term_settings", []):
        if not isinstance(period, dict):
            continue
        try:
            period_start = parse_date(str(period["start_date"]))
            period_end = parse_date(str(period["end_date"]))
        except (KeyError, argparse.ArgumentTypeError):
            continue
        if ((period_start == start and period_end == end) if entire_year else
                (period_start <= start <= end <= period_end)):
            candidates.append(period)
    if len(candidates) != 1:
        raise BackupError("指定期間に対応する会計年度設定を一意に取得できませんでした。")
    period = candidates[0]
    fiscal_year = period.get("fiscal_year")
    if not isinstance(fiscal_year, int) or isinstance(fiscal_year, bool):
        raise BackupError("会計年度設定のfiscal_yearが不正です。")
    return {key: period[key] for key in ("start_date", "end_date", "fiscal_year")}


def set_period(manifest: BackupManifest, period: dict[str, Any]) -> None:
    manifest.data["accounting_period"] = period
    # term_period comes from actual journals, not an assumed fiscal_year mapping.
    manifest.checkpoint()


def retrieve_report(client: Any, manifest: BackupManifest, name: str,
                    period: dict[str, Any], *, include_tax: bool = False) -> None:
    entry = manifest.data["endpoints"][name]
    manifest.start(name)
    params = {"fiscal_year": period["fiscal_year"],
              "start_date": manifest.data["start_date"],
              "end_date": manifest.data["end_date"],
              "with_sub_accounts": "true", "include_tax": str(include_tax).lower(),
              "journal_types": ["journal_entry", "adjusting_entry"]}
    result = client.get(REPORT_ENDPOINTS[name], params=params)
    entry["requests"].append(request_record(client, params, 1))
    entry["page_count"] = 1
    manifest.save_json(name, f"reports/{name}.json", result)
    # The documented report is an object containing report_type, columns, rows.
    if (result.get("report_type") != name or not isinstance(result.get("columns"), list)
            or not isinstance(result.get("rows"), list)):
        raise BackupError(f"{name}: 帳票応答のreport_type/columns/rowsが不正です。")
    if (result.get("start_date") != params["start_date"] or
            result.get("end_date") != params["end_date"]):
        raise BackupError(f"{name}: 帳票応答の対象期間が指定期間と一致しません。")
    entry["retrieved_count"] = len(result["rows"])
    entry["response_structure_recognized"] = True
    entry["retrieved_count_unit"] = "top_level_report_rows"
    entry["api_total_count"] = api_total_count(result)
    manifest.succeed(name)


def retrieve_journals(client: Any, manifest: BackupManifest, *, per_page: int = 100,
                      all_pages: bool = True, page: int = 1,
                      legacy_names: bool = False) -> None:
    name = "journals"
    entry = manifest.data["endpoints"][name]
    manifest.start(name)
    start, end = manifest.data["start_date"], manifest.data["end_date"]
    observed_terms: set[int | str] = set()

    def preserve_page(number: int, raw: dict[str, Any]) -> None:
        params = {"start_date": start, "end_date": end,
                  "page": number, "per_page": per_page}
        entry["requests"].append(request_record(client, params, number))
        entry["page_count"] += 1
        entry["retrieved_count"] = (entry["retrieved_count"] or 0) + len(find_journal_list(raw))
        manifest.save_json(name, f"journals/pages/page_{number:04d}.json", raw)
        total = api_total_count(raw)
        if total is not None:
            if entry["api_total_count"] is not None and entry["api_total_count"] != total:
                raise BackupError("API total_countが取得途中で変化しました。再取得が必要です。")
            entry["api_total_count"] = total
        for journal in find_journal_list(raw):
            term = journal.get("term_period")
            if isinstance(term, (int, str)) and not isinstance(term, bool):
                observed_terms.add(term)
        manifest.data["term_period"] = (
            next(iter(observed_terms)) if len(observed_terms) == 1 else None
        )
        entry["observed_term_periods"] = sorted(observed_terms, key=str)
        manifest.checkpoint()

    if all_pages:
        result = fetch_all_journals(client, start, end, per_page, on_page=preserve_page)
        entry["total_count_verified"] = entry["api_total_count"] is not None
    else:
        result = fetch_journal_page(client, start, end, page, per_page)
        preserve_page(page, result)
        raw_list = next((result[key] for key in JOURNAL_LIST_KEYS
                         if isinstance(result.get(key), list)), None)
        if raw_list is None or any(not isinstance(item, dict) for item in raw_list):
            raise BackupError("仕訳配列が不正です。RAWは保存済みです。")

    journals = find_journal_list(result)
    entry["response_structure_recognized"] = True
    duplicates = detect_duplicate_journals(journals)
    entry["duplicate_journal_count"] = sum(duplicates.values())
    if duplicates:
        # RAW data is kept unchanged; IDs are not written to the terminal.
        if all_pages:
            result["backup_metadata"]["complete"] = False
        combined = (f"journals_{start}_{end}_all.json" if legacy_names else "journals/all.json")
        if all_pages:
            manifest.save_json(name, combined, result)
        raise BackupError(f"重複仕訳IDを検出しました（重複出現数={sum(duplicates.values())}）。")

    if legacy_names:
        suffix = "all" if all_pages else f"page{page}"
        base = f"journals_{start}_{end}_{suffix}"
        json_name = f"{base}.json"
        csv_name = f"{base}.csv"
        expanded_name = (f"journals_{start}_{end}_expanded.csv" if all_pages
                         else f"{base}_expanded.csv")
    else:
        json_name, csv_name, expanded_name = (
            "journals/all.json", "journals/flat.csv", "journals/expanded.csv")
    # RAW takes priority: no CSV conversion failure can erase saved JSON.
    manifest.save_json(name, json_name, result)
    rows = flatten_journals(result)
    expanded_rows = flatten_journal_branches(result)
    expanded_count = len({row["journal_index"] for row in expanded_rows})
    entry["expanded_journal_count"] = expanded_count
    entry["expanded_branch_count"] = len(expanded_rows)
    if expanded_count != len(journals):
        raise BackupError("expanded CSVの仕訳数が取得件数と一致しません。RAWは保存済みです。")
    for relative, data, writer in (
        (csv_name, rows, save_csv), (expanded_name, expanded_rows, save_expanded_csv)
    ):
        path = manifest.target(relative)
        writer(data, path, overwrite=manifest.overwrite)
        manifest.record_file(name, path)
    manifest.succeed(name, complete=all_pages)


def print_failure(manifest: BackupManifest | None, exc: BaseException,
                  name: str | None = None) -> int:
    if manifest is not None:
        try:
            manifest.fail(exc, name)
        except (OSError, BackupError) as save_error:
            print(f"manifest更新失敗: {type(save_error).__name__}。バックアップは未完了です。",
                  file=sys.stderr)
        message = manifest.data["errors"][-1] if manifest.data["errors"] else type(exc).__name__
    else:
        message = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
    print(f"エラー: {message}", file=sys.stderr)
    return 130 if isinstance(exc, KeyboardInterrupt) else 1


def run_journals_cli(args: argparse.Namespace, client_factory: Callable[[], Any]) -> int:
    manifest = None
    try:
        manifest = BackupManifest(args.output_dir, start_date=args.start_date,
                                  end_date=args.end_date, overwrite=args.overwrite,
                                  scope="journals_all" if args.all_pages else "journals_single_page")
        manifest.register_endpoint("journals", JOURNALS_URL)
        manifest.checkpoint()
        client = client_factory()
        start, end, fiscal_year, used_default = resolve_date_range(
            client, args.start_date, args.end_date)
        manifest.data.update(start_date=start, end_date=end)
        if used_default:
            manifest.data["accounting_period"] = {
                "start_date": start, "end_date": end, "fiscal_year": fiscal_year}
        manifest.checkpoint()
        retrieve_journals(client, manifest, per_page=args.per_page,
                          all_pages=args.all_pages, page=args.page or 1, legacy_names=True)
        manifest.finish()
        print(f"仕訳取得・保存完了: {manifest.root}（manifest complete={manifest.data['complete']}）")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if manifest is not None and "client" in locals():
            manifest.data["endpoints"]["journals"]["last_http_status"] = getattr(client, "last_http_status", None)
        return print_failure(manifest, exc, "journals" if manifest else None)


def build_backup_parser(mode: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MFクラウド会計の取得専用ローカルバックアップ")
    parser.add_argument("--start-date", type=parse_date, required=mode != "master")
    parser.add_argument("--end-date", type=parse_date, required=mode != "master")
    parser.add_argument("--output-dir", type=Path, help="バックアップ単位の出力先ディレクトリ")
    parser.add_argument("--overwrite", action="store_true")
    if mode == "fiscal_year":
        parser.add_argument("--per-page", type=int, default=100)
    if mode != "master":
        parser.add_argument("--include-tax", action="store_true",
                            help="帳票のinclude_tax=true（既定false）。manifestへ記録")
    return parser


def run_backup_cli(mode: str, argv: list[str] | None = None,
                   client_factory: Callable[[], Any] = MoneyForwardClient) -> int:
    args = build_backup_parser(mode).parse_args(argv)
    if (bool(args.start_date) != bool(args.end_date) or
            (args.start_date and args.start_date > args.end_date)):
        print("エラー: 有効な開始日・終了日を両方指定してください。", file=sys.stderr)
        return 2
    if not 1 <= getattr(args, "per_page", 100) <= 10000:
        print("エラー: per-pageは1～10000にしてください。", file=sys.stderr)
        return 2
    if args.output_dir is None:
        directory_name = (f"{args.start_date}_{args.end_date}" if args.start_date else "master")
        if mode == "reports":
            directory_name += "_reports"
        elif mode == "master" and args.start_date:
            directory_name += "_master"
        args.output_dir = OUTPUT_DIR / directory_name
    return backup(mode, args, client_factory)


def backup(mode: str, args: argparse.Namespace, client_factory: Callable[[], Any]) -> int:
    manifest = None
    current = None
    try:
        manifest = BackupManifest(args.output_dir, start_date=args.start_date,
                                  end_date=args.end_date, overwrite=args.overwrite, scope=mode)
        names = list(MASTER_ENDPOINTS) if mode != "reports" else ["term_settings"]
        for name in names:
            manifest.register_endpoint(
                name, MASTER_ENDPOINTS[name],
                requirement=FISCAL_YEAR_REQUIREMENTS.get(name, "required")
                if mode == "fiscal_year" else "required")
        if mode == "fiscal_year":
            manifest.register_endpoint("journals", JOURNALS_URL)
        if mode != "master":
            for name, url in REPORT_ENDPOINTS.items():
                manifest.register_endpoint(
                    name, url, requirement=FISCAL_YEAR_REQUIREMENTS.get(name, "required")
                    if mode == "fiscal_year" else "required")
        manifest.checkpoint()
        client = client_factory()
        period = None

        def attempt(name: str, operation: Callable[[], Any]) -> Any:
            try:
                return operation()
            except Exception as exc:
                entry = manifest.data["endpoints"][name]
                entry["last_http_status"] = getattr(client, "last_http_status", None)
                # Failure to persist this checkpoint remains a fatal run error.
                manifest.fail(exc, name, fatal=False)
                label = "エラー" if entry["requirement"] == "required" else "警告"
                print(f"{label}: {name}: {entry['errors'][-1]}", file=sys.stderr)
                return None

        for current in names:
            def master_operation() -> None:
                nonlocal period
                result = retrieve_master(client, manifest, current)
                if current == "term_settings" and args.start_date:
                    selected = select_period(result, args.start_date, args.end_date,
                                             entire_year=mode == "fiscal_year")
                    set_period(manifest, selected)
                    period = selected
            attempt(current, master_operation)
        if mode == "fiscal_year":
            current = "journals"
            attempt(current, lambda: retrieve_journals(client, manifest, per_page=args.per_page))
        if mode != "master":
            for current in REPORT_ENDPOINTS:
                if period is None:
                    manifest.fail(BackupError("term_settingsから対象会計年度を確定できないため帳票取得を省略しました。"),
                                  current, fatal=False)
                    manifest.data["endpoints"][current]["status"] = "skipped_dependency"
                    manifest.checkpoint()
                else:
                    attempt(current, lambda: retrieve_report(client, manifest, current, period,
                                                             include_tax=args.include_tax))
        manifest.finish()
        print(f"バックアップ結果: {manifest.data['status']}: {manifest.root}")
        return 0 if manifest.data["complete"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        if manifest is not None and current is not None and "client" in locals():
            manifest.data["endpoints"][current]["last_http_status"] = getattr(client, "last_http_status", None)
        return print_failure(manifest, exc, current)
