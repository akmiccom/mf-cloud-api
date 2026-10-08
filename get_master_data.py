"""Save the six master API responses as current RAW JSON snapshots."""
from mf_backup import run_backup_cli


def main() -> int:
    return run_backup_cli("master")


if __name__ == "__main__":
    raise SystemExit(main())
