from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backup_support import BackupError, BackupManifest, api_total_count
import get_journals
from mf_auth import OAuthError
from mf_backup import master_records, retrieve_master, run_backup_cli
from mf_endpoints import JOURNALS_URL, MASTER_ENDPOINTS, REPORT_ENDPOINTS
from test_get_journals import FakeClient

START, END = "2024-11-01", "2025-10-31"


def journal(identifier: str, *, opening=False) -> dict:
    return {
        "id": identifier, "number": 1, "transaction_date": START,
        "journal_type": "adjusting_entry", "entered_by": "JOURNAL_TYPE_OPENING" if opening else "JOURNAL_TYPE_NORMAL",
        "is_realized": True, "memo": "synthetic", "tags": ["synthetic"],
        "term_period": 2024, "transaction_id": "synthetic-transaction",
        "create_time": "2024-11-01T00:00:00Z", "update_time": "2024-11-01T01:00:00Z",
        "voucher_file_ids": ["synthetic-voucher"], "future_unknown": {"keep": [1, None]},
        "branches": [{"remark": "synthetic", "unknown_branch": "keep",
                      "debitor": {"account_id": "synthetic-account", "value": 1,
                                  "tax_long_name": "synthetic-tax", "future_side": {"keep": True}},
                      "creditor": {"value": 1, "tax_long_name": "synthetic-tax"}}],
    }


def full_responses() -> list[dict]:
    masters = [
        {"term_settings": [{"start_date": START, "end_date": END, "fiscal_year": 2024,
                            "accounting_method": "TAX_INCLUDED", "unknown": True}]},
        {"accounts": [{"id": "synthetic-account", "available": False}]},
        {"sub_accounts": [{"id": "synthetic-sub"}]},
        {"taxes": [{"id": "synthetic-tax", "available": False}]},
        {"departments": []}, {"trade_partners": [{"code": "synthetic-partner", "available": False}]},
    ]
    pages = [
        {"journals": [journal("one", opening=True)], "metadata": {"total_count": 2, "total_pages": 2},
         "unknown_top_level": {"keep": True}},
        {"journals": [journal("two")], "metadata": {"total_count": 2, "total_pages": 2},
         "unknown_top_level": {"different": True}},
    ]
    reports = [{"report_type": name, "columns": ["opening_balance", "closing_balance"],
                "start_date": START, "end_date": END,
                "rows": [{"name": "synthetic", "values": [0, 1]}], "future_report": True}
               for name in REPORT_ENDPOINTS]
    return masters + pages + reports


