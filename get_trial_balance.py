"""Retrieve MF trial balance reports without calculating any accounting data."""
from mf_backup import run_backup_cli


def main() -> int:
    return run_backup_cli("reports")


if __name__ == "__main__":
    raise SystemExit(main())
