"""Writer for the Netscape bookmark HTML format that Chrome, Edge and Firefox import."""
from __future__ import annotations

from html import escape


def _anchor(link: dict, indent: str) -> str:
    attrs = f'HREF="{escape(link["url"], quote=True)}"'
    if link.get("created_at"):
        attrs += f' ADD_DATE="{int(link["created_at"])}"'
    title = link.get("title") or link["url"]
    return f"{indent}<DT><A {attrs}>{escape(title)}</A>"


def export_chrome_html(links: list[dict]) -> str:
    """Every tag becomes a folder on the bookmarks bar; a link with several tags is listed in each
    of them, and untagged links go directly on the bar."""
    by_tag: dict[str, list[dict]] = {}
    untagged: list[dict] = []
    for link in links:
        for tag in link.get("tags") or []:
            by_tag.setdefault(tag, []).append(link)
        if not link.get("tags"):
            untagged.append(link)

    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
        '    <DT><H3 PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>',
        "    <DL><p>",
    ]
    for tag in sorted(by_tag):
        lines.append(f"        <DT><H3>{escape(tag)}</H3>")
        lines.append("        <DL><p>")
        lines.extend(_anchor(link, " " * 12) for link in by_tag[tag])
        lines.append("        </DL><p>")
    lines.extend(_anchor(link, " " * 8) for link in untagged)
    lines += ["    </DL><p>", "</DL><p>", ""]
    return "\n".join(lines)