class BackupTests(unittest.TestCase):
    def run_cli(self, root: Path, responses: list, *, mode="fiscal_year", extra=None):
        client = FakeClient(copy.deepcopy(responses))
        factory = Mock(return_value=client)
        argv = ["--start-date", START, "--end-date", END, "--output-dir", str(root)] + (extra or [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = run_backup_cli(mode, argv, client_factory=factory)
        return status, client, factory, err.getvalue()

    def test_nonrequired_http_403_and_other_failures_continue(self) -> None:
        cases = [("taxes",), ("departments",), ("trade_partners",),
                 ("sub_accounts",), ("taxes", "departments", "trade_partners"),
                 ("sub_accounts", "taxes", "departments", "trade_partners"),
                 ("trial_balance_bs",), ("trial_balance_pl",),
                 ("trial_balance_bs", "trial_balance_pl")]
        for failed in cases:
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temporary:
                responses = full_responses()
                urls = {**MASTER_ENDPOINTS, **REPORT_ENDPOINTS}
                indices = {name: index for index, name in enumerate(MASTER_ENDPOINTS)}
                indices.update({name: 8 + index for index, name in enumerate(REPORT_ENDPOINTS)})
                for name in failed:
                    responses[indices[name]] = OAuthError("synthetic secret")
                class HttpClient(FakeClient):
                    def get(self, url, *, params=None):
                        self.last_http_status = 403 if url in {urls[n] for n in failed} else 200
                        return super().get(url, params=params)
                client = HttpClient(responses)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = run_backup_cli("fiscal_year", ["--start-date", START, "--end-date", END,
                                                          "--output-dir", temporary], client_factory=lambda: client)
                result = json.loads((Path(temporary) / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertTrue(result["complete"])
                self.assertEqual(result["status"], "complete_with_warnings")
                self.assertEqual({w["endpoint"] for w in result["warnings"]}, set(failed))
                self.assertEqual(result["errors"], [])
                for name in failed:
                    entry = result["endpoints"][name]
                    self.assertEqual(entry["status"], "failed")
                    self.assertFalse(entry["success"])
                    self.assertEqual(entry["last_http_status"], 403)
                    self.assertTrue(entry["errors"])
                    requirement = "optional" if name in ("taxes", "departments", "trade_partners") else "recommended"
                    self.assertEqual(entry["requirement"], requirement)
                    warning = next(w for w in result["warnings"] if w["endpoint"] == name)
                    self.assertEqual(warning["http_status"], 403)
                    self.assertEqual(warning["requirement"], requirement)
                for name in ("journals", *REPORT_ENDPOINTS):
                    if name not in failed:
                        self.assertEqual(result["endpoints"][name]["status"], "success")

    def test_required_failure_still_attempts_independent_endpoints(self) -> None:
        for name in ("term_settings", "accounts", "journals"):
            with self.subTest(endpoint=name), tempfile.TemporaryDirectory() as temporary:
                responses = full_responses()
                index = {"term_settings": 0, "accounts": 1, "journals": 6}[name]
                responses[index] = OAuthError("synthetic failure")
                if name == "journals":
                    del responses[7]
                code, client, _, _ = self.run_cli(Path(temporary), responses)
                result = json.loads((Path(temporary) / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["complete"])
                self.assertEqual(result["endpoints"][name]["status"], "failed")
                self.assertTrue(result["errors"])
                if name == "term_settings":
                    self.assertEqual(result["endpoints"]["journals"]["status"], "success")
                    self.assertEqual(result["endpoints"]["trial_balance_pl"]["status"], "skipped_dependency")
                else:
                    self.assertIn(REPORT_ENDPOINTS["trial_balance_pl"], [c["url"] for c in client.calls])

    def test_full_backup_success_order_manifest_sha256_and_raw(self) -> None:
        responses = full_responses()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            status, client, _, _ = self.run_cli(root, responses)
            self.assertEqual(status, 0)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "success")
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["backup_format_version"], 1)
            self.assertEqual(manifest["accounting_period"]["fiscal_year"], 2024)
            self.assertEqual(manifest["term_period"], 2024)
            self.assertEqual(len(manifest["endpoints"]), 9)
            self.assertEqual([call["url"] for call in client.calls],
                             list(MASTER_ENDPOINTS.values()) + [JOURNALS_URL] * 2 + list(REPORT_ENDPOINTS.values()))
            actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*")
                            if path.is_file() and path.name != "manifest.json"}
            self.assertEqual(actual_files, {item["relative_path"] for item in manifest["files"]})
            for item in manifest["files"]:
                data = (root / item["relative_path"]).read_bytes()
                self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(item["size_bytes"], len(data))
            for page, original in enumerate(responses[6:8], 1):
                raw = json.loads((root / f"journals/pages/page_{page:04d}.json").read_text(encoding="utf-8"))
                self.assertEqual(raw, original)
            combined = json.loads((root / "journals/all.json").read_text(encoding="utf-8"))
            self.assertEqual(set(combined), {"journals", "backup_metadata"})
            self.assertTrue(combined["backup_metadata"]["complete"])
            self.assertEqual(combined["backup_metadata"]["retrieved_journal_count"], 2)
            self.assertEqual(combined["journals"], responses[6]["journals"] + responses[7]["journals"])
            for call in client.calls[:6]:
                self.assertIsNone(call["params"])
            for call in client.calls[-2:]:
                self.assertEqual(call["params"]["fiscal_year"], 2024)
                self.assertEqual(call["params"]["start_date"], START)
                self.assertEqual(call["params"]["end_date"], END)
                self.assertEqual(call["params"]["with_sub_accounts"], "true")
                self.assertEqual(call["params"]["journal_types"], ["journal_entry", "adjusting_entry"])
            self.assertEqual(manifest["endpoints"]["journals"]["api_total_count"], 2)
            self.assertTrue(manifest["endpoints"]["journals"]["total_count_verified"])

    def test_optional_failure_keeps_files_and_continues(self) -> None:
        responses = full_responses()
        responses[3] = OAuthError("access_token=fake-access client_secret=fake-secret")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            status, _, _, error = self.run_cli(root, responses)
            self.assertEqual(status, 0)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete_with_warnings")
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["endpoints"]["taxes"]["status"], "failed")
            self.assertEqual(manifest["endpoints"]["journals"]["status"], "success")
            self.assertEqual(manifest["warnings"][0]["endpoint"], "taxes")
            self.assertTrue((root / "master/accounts.json").exists())
            serialized = json.dumps(manifest) + error
            self.assertNotIn("fake-access", serialized)
            self.assertNotIn("fake-secret", serialized)

    def test_total_mismatch_is_failure_and_raw_is_kept(self) -> None:
        responses = full_responses()
        responses[6]["metadata"] = {"total_count": 7, "total_pages": 1}
        del responses[7]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            status, _, _, error = self.run_cli(root, responses)
            self.assertEqual(status, 1)
            self.assertIn("total_count不一致", error)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("total_count不一致", manifest["errors"][0])
            self.assertEqual(manifest["endpoints"]["journals"]["api_total_count"], 7)
            self.assertEqual(manifest["endpoints"]["journals"]["retrieved_count"], 1)
            self.assertFalse((root / "journals/all.json").exists())
            self.assertTrue((root / "journals/pages/page_0001.json").exists())

    def test_duplicate_ids_fail_without_dropping_raw(self) -> None:
        responses = full_responses()
        responses[7]["journals"][0]["id"] = "one"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "backup"
            status, _, _, error = self.run_cli(root, responses)
            self.assertEqual(status, 1)
            self.assertIn("重複", error)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["endpoints"]["journals"]["duplicate_journal_count"], 1)
            combined = json.loads((root / "journals/all.json").read_text(encoding="utf-8"))
            self.assertEqual(len(combined["journals"]), 2)
            self.assertFalse(combined["backup_metadata"]["complete"])

    def test_overwrite_refusal_before_authentication_preserves_existing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = b"original backup"
            (root / "manifest.json").write_bytes(original)
            status, _, factory, error = self.run_cli(root, full_responses())
            self.assertEqual(status, 1)
            factory.assert_not_called()
            self.assertIn("既存", error)
            self.assertEqual((root / "manifest.json").read_bytes(), original)

    def test_explicit_overwrite_records_only_current_run_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            status, _, _, _ = self.run_cli(root, full_responses(), extra=["--overwrite"])
            self.assertEqual(status, 0)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("unrelated.txt", {item["relative_path"] for item in manifest["files"]})
            self.assertEqual((root / "unrelated.txt").read_text(encoding="utf-8"), "keep")

    def test_accounting_period_mismatch_only_skips_dependent_reports(self) -> None:
        responses = full_responses()
        responses[0]["term_settings"][0]["fiscal_year"] = "not-an-integer"
        with tempfile.TemporaryDirectory() as temporary:
            status, client, _, _ = self.run_cli(Path(temporary), responses)
            self.assertEqual(status, 1)
            self.assertEqual(len(client.calls), 8)
            manifest = json.loads((Path(temporary) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["endpoints"]["journals"]["status"], "success")
            for name in REPORT_ENDPOINTS:
                self.assertEqual(manifest["endpoints"][name]["status"], "skipped_dependency")

    def test_empty_journal_list_is_valid_when_api_total_is_zero(self) -> None:
        responses = full_responses()
        responses[6:8] = [{"journals": [], "metadata": {"total_count": 0, "total_pages": 0}}]
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(self.run_cli(Path(temporary), responses)[0], 0)

    def test_csv_failure_keeps_raw_and_marks_backup_failed(self) -> None:
        responses = full_responses()
        responses[6]["journals"][0]["branches"] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status, _, _, error = self.run_cli(root, responses)
            self.assertEqual(status, 1)
            self.assertIn("expanded CSV", error)
            self.assertTrue((root / "journals/all.json").exists())

    def test_invalid_report_response_is_not_success(self) -> None:
        responses = full_responses()
        responses[-2] = {"unexpected": "not-a-report"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_cli(root, responses)[0], 0)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["endpoints"]["trial_balance_bs"]["status"], "failed")
            self.assertEqual(manifest["endpoints"]["trial_balance_pl"]["status"], "success")
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["status"], "complete_with_warnings")
            self.assertTrue((root / "reports/trial_balance_bs.json").exists())

    def test_wrong_report_dates_are_not_success(self) -> None:
        responses = full_responses()
        responses[-2]["end_date"] = "2024-12-31"
        with tempfile.TemporaryDirectory() as temporary:
            status, _, _, error = self.run_cli(Path(temporary), responses)
            self.assertEqual(status, 0)
            self.assertIn("対象期間", error)

    def test_master_only_cli_and_raw_responses(self) -> None:
        responses = full_responses()[:6]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_cli(root, responses, mode="master")[0], 0)
            for name, result in zip(MASTER_ENDPOINTS, responses):
                self.assertEqual(json.loads((root / f"master/{name}.json").read_text(encoding="utf-8")), result)

    def test_master_cli_without_dates(self) -> None:
        client = FakeClient(full_responses()[:6])
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                status = run_backup_cli("master", ["--output-dir", temporary], client_factory=lambda: client)
            self.assertEqual(status, 0)

    def test_reports_only_cli_and_tax_option(self) -> None:
        responses = full_responses()
        with tempfile.TemporaryDirectory() as temporary:
            status, client, _, _ = self.run_cli(Path(temporary),
                                              [responses[0]] + responses[-2:],
                                              mode="reports", extra=["--include-tax"])
            self.assertEqual(status, 0)
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(client.calls[-1]["params"]["include_tax"], "true")

    def test_unsupported_master_pagination_fails_closed(self) -> None:
        responses = full_responses()
        responses[1]["metadata"] = {"total_count": 2, "total_pages": 2}
        with tempfile.TemporaryDirectory() as temporary:
            status, _, _, error = self.run_cli(Path(temporary), responses)
            self.assertEqual(status, 1)
            self.assertIn("pagination", error)

    def test_new_fields_adjusting_entry_and_opening_preserved_in_expanded_csv(self) -> None:
        item = journal("synthetic", opening=True)
        row = get_journals.flatten_journal_branches({"journals": [item]})[0]
        self.assertEqual(row["journal_journal_type"], "adjusting_entry")
        self.assertEqual(row["journal_entered_by"], "JOURNAL_TYPE_OPENING")
        self.assertEqual(row["journal_tags"], '["synthetic"]')
        self.assertEqual(row["journal_term_period"], 2024)
        self.assertEqual(row["journal_create_time"], item["create_time"])
        self.assertEqual(row["journal_update_time"], item["update_time"])
        self.assertEqual(row["debitor_tax_long_name"], "synthetic-tax")
        self.assertEqual(row["creditor_tax_long_name"], "synthetic-tax")


