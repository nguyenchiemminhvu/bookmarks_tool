"""Command line: `python -m bookmarks [--port N] [--no-browser]` or `python -m bookmarks import FILE`."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from .backup import BackupScheduler
from .credentials import CredentialStore
from .importer import parse_bookmarks_html
from .server import make_server
from .store import Store

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEGACY_DATA_DIR = Path.home() / ".bookmarks"
DEFAULT_PORT = 47213  # 8765-style ports collide with dev tools (e.g. VS Code extensions)


def migrate_legacy_data(data_dir: Path, legacy_dir: Path = LEGACY_DATA_DIR) -> bool:
    """Copy a pre-existing ~/.bookmarks database (and credentials) into an empty ``data_dir``."""
    old_db, new_db = legacy_dir / "bookmarks.db", data_dir / "bookmarks.db"
    if new_db.exists() or not old_db.is_file():
        return False
    src, dst = sqlite3.connect(old_db), sqlite3.connect(new_db)
    try:
        src.backup(dst)  # consistent copy even if the old server left WAL data behind
    finally:
        dst.close()
        src.close()
    old_creds, new_creds = legacy_dir / "credentials.json", data_dir / "credentials.json"
    if old_creds.is_file() and not new_creds.exists():
        CredentialStore(new_creds).adopt(CredentialStore(old_creds))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bookmarks", description="Tag-based bookmark dashboard.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help=f"where bookmarks.db, credentials.json and backups live (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard in a browser")
    commands = parser.add_subparsers(dest="command")
    import_cmd = commands.add_parser(
        "import", help="import a browser bookmarks HTML export (folders become tags) or a .json backup")
    import_cmd.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.data_dir == DEFAULT_DATA_DIR and migrate_legacy_data(args.data_dir):
        print(f"Copied existing data from {LEGACY_DATA_DIR} to {args.data_dir} (the old folder is left untouched).")
    store = Store(args.data_dir / "bookmarks.db")

    if args.command == "import":
        text = args.file.read_text(encoding="utf-8")
        if args.file.suffix.lower() == ".json":
            result = store.import_backup(json.loads(text))
        else:
            result = store.import_links(parse_bookmarks_html(text))
        print(f"Imported {result['added']} new links, merged {result['merged']} into existing links, "
              f"skipped {result['skipped']}.")
        return 0

    credentials = CredentialStore(args.data_dir / "credentials.json")
    try:
        server = make_server(store, credentials, args.port)
    except OSError as exc:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {exc}\n"
              f"Another program is probably using port {args.port}. Start with a different one, e.g. "
              f"python -m bookmarks --port {args.port + 1}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Bookmarks dashboard: {url}\nData directory: {args.data_dir}\nPress Ctrl+C to stop.")
    backups = BackupScheduler(store, args.data_dir)
    backups.start()
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backups.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
