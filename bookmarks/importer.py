"""Parser for the Netscape bookmark HTML format exported by Chrome, Edge and Firefox."""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

# Browser root containers; they carry no meaning, so they do not become tags.
ROOT_FOLDERS = {"bookmarks bar", "bookmarks toolbar", "bookmarks menu", "other bookmarks", "mobile bookmarks"}


@dataclass
class ImportedLink:
    url: str
    title: str
    folders: list[str] = field(default_factory=list)  # folder path, outermost first
    add_date: int | None = None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[ImportedLink] = []
        self._stack: list[str | None] = []  # folder name for each open <DL>
        self._pending_folder: str | None = None
        self._h3_text: list[str] | None = None
        self._anchor: dict[str, str | None] | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._h3_text = []
        elif tag == "dl":
            self._stack.append(self._pending_folder)
            self._pending_folder = None
        elif tag == "a":
            self._anchor = dict(attrs)
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._h3_text is not None:
            self._pending_folder = "".join(self._h3_text).strip()
            self._h3_text = None
        elif tag == "dl" and self._stack:
            self._stack.pop()
        elif tag == "a" and self._anchor is not None:
            self._emit(self._anchor, "".join(self._anchor_text).strip())
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._h3_text is not None:
            self._h3_text.append(data)
        elif self._anchor is not None:
            self._anchor_text.append(data)

    def _emit(self, attrs: dict[str, str | None], title: str) -> None:
        url = (attrs.get("href") or "").strip()
        if not url:
            return
        folders = [name for name in self._stack if name]
        if folders and folders[0].lower() in ROOT_FOLDERS:
            folders = folders[1:]
        add_date = attrs.get("add_date")
        self.links.append(ImportedLink(
            url=url,
            title=title,
            folders=folders,
            add_date=int(add_date) if add_date and add_date.isdigit() else None,
        ))


def parse_bookmarks_html(html: str) -> list[ImportedLink]:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    return parser.links