class PaginationValidationTests(unittest.TestCase):
    def test_total_count_recognized_in_all_supported_containers(self) -> None:
        for key in ("metadata", "pagination", "paging", "meta", None):
            with self.subTest(key=key):
                payload = {"total_count": "1", "total_pages": 1}
                raw = {"journals": [{"id": "one"}], **({key: payload} if key else payload)}
                result = get_journals.fetch_all_journals(FakeClient([raw]), START, END, 100)
                self.assertTrue(result["backup_metadata"]["total_count_verified"])
                self.assertEqual(result["backup_metadata"]["api_total_count"], 1)

    def test_changing_total_count_is_rejected(self) -> None:
        client = FakeClient([
            {"journals": [{"id": "one"}], "metadata": {"total_count": 2, "total_pages": 2}},
            {"journals": [{"id": "two"}], "metadata": {"total_count": 3, "total_pages": 2}},
        ])
        with self.assertRaisesRegex(BackupError, "変化"):
            get_journals.fetch_all_journals(client, START, END, 100)

    def test_invalid_total_counts_are_rejected(self) -> None:
        for total in (True, -1, 2.5, "invalid", None):
            with self.subTest(total=total), self.assertRaises(BackupError):
                api_total_count({"metadata": {"total_count": total}})

    def test_conflicting_total_containers_rejected(self) -> None:
        with self.assertRaises(BackupError):
            api_total_count({"metadata": {"total_count": 1}, "meta": {"total_count": 2}})

    def test_absent_total_count_is_explicitly_unverified(self) -> None:
        result = get_journals.fetch_all_journals(FakeClient([{"journals": []}]), START, END, 100)
        self.assertIsNone(result["backup_metadata"]["api_total_count"])
        self.assertFalse(result["backup_metadata"]["total_count_verified"])

    def test_malformed_journals_do_not_silently_disappear(self) -> None:
        for raw in ({"unknown": []}, {"journals": [None]}):
            with self.subTest(raw=raw), self.assertRaises(BackupError):
                get_journals.fetch_all_journals(FakeClient([raw]), START, END, 100)

    def test_invalid_or_conflicting_pagination_is_rejected(self) -> None:
        for metadata in (
            {"total_pages": "invalid"}, {"current_page": 2, "total_pages": 2},
            {"next_page": 3}, {"total_pages": 0},
            {"has_next": False, "total_pages": 2}, {"has_next": "true"},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(BackupError):
                get_journals.fetch_all_journals(FakeClient([
                    {"journals": [{"id": "one"}], "metadata": metadata}
                ]), START, END, 100)

    def test_empty_page_with_next_page_is_rejected(self) -> None:
        with self.assertRaisesRegex(BackupError, "空ページ"):
            get_journals.fetch_all_journals(FakeClient([
                {"journals": [], "metadata": {"has_next": True}}
            ]), START, END, 100)

    def test_low_level_savers_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "existing.json"
            path.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                get_journals.save_json({}, path)
            self.assertEqual(path.read_bytes(), b"keep")

    def test_manifest_path_traversal_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = BackupManifest(Path(temporary) / "backup")
            with self.assertRaises(BackupError):
                manifest.target("../outside.json")


class MasterResponseTests(unittest.TestCase):
    def test_sub_accounts_saved_response_shape_and_count(self) -> None:
        # Shape observed locally; all values here are synthetic, never copied.
        raw = {"sub_accounts": [
            {"account_id": "synthetic-account", "id": f"synthetic-{index}",
             "name": "synthetic", "search_key": None, "tax_id": "synthetic-tax"}
            for index in range(100)
        ]}
        before = copy.deepcopy(raw)
        with tempfile.TemporaryDirectory() as temporary:
            manifest = BackupManifest(Path(temporary))
            manifest.register_endpoint("sub_accounts", MASTER_ENDPOINTS["sub_accounts"])
            retrieve_master(FakeClient([raw]), manifest, "sub_accounts")
            saved = json.loads((Path(temporary) / "master/sub_accounts.json").read_text(encoding="utf-8"))
            entry = manifest.data["endpoints"]["sub_accounts"]
            self.assertEqual(entry["retrieved_count"], 100)
            self.assertTrue(entry["complete"])
            self.assertEqual(entry["status"], "success")
            self.assertEqual(saved, before)
            self.assertEqual(raw, before)

    def test_endpoint_specific_arrays_accounts_taxes_departments_trade_partners(self) -> None:
        payloads = {
            "accounts": {"accounts": [{"id": "synthetic", "sub_accounts": []}]},
            "term_settings": {"term_settings": [{"fiscal_year": 2024}]},
            "taxes": {"taxes": [{"id": "synthetic"}]},
            "departments": {"departments": [{"id": "synthetic"}]},
            "trade_partners": {"trade_partners": [{"code": "synthetic"}]},
        }
        for name, raw in payloads.items():
            with self.subTest(endpoint=name), tempfile.TemporaryDirectory() as temporary:
                manifest = BackupManifest(Path(temporary))
                manifest.register_endpoint(name, MASTER_ENDPOINTS[name])
                retrieve_master(FakeClient([raw]), manifest, name)
                entry = manifest.data["endpoints"][name]
                self.assertEqual(entry["retrieved_count"], 1)
                self.assertTrue(entry["response_structure_recognized"])
                self.assertTrue(entry["complete"])

    def test_empty_arrays_are_normal_success_for_all_master_endpoints(self) -> None:
        for name in MASTER_ENDPOINTS:
            with self.subTest(endpoint=name), tempfile.TemporaryDirectory() as temporary:
                manifest = BackupManifest(Path(temporary))
                manifest.register_endpoint(name, MASTER_ENDPOINTS[name])
                retrieve_master(FakeClient([{name: []}]), manifest, name)
                entry = manifest.data["endpoints"][name]
                self.assertEqual(entry["retrieved_count"], 0)
                self.assertTrue(entry["response_structure_recognized"])
                self.assertTrue(entry["success"])
                self.assertTrue(entry["complete"])

    def test_unknown_structure_is_error_not_zero_and_raw_is_saved(self) -> None:
        for name in MASTER_ENDPOINTS:
            for raw in ({"other": []}, {name: None}, {name: {}}, {name: [None]}):
                with self.subTest(endpoint=name, shape=type(raw.get(name)).__name__), tempfile.TemporaryDirectory() as temporary:
                    manifest = BackupManifest(Path(temporary))
                    manifest.register_endpoint(name, MASTER_ENDPOINTS[name])
                    with self.assertRaises(BackupError) as caught:
                        retrieve_master(FakeClient([raw]), manifest, name)
                    manifest.fail(caught.exception, name)
                    entry = manifest.data["endpoints"][name]
                    self.assertIsNone(entry["retrieved_count"])
                    self.assertFalse(entry["response_structure_recognized"])
                    self.assertEqual(entry["status"], "failed")
                    self.assertFalse(entry["complete"])
                    self.assertEqual(json.loads((Path(temporary) / f"master/{name}.json").read_text(encoding="utf-8")), raw)


class ManifestReplacementTests(unittest.TestCase):
    def test_previous_fixed_tmp_does_not_block_or_get_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = BackupManifest(root)
            old_tmp = root / "manifest.json.tmp"
            old_tmp.write_bytes(b"previous staged manifest")
            manifest.register_endpoint("sub_accounts", MASTER_ENDPOINTS["sub_accounts"])
            retrieve_master(FakeClient([{"sub_accounts": []}]), manifest, "sub_accounts")
            self.assertEqual(old_tmp.read_bytes(), b"previous staged manifest")
            saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["endpoints"]["sub_accounts"]["status"], "success")

    def test_transient_replace_permission_error_is_retried(self) -> None:
        original_replace = Path.replace
        attempts = []
        def transient(path, target):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("synthetic temporary lock")
            return original_replace(path, target)
        with tempfile.TemporaryDirectory() as temporary:
            manifest = BackupManifest(Path(temporary))
            manifest.register_endpoint("sub_accounts", MASTER_ENDPOINTS["sub_accounts"])
            with patch.object(Path, "replace", transient), patch("backup_support.time.sleep") as sleep:
                manifest.checkpoint()
            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once_with(0.05)

    def test_sub_accounts_success_replace_failure_records_failed_manifest(self) -> None:
        original_replace = Path.replace
        def blocked_success(path, target):
            staged = json.loads(path.read_text(encoding="utf-8"))
            if staged.get("endpoints", {}).get("sub_accounts", {}).get("status") == "success":
                raise PermissionError("synthetic temporary lock")
            return original_replace(path, target)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(full_responses())
            with (patch.object(Path, "replace", blocked_success), patch("backup_support.time.sleep"),
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
                status = run_backup_cli("fiscal_year", ["--start-date", START, "--end-date", END,
                                                        "--output-dir", temporary], client_factory=lambda: client)
            self.assertEqual(status, 0)
            saved = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "complete_with_warnings")
            entry = saved["endpoints"]["sub_accounts"]
            self.assertEqual(entry["retrieved_count"], 1)
            self.assertEqual(entry["status"], "failed")
            self.assertFalse(entry["complete"])
            self.assertTrue(entry["errors"])
            self.assertEqual(saved["endpoints"]["taxes"]["status"], "success")


if __name__ == "__main__":
    unittest.main()
