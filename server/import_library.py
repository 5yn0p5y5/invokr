"""
Register files you already have, so the extension stops re-downloading them.

    python import_library.py                                  # scan the library folder
    python import_library.py --path "D:\\Music"                # scan somewhere else
    python import_library.py --path "D:\\Music" --dry-run      # report only
    python import_library.py --rebase-from "D:\\Old\\library"  # after moving the archive

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
    parser.add_argument(
        "--rebase-from",
        default=None,
        metavar="OLD_LIBRARY",
        help="after moving the archive, rewrite recorded paths that start with "
        "this old library folder so they point inside the current one",
    )
    args = parser.parse_args(argv)

    db.configure(CONFIG.db_path, library_root=CONFIG.library_path)

    if args.rebase_from:
        # Paths recorded before a move are still absolute. Nothing is touched on
        # disk: this only re-points the index at where the files now live.
        rows = db.rebase_paths(args.rebase_from)
        db.record_event(
            "paths_rebased",
            detail={
                "from": args.rebase_from,
                "to": str(CONFIG.library_path),
                "rows": rows,
            },
        )
        print(
            f"rewrote {rows} recorded path(s)\n"
            f"  from {args.rebase_from}\n"
            f"  to   {CONFIG.library_path}",
            file=sys.stderr,
        )
        return 0

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
