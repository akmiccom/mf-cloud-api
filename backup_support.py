"""Local-only backup files, integrity checks and non-secret manifests."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BACKUP_FORMAT_VERSION = 1


class BackupError(RuntimeError):
    """Only locally composed, non-secret validation messages belong here."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_total_count(result: dict[str, Any]) -> int | None:
    values: set[int] = set()
    for container in (result, *(result.get(key) for key in
                               ("metadata", "pagination", "paging", "meta"))):
        if not isinstance(container, dict):
            continue
        for key in ("total_count", "total_entries"):
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, bool) or not (
                isinstance(value, int) or (isinstance(value, str) and value.isdecimal())
            ):
                raise BackupError("API total_countが非負整数ではありません。")
            number = int(value)
            if number < 0:
                raise BackupError("API total_countが非負整数ではありません。")
            values.add(number)
    if len(values) > 1:
        raise BackupError("API total_countが同一レスポンス内で矛盾しています。")
    return next(iter(values), None)


def validate_total(count: int, expected: int | None) -> None:
    if expected is not None and count != expected:
        raise BackupError(f"total_count不一致: 実取得件数={count}, API total_count={expected}")


def file_integrity(path: Path, root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"relative_path": path.relative_to(root).as_posix(),
            "sha256": digest.hexdigest(), "size_bytes": path.stat().st_size}


class BackupManifest:
    """A failed run keeps RAW pages; only files from this run are inventoried.

    Refuse nonempty destinations unless overwrite is explicit. Checkpoint the
    manifest before network access and after each saved file. Unknown exceptions
    are recorded by class only, never by their potentially secret-bearing text.
    """

    def __init__(self, root: Path, *, start_date: str | None = None,
                 end_date: str | None = None, overwrite: bool = False,
                 scope: str = "fiscal_year") -> None:
        self.root = root.resolve()
        self.overwrite = overwrite
        if self.root.exists() and (not self.root.is_dir() or
                                   (any(self.root.iterdir()) and not overwrite)):
            raise BackupError("出力先に既存ファイルがあります。別の出力先か --overwrite を指定してください。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manifest.json"
        if self.path.is_symlink():
            raise BackupError("manifestのシンボリックリンクは上書きしません。")
        self.data: dict[str, Any] = {
            "backup_format_version": BACKUP_FORMAT_VERSION, "created_at": utc_now(),
            "start_date": start_date, "end_date": end_date, "scope": scope,
            "accounting_period": None, "term_period": None,
            "master_data_scope": "snapshot_at_retrieval_not_historical_fiscal_year",
            "status": "incomplete", "complete": False,
            "endpoints": {}, "files": [], "errors": [], "warnings": [],
        }
        # Reserve a valid incomplete manifest before authentication/network calls.
        with self.path.open("w" if overwrite else "x", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def checkpoint(self) -> None:
        # Atomic replacement of our own manifest; no data-file overwrite here.
        # A failed/interrupted replace must not block the next failure checkpoint.
        # Never reuse or remove a previous run's manifest.json.tmp.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                         prefix=".manifest-", suffix=".json.tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        for attempt in range(3):
            try:
                temporary.replace(self.path)
                return
            except PermissionError:
                if attempt == 2:
                    # Keep the newly staged manifest for diagnosis; CLI fails.
                    raise
                time.sleep(0.05 * (attempt + 1))

    def register_endpoint(self, name: str, url: str, *, requirement: str = "required") -> dict[str, Any]:
        entry = {
            "http_method": "GET", "endpoint_path": urlparse(url).path,
            "requirement": requirement,
            "status": "not_started", "success": False, "complete": False,
            "retrieved_count": None, "response_structure_recognized": None,
            "page_count": 0, "api_total_count": None,
            "total_count_verified": False, "files": [], "errors": [],
            "requests": [],
        }
        self.data["endpoints"][name] = entry
        return entry

    def start(self, name: str) -> None:
        self.data["endpoints"][name]["status"] = "incomplete"
        self.checkpoint()

    def target(self, relative: str) -> Path:
        path = self.root / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise BackupError("保存先がバックアップディレクトリ外です。")
        if path.exists() and not self.overwrite:
            raise BackupError("保存先ファイルが既に存在します。")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def record_file(self, name: str, path: Path) -> None:
        record = file_integrity(path, self.root)
        self.data["files"].append(record)
        self.data["endpoints"][name]["files"].append(record["relative_path"])
        self.checkpoint()

    def save_json(self, name: str, relative: str, result: dict[str, Any]) -> None:
        path = self.target(relative)
        with path.open("w" if self.overwrite else "x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        self.record_file(name, path)

    def succeed(self, name: str, *, complete: bool = True) -> None:
        entry = self.data["endpoints"][name]
        entry.update(status="success", success=True, complete=complete)
        self.checkpoint()

    def fail(self, exc: BaseException, name: str | None = None, *, fatal: bool = True) -> None:
        message = str(exc) if isinstance(exc, BackupError) else (
            f"{type(exc).__name__}: 取得・保存に失敗しました（詳細本文は秘密保護のため非記録）。"
        )
        if name:
            entry = self.data["endpoints"][name]
            entry.update(status="failed", success=False, complete=False)
            entry["errors"].append(message)
        self.data.update(status="failed", complete=False)
        if not fatal and name and entry["requirement"] != "required":
            self.data["warnings"].append({"endpoint": name,
                                          "requirement": entry["requirement"],
                                          "message": message,
                                          "http_status": entry.get("last_http_status")})
        else:
            self.data["errors"].append(message)
        self.checkpoint()

    def finish(self) -> None:
        entries = [entry for entry in self.data["endpoints"].values()
                   if entry["requirement"] == "required"]
        success = (bool(entries) and not self.data["errors"]
                   and all(entry["success"] for entry in entries))
        complete = success and all(entry["complete"] for entry in entries)
        status = "success" if success else "failed"
        if complete and self.data["warnings"]:
            status = "complete_with_warnings"
        self.data.update(status=status, complete=complete,
                         finished_at=utc_now())
        self.checkpoint()
