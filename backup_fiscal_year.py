"""Back up all nine required GET endpoints for one MF accounting period."""
from mf_backup import run_backup_cli


def main() -> int:
    return run_backup_cli("fiscal_year")


if __name__ == "__main__":
    raise SystemExit(main())
