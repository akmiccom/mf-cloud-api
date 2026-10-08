from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Any

import get_journals
from mf_auth import OAuthError


class FakeClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "params": params})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def journal_page(
    journals: list[dict[str, Any]],
    **metadata: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {"journals": journals}
    if metadata:
        result["pagination"] = metadata
    return result


class FetchAllJournalsTests(unittest.TestCase):
    def test_single_short_page_stops_after_one_request(self) -> None:
        client = FakeClient([journal_page([{"id": "one"}])])

        result = get_journals.fetch_all_journals(
            client, "2025-11-01", "2026-10-31", 100
        )

        self.assertEqual(result["journals"], [{"id": "one"}])
        self.assertEqual(len(client.calls), 1)

    def test_fetches_full_page_then_short_page(self) -> None:
        client = FakeClient(
            [
                journal_page([{"id": str(index)} for index in range(100)]),
                journal_page([{"id": str(index)} for index in range(100, 120)]),
            ]
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = get_journals.fetch_all_journals(
                client, "2025-11-01", "2026-10-31", 100
            )

        self.assertEqual(len(result["journals"]), 120)
        self.assertEqual([call["params"]["page"] for call in client.calls], [1, 2])
        self.assertIn("ページ 2: 20件 (累計 120件)", output.getvalue())

    def test_full_pages_stop_on_empty_page(self) -> None:
        client = FakeClient(
            [
                journal_page([{"id": f"a{index}"} for index in range(100)]),
                journal_page([{"id": f"b{index}"} for index in range(100)]),
                journal_page([]),
            ]
        )

        result = get_journals.fetch_all_journals(
            client, "2025-11-01", "2026-10-31", 100
        )

        self.assertEqual(len(result["journals"]), 200)
        self.assertEqual(len(client.calls), 3)

    def test_explicit_pagination_metadata_controls_continuation(self) -> None:
        client = FakeClient(
            [
                journal_page([{"id": "one"}], current_page=1, total_pages=2),
                journal_page([{"id": "two"}], current_page=2, total_pages=2),
            ]
        )

        result = get_journals.fetch_all_journals(
            client, "2025-11-01", "2026-10-31", 100
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual([journal["id"] for journal in result["journals"]], [
            "one",
            "two",
        ])

    def test_mid_fetch_api_error_preserves_raw_pages_and_failed_manifest(self) -> None:
        client = FakeClient(
            [
                journal_page([{"id": str(index)} for index in range(100)]),
                OAuthError("simulated API failure"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "output"
            argv = [
                "get_journals.py",
                "--start-date",
                "2025-11-01",
                "--end-date",
                "2026-10-31",
                "--all-pages",
                "--output-dir",
                str(output_dir),
            ]

            with (
                patch.object(sys, "argv", argv),
                patch.object(get_journals, "MoneyForwardClient", return_value=client),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = get_journals.main()

            self.assertEqual(status, 1)
            self.assertTrue((output_dir / "journals/pages/page_0001.json").exists())
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["complete"])
            self.assertFalse((output_dir / "journals_2025-11-01_2026-10-31_all.json").exists())

    def test_maximum_page_limit_raises_instead_of_returning_partial_data(self) -> None:
        full_page = journal_page([{"id": "one"}, {"id": "two"}])
        client = FakeClient([full_page, full_page])

        with self.assertRaisesRegex(OAuthError, "安全上限"):
            get_journals.fetch_all_journals(
                client,
                "2025-11-01",
                "2026-10-31",
                2,
                max_pages=2,
            )

        self.assertEqual(len(client.calls), 2)


class JournalCliTests(unittest.TestCase):
    def test_standard_and_composite_branches_expand_to_one_row_each(self) -> None:
        result = journal_page(
            [
                {
                    "number": 42,
                    "transaction_date": "2025-11-01",
                    "id": "journal-42",
                    "entered_by": "担当者",
                    "is_realized": True,
                    "journal_type": "expense",
                    "memo": "昼食",
                    "transaction_id": "transaction-42",
                    "voucher_file_ids": ["voucher-1"],
                    "branches": [
                        {
                            "remark": "一行目",
                            "debitor": {
                                "account_name": "福利厚生費",
                                "sub_account_name": "昼食費",
                                "value": 303,
                                "account_id": "debit-account",
                            },
                            "creditor": {
                                "account_name": "未払金",
                                "value": 303,
                                "tax_name": "対象外",
                            },
                        },
                        {
                            "remark": "二行目",
                            "debitor": {
                                "account_name": "消耗品費",
                                "value": 100,
                            },
                            "creditor": {"account_name": "現金", "value": 100},
                        },
                    ],
                }
            ]
        )

        rows = get_journals.flatten_journal_branches(result)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["branch_index"] for row in rows], [1, 2])
        self.assertEqual(
            [row["journal_id"] for row in rows],
            ["journal-42", "journal-42"],
        )
        self.assertEqual(rows[0]["journal_transaction_date"], "2025-11-01")
        self.assertEqual(rows[0]["debitor_account_name"], "福利厚生費")
        self.assertEqual(rows[0]["debitor_sub_account_name"], "昼食費")
        self.assertEqual(rows[0]["debitor_value"], 303)
        self.assertEqual(rows[0]["creditor_account_name"], "未払金")
        self.assertEqual(rows[0]["remark"], "一行目")
        self.assertEqual(rows[0]["debitor_account_id"], "debit-account")
        self.assertEqual(rows[0]["creditor_tax_name"], "対象外")
        self.assertEqual(rows[0]["journal_voucher_file_ids"], '["voucher-1"]')
        self.assertEqual(
            set(get_journals.JOURNAL_BRANCH_COLUMNS),
            set(rows[0]),
        )

    def test_null_debitor_or_creditor_becomes_blank_columns(self) -> None:
        result = journal_page(
            [
                {
                    "id": "journal-null-side",
                    "branches": [
                        {"remark": "借方なし", "debitor": None, "creditor": {}},
                        {"remark": "貸方なし", "debitor": {}, "creditor": None},
                    ],
                }
            ]
        )

        rows = get_journals.flatten_journal_branches(result)

        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["debitor_account_name"], "")
            self.assertEqual(row["debitor_value"], "")
            self.assertEqual(row["creditor_account_name"], "")
            self.assertEqual(row["creditor_value"], "")

    def test_expanded_csv_has_utf8_bom_and_preserves_japanese_and_numbers(self) -> None:
        rows = [
            {
                "journal_index": 1,
                "journal_transaction_date": "2025-11-01",
                "branch_index": 1,
                "remark": "セブン-イレブン",
                "debitor_account_name": "福利厚生費",
                "debitor_sub_account_name": "昼食費",
                "debitor_value": 303,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "expanded.csv"
            get_journals.save_expanded_csv(rows, output_path)

            contents = output_path.read_bytes()
            self.assertTrue(contents.startswith(b"\xef\xbb\xbf"))
            with output_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as file:
                saved_rows = list(csv.DictReader(file))

        self.assertEqual(saved_rows[0]["remark"], "セブン-イレブン")
        self.assertEqual(saved_rows[0]["debitor_account_name"], "福利厚生費")
        self.assertEqual(saved_rows[0]["debitor_sub_account_name"], "昼食費")
        self.assertEqual(saved_rows[0]["debitor_value"], "303")
        self.assertIn(b",303,", contents)

    def test_duplicate_ids_are_reported_without_deduplication(self) -> None:
        journals = [
            {"id": "duplicate"},
            {"id": "unique"},
            {"id": "duplicate"},
            {"id": "duplicate"},
        ]

        self.assertEqual(
            get_journals.detect_duplicate_journals(journals),
            {"duplicate": 2},
        )

    def test_legacy_single_page_cli_saves_page_one_filenames(self) -> None:
        result = journal_page(
            [{"id": "one", "branches": [{"debitor": {}, "creditor": {}}]}]
        )
        client = FakeClient([result])
        with tempfile.TemporaryDirectory() as temporary_directory:
            argv = [
                "get_journals.py",
                "--start-date",
                "2025-11-01",
                "--end-date",
                "2026-10-31",
                "--page",
                "1",
                "--per-page",
                "100",
                "--output-dir",
                temporary_directory,
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(get_journals, "MoneyForwardClient", return_value=client),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = get_journals.main()

            self.assertEqual(status, 0)
            self.assertEqual(len(client.calls), 1)
            raw_csv = (
                Path(temporary_directory)
                / "journals_2025-11-01_2026-10-31_page1.csv"
            )
            raw_json = (
                Path(temporary_directory)
                / "journals_2025-11-01_2026-10-31_page1.json"
            )
            self.assertTrue(
                raw_json.exists()
            )
            self.assertTrue(raw_csv.exists())
            self.assertEqual(
                json.loads(raw_json.read_text(encoding="utf-8")),
                result,
            )
            with raw_csv.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as file:
                raw_rows = list(csv.DictReader(file))
            self.assertEqual(
                raw_rows[0]["journal_branches"],
                '[{"debitor": {}, "creditor": {}}]',
            )
            self.assertTrue(
                (
                    Path(temporary_directory)
                    / "journals_2025-11-01_2026-10-31_page1_expanded.csv"
                ).exists()
            )

    def test_all_pages_cli_saves_distinct_filenames(self) -> None:
        client = FakeClient(
            [
                journal_page(
                    [
                        {
                            "id": "one",
                            "branches": [
                                {"debitor": {}, "creditor": {}}
                            ],
                        }
                    ],
                    current_page=1,
                    total_pages=1,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            argv = [
                "get_journals.py",
                "--start-date",
                "2025-11-01",
                "--end-date",
                "2026-10-31",
                "--all-pages",
                "--output-dir",
                temporary_directory,
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    get_journals,
                    "MoneyForwardClient",
                    return_value=client,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = get_journals.main()

            self.assertEqual(status, 0)
            self.assertEqual(client.calls[0]["params"]["per_page"], 100)
            self.assertTrue(
                (
                    Path(temporary_directory)
                    / "journals_2025-11-01_2026-10-31_all.json"
                ).exists()
            )
            self.assertTrue(
                (
                    Path(temporary_directory)
                    / "journals_2025-11-01_2026-10-31_all.csv"
                ).exists()
            )
            self.assertTrue(
                (
                    Path(temporary_directory)
                    / "journals_2025-11-01_2026-10-31_expanded.csv"
                ).exists()
            )

    def test_all_pages_and_explicit_page_are_rejected(self) -> None:
        argv = ["get_journals.py", "--all-pages", "--page", "3"]
        with (
            patch.object(sys, "argv", argv),
            patch.object(get_journals, "MoneyForwardClient") as client_class,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = get_journals.main()

        self.assertEqual(status, 2)
        client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
