"""SQLite storage for links and their tags."""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .errors import DuplicateURL, NotFound
from .importer import ImportedLink
from .tags import normalize_tag, normalize_tags

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id           INTEGER PRIMARY KEY,
    url          TEXT    NOT NULL UNIQUE,
    title        TEXT    NOT NULL DEFAULT '',
    notes        TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    check_status TEXT,
    check_code   INTEGER,
    check_error  TEXT,
    checked_at   INTEGER
);
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT    NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS link_tags (
    link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (link_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS link_tags_tag ON link_tags(tag_id);
"""

_SELECT = """
SELECT l.*,
       (SELECT group_concat(t.name, ' ')
          FROM link_tags lt JOIN tags t ON t.id = lt.tag_id
         WHERE lt.link_id = l.id) AS tag_names
  FROM links l
"""

# URLs with these schemes are stored verbatim; anything else without "://" gets "https://".
_KEEP_SCHEMES = re.compile(r"^(mailto|javascript|data|about|file|tel|chrome|edge):", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL is required")
    if "://" not in url and not _KEEP_SCHEMES.match(url):
        url = "https://" + url
    return url


def _to_dict(row: sqlite3.Row) -> dict:
    link = dict(row)
    names = link.pop("tag_names")
    link["tags"] = sorted(names.split()) if names else []
    return link


def _get(con: sqlite3.Connection, link_id: int) -> dict:
    row = con.execute(_SELECT + " WHERE l.id = ?", (link_id,)).fetchone()
    if row is None:
        raise NotFound(f"link {link_id} not found")
    return _to_dict(row)


def _add_tags(con: sqlite3.Connection, link_id: int, tags: Iterable[str]) -> None:
    for tag in tags:
        con.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
        con.execute(
            "INSERT OR IGNORE INTO link_tags(link_id, tag_id) SELECT ?, id FROM tags WHERE name = ?",
            (link_id, tag),
        )


def _prune_tags(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM link_tags)")


def _existing_ids(con: sqlite3.Connection, ids: list[int]) -> list[int]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [row[0] for row in con.execute(f"SELECT id FROM links WHERE id IN ({marks})", ids)]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._tx() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=15)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            with con:
                yield con
        finally:
            con.close()

    # ---- links -----------------------------------------------------------

    def list_links(self) -> list[dict]:
        with self._tx() as con:
            rows = con.execute(_SELECT + " ORDER BY l.created_at DESC, l.id DESC").fetchall()
        return [_to_dict(row) for row in rows]

    def get_link(self, link_id: int) -> dict:
        with self._tx() as con:
            return _get(con, link_id)

    def add_link(self, url: str, title: str = "", notes: str = "", tags: Iterable[str] = (),
                 created_at: int | None = None) -> dict:
        url = normalize_url(url)
        now = int(time.time())
        with self._tx() as con:
            try:
                cur = con.execute(
                    "INSERT INTO links(url, title, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (url, title.strip(), notes.strip(), created_at or now, now),
                )
            except sqlite3.IntegrityError:
                raise DuplicateURL(f"URL already saved: {url}") from None
            _add_tags(con, cur.lastrowid, normalize_tags(tags))
            return _get(con, cur.lastrowid)

    def update_link(self, link_id: int, url: str, title: str = "", notes: str = "",
                    tags: Iterable[str] = ()) -> dict:
        url = normalize_url(url)
        with self._tx() as con:
            old = _get(con, link_id)
            try:
                con.execute(
                    "UPDATE links SET url = ?, title = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (url, title.strip(), notes.strip(), int(time.time()), link_id),
                )
            except sqlite3.IntegrityError:
                raise DuplicateURL(f"URL already saved: {url}") from None
            if url != old["url"]:
                con.execute(
                    "UPDATE links SET check_status = NULL, check_code = NULL, check_error = NULL,"
                    " checked_at = NULL WHERE id = ?",
                    (link_id,),
                )
            con.execute("DELETE FROM link_tags WHERE link_id = ?", (link_id,))
            _add_tags(con, link_id, normalize_tags(tags))
            _prune_tags(con)
            return _get(con, link_id)

    def delete_link(self, link_id: int) -> None:
        with self._tx() as con:
            if con.execute("DELETE FROM links WHERE id = ?", (link_id,)).rowcount == 0:
                raise NotFound(f"link {link_id} not found")
            _prune_tags(con)

    def delete_links(self, ids: list[int]) -> None:
        with self._tx() as con:
            con.executemany("DELETE FROM links WHERE id = ?", [(i,) for i in ids])
            _prune_tags(con)

    def retag(self, ids: list[int], add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        add, remove = normalize_tags(add), normalize_tags(remove)
        now = int(time.time())
        with self._tx() as con:
            for link_id in _existing_ids(con, ids):
                _add_tags(con, link_id, add)
                con.executemany(
                    "DELETE FROM link_tags WHERE link_id = ? AND tag_id IN (SELECT id FROM tags WHERE name = ?)",
                    [(link_id, tag) for tag in remove],
                )
                con.execute("UPDATE links SET updated_at = ? WHERE id = ?", (now, link_id))
            _prune_tags(con)

    def link_urls(self, ids: list[int] | None = None) -> list[tuple[int, str]]:
        with self._tx() as con:
            if ids is None:
                return [(r[0], r[1]) for r in con.execute("SELECT id, url FROM links")]
            existing = _existing_ids(con, ids)
            if not existing:
                return []
            marks = ",".join("?" * len(existing))
            return [(r[0], r[1]) for r in con.execute(f"SELECT id, url FROM links WHERE id IN ({marks})", existing)]

    def set_check_result(self, link_id: int, status: str, code: int | None, error: str | None,
                         checked_at: int) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE links SET check_status = ?, check_code = ?, check_error = ?, checked_at = ? WHERE id = ?",
                (status, code, error, checked_at, link_id),
            )

    # ---- tags ------------------------------------------------------------

    def rename_tag(self, old: str, new: str) -> str:
        """Rename ``old`` to ``new``; if ``new`` already exists the two tags are merged."""
        new = normalize_tag(new)
        if not new:
            raise ValueError("new tag name is empty after normalization (use lowercase ASCII)")
        with self._tx() as con:
            row = con.execute("SELECT id FROM tags WHERE name = ?", (old,)).fetchone()
            if row is None:
                raise NotFound(f"tag {old!r} not found")
            if new != old:
                con.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (new,))
                con.execute(
                    "INSERT OR IGNORE INTO link_tags(link_id, tag_id)"
                    " SELECT link_id, (SELECT id FROM tags WHERE name = ?) FROM link_tags WHERE tag_id = ?",
                    (new, row[0]),
                )
                con.execute("DELETE FROM tags WHERE id = ?", (row[0],))
        return new

    def delete_tag(self, name: str) -> None:
        with self._tx() as con:
            if con.execute("DELETE FROM tags WHERE name = ?", (name,)).rowcount == 0:
                raise NotFound(f"tag {name!r} not found")

    # ---- export / import -------------------------------------------------

    def export_data(self) -> dict:
        return {"exported_at": int(time.time()), "links": self.list_links()}

    def import_backup(self, data: object) -> dict:
        """Restore links from an ``export_data`` document; known URLs only gain the backup's tags."""
        links = data.get("links") if isinstance(data, dict) else None
        if not isinstance(links, list):
            raise ValueError("not a bookmarks backup: 'links' list missing")
        added = merged = skipped = 0
        now = int(time.time())

        def text(link: dict, key: str) -> str:
            value = link.get(key)
            return value.strip() if isinstance(value, str) else ""

        def number(link: dict, key: str) -> int | None:
            value = link.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        with self._tx() as con:
            for link in links:
                try:
                    if not isinstance(link, dict) or not isinstance(link.get("url"), str):
                        raise ValueError
                    url = normalize_url(link["url"])
                except ValueError:
                    skipped += 1
                    continue
                raw_tags = link.get("tags")
                tags = normalize_tags([t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else [])
                row = con.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
                if row is not None:
                    _add_tags(con, row[0], tags)
                    merged += 1
                    continue
                status = link.get("check_status")
                cur = con.execute(
                    "INSERT INTO links(url, title, notes, created_at, updated_at,"
                    " check_status, check_code, check_error, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (url, text(link, "title"), text(link, "notes"), number(link, "created_at") or now,
                     number(link, "updated_at") or now, status if isinstance(status, str) else None,
                     number(link, "check_code"), text(link, "check_error") or None, number(link, "checked_at")),
                )
                _add_tags(con, cur.lastrowid, tags)
                added += 1
        return {"added": added, "merged": merged, "skipped": skipped}

    def import_links(self, items: Iterable[ImportedLink]) -> dict:
        """Insert new URLs; for URLs already stored, add the imported tags."""
        added = merged = skipped = 0
        now = int(time.time())
        with self._tx() as con:
            for item in items:
                try:
                    url = normalize_url(item.url)
                except ValueError:
                    skipped += 1
                    continue
                tags = normalize_tags(item.folders)
                row = con.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
                if row is not None:
                    _add_tags(con, row[0], tags)
                    merged += 1
                    continue
                cur = con.execute(
                    "INSERT INTO links(url, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (url, item.title.strip(), item.add_date or now, now),
                )
                _add_tags(con, cur.lastrowid, tags)
                added += 1
        return {"added": added, "merged": merged, "skipped": skipped}
