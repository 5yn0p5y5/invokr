"""
Register files you already have, so the extension stops re-downloading them.

    python import_library.py                                  # scan the library folder
    python import_library.py --path "D:\\Music"                # scan somewhere else
    python import_library.py --path "D:\\Music" --dry-run      # report only

Reads the comment tag and freeform atoms, so files written by spotDL are
identified exactly (YouTube URL in the comment, Spotify URL in WOAS) rather than
by guessing from filenames.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from invokr import db, library
from invokr.config import CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register already-downloaded songs.")
    parser.add_argument(
        "--path",
        default=None,
        help="folder to scan (default: the configured library folder)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, change nothing"
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="do not descend into subfolders"
    )
    args = parser.parse_args(argv)

    db.configure(CONFIG.db_path)

    root = Path(args.path) if args.path else CONFIG.library_path
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    records = library.scan(root, recursive=not args.no_recursive)
    report = library.import_records(records, dry_run=args.dry_run, root=str(root))

    print(json.dumps(report.to_dict(), indent=2))

    print(
        f"\n{report.scanned} file(s) scanned under {root}\n"
        f"  {len(report.added)} {'would be added' if args.dry_run else 'added'}\n"
        f"  {report.already_known_count} already recorded\n"
        f"  {report.unreadable_count} unreadable",
        file=sys.stderr,
    )
    if args.dry_run:
        print("\n(dry run — nothing was written)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
