# Bookmarks dashboard

A local, tag-based bookmark manager with a web dashboard. Pure Python standard library (3.10+):
nothing to install.

- Add, edit, delete links; bulk add/remove tags, check, or delete.
- Tags instead of folders. Filter by several tags at once (AND); tag counts narrow as you filter.
- Tag search: every word typed in the search box must match one of a link's tags (AND), so more words narrow the results.
- Link liveness checks, with optional per-host credentials for intranet tools.
- Import a browser bookmarks HTML export (each folder name on a link's path becomes a tag) or a JSON backup.
- Data lives in `data/` inside this project folder, with an automatic weekly JSON backup next to the database.

## Run

```sh
python -m bookmarks                      # serves http://127.0.0.1:47213/ and opens the browser
python -m bookmarks --port 9000 --no-browser
python -m bookmarks import current_google_bookmarks.html
python -m bookmarks import data/bookmarks-backup-20260101-120000.json
python -m bookmarks --data-dir D:/bm     # use another data directory
```

Run from this folder, or put it on `PYTHONPATH`. The server listens on 127.0.0.1 only.

## Run in the background

Start the dashboard automatically, without a console window. Run it as your own user, not as a system
service: data is read from the `data/` folder next to `bookmarks/` (or next to the exe), and `credentials.json` is restricted
to your account. Always pass `--no-browser`. While it runs, open `http://127.0.0.1:47213/` directly;
`python -m bookmarks import FILE` still works alongside it, but a second server fails on the busy port.

### Windows: PyInstaller executable (recommended)

Build a standalone, windowless `bookmarks.exe` once with [PyInstaller](https://pyinstaller.org). It needs no
Python on the machine that runs it and avoids `pythonw.exe` path problems.

```powershell
pip install pyinstaller
pyinstaller --onefile --noconsole --name bookmarks --add-data "bookmarks\static;bookmarks\static" run_bookmarks.py
```

The result is `dist\bookmarks.exe` (`run_bookmarks.py` is the entry point; `--noconsole` means no console
window). Copy it to a permanent folder, e.g. `D:\tools\bookmarks\`. The exe keeps its data in a `data\` folder
**next to the exe** (not in a temporary extraction folder), so existing data can be copied there or the exe
pointed at it with `--data-dir`. Rebuild and replace the exe to update; `data\` is untouched.

Put a shortcut to `bookmarks.exe` in `shell:startup`, add
`--no-browser` to its target, and set *Start in* to the exe's folder.

Without building an exe, `pythonw.exe -m bookmarks --no-browser` (from `<python folder>`, *Start in* set to
this folder) also runs without a console window, but depends on the Python install staying in place.

### Linux: systemd user service

`~/.config/systemd/user/bookmarks.service` (set `WorkingDirectory` to the folder containing `bookmarks/`):

```ini
[Unit]
Description=Bookmarks dashboard

[Service]
WorkingDirectory=%h/tools/bookmarks
ExecStart=/usr/bin/python3 -m bookmarks --no-browser
Environment=PYTHONUNBUFFERED=1
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now bookmarks
loginctl enable-linger "$USER"     # start at boot, not only at login
journalctl --user -u bookmarks -f  # logs
```

## Data

| File | Content |
| --- | --- |
| `data/bookmarks.db` | SQLite: `links`, `tags`, `link_tags` |
| `data/credentials.json` | Per-host credentials (plain JSON, see below) |
| `data/bookmarks-backup-YYYYMMDD-HHMMSS.json` | Automatic backup: every link, tag, and check result; no credentials |

`data/` sits next to the `bookmarks/` package (git-ignored), so it moves with the project folder. Use
`--data-dir` to put it elsewhere. On the first start, an existing `~/.bookmarks/` database and credentials
are copied into `data/` (the old folder is left untouched and can be deleted).

### Backups

While the server runs, it writes a backup right after start-up if the newest one is older than 7 days, and
re-checks every hour, so one is created about once a week (a server that is off for longer simply backs up at
its next start). The newest 8 backups are kept. **Export** opens a dialog where you tick the formats to download:
the JSON backup, a bookmarks HTML file for Chrome's *Import bookmarks* feature, or both. In the Chrome file each tag
becomes a folder on the bookmarks bar, a link with several tags appears in every one of them (Chrome keeps the
duplicates), and untagged links sit directly on the bar.

If the server or database is unavailable, start a fresh one (`--data-dir` pointing at an empty folder, or
delete the broken `bookmarks.db`) and restore with **Import** in the UI or
`python -m bookmarks import <backup>.json`; search and tag filtering then work again. Importing into a database
that already has the URLs only adds the backup's tags to them.

## Tags

Tags are lowercase ASCII: `a-z`, `0-9`, joined by `-`, `_` or `.`. Any input is normalized: `BMW ICON 25` →
`bmw-icon-25`, `C/C++` → `c-cpp`, `R&S` → `r-and-s`, accents are stripped.

On import, `Bookmarks bar/Working/BMW ICON 25/Collab/<link>` gets the tags `working bmw-icon-25 collab`.
A URL that appears in several folders becomes one link carrying all their tags. Clean up afterwards from
the sidebar: hover a tag → **rename** (renaming to an existing tag merges both) or **delete** (the links stay).

Suggested scheme: one tag per dimension, combined at search time.

- organization: `lgedv`
- project: `bmw-icon-25`, `gm-gvm`, `hkmc-xv1`
- tool: `jira`, `collab`, `polarion`, `codebeamer`, `jenkins`, `opengrok`, `git`
- topic: `cpp`, `python`, `yocto`, `zephyr`, `autosar`, `aspice`, `english`
- workflow: `buffer` (temporary), `utility`

Selecting `bmw-icon-25` + `jira` then shows exactly that project's Jira links.

## Link status

| Status | Meaning |
| --- | --- |
| OK | 2xx/3xx |
| Login required | 401/403/407, or redirected to a login/SSO page |
| Broken | 404/410 |
| Unreachable | DNS failure, connection refused/reset, timeout (also intranet hosts when off the corporate network) |
| Error | Other HTTP errors (5xx, 400, 429, ...) or protocol failures |
| Skipped | Not an http(s) URL |

**Check all** checks every link visible under the current filters (16 in parallel, 10 s timeout).
It tries HEAD first and retries with GET when HEAD fails. TLS certificates are not verified because
intranet tools often use self-signed certificates. The check only reads the status code.

## Credentials

Clicking a link opens it in your browser with the browser's own login. Credentials are only for the
dashboard's checker and **Fetch title**, so intranet tools are checked as you and not as *Login required*.

Open **Credentials** (or click a *Login required* badge) and add one per host:

| Type | Sends | Typical use |
| --- | --- | --- |
| Bearer token | `Authorization: Bearer <token>` | Jira / Confluence (Collab) / Polarion personal access tokens |
| Basic | `Authorization: Basic base64(user:secret)` | Tools that accept username + password or API token |
| Custom header | `<Header-Name>: <secret>` | GitLab `PRIVATE-TOKEN` |

- Host is an exact name (`vlm.lge.com`, `10.220.48.194`) or a wildcard (`*.lge.com`). The most specific match wins.
- The credential is sent only to the matching host. It is re-applied after redirects only if the new host matches too.
- `credentials.json` is plain JSON, written atomically and restricted to your account: `icacls` on Windows,
  mode 0600 elsewhere. Anything running as your user can read it, the same as `~/.git-credentials`.
- The API never returns a saved secret. Editing a credential with an empty secret keeps the old one.
- **Test** checks one URL with the matching credential. Tools that authenticate page URLs only through
  SSO cookies (not tokens) still show *Login required*.

## Quick add from any page

Drag **+ Save to Bookmarks** (sidebar footer) to the browser's bookmarks bar. Clicking it on any page opens
the dashboard with the URL and title prefilled. If the URL is already saved, its editor opens instead.

## Keyboard

- `/` focuses search; `Esc` clears it.
- Shift-click row checkboxes to select a range.

## Development

```sh
python -m unittest discover -s tests
```

| Module | Role |
| --- | --- |
| `bookmarks/store.py` | SQLite schema and queries |
| `bookmarks/backup.py` | Weekly JSON backup scheduler |
| `bookmarks/tags.py` | Tag normalization |
| `bookmarks/importer.py` | Netscape bookmark HTML parser |
| `bookmarks/checker.py` | Link checks, title fetch, background check job |
| `bookmarks/credentials.py` | Credential storage and host matching |
| `bookmarks/server.py` | HTTP server and JSON API |
| `bookmarks/static/` | Single-page UI (vanilla JS, no build step) |

The server rejects requests whose `Host` is not `127.0.0.1:<port>`/`localhost:<port>` (DNS rebinding),
writes from a foreign `Origin`, and non-JSON write bodies (cross-site form posts). The UI is served with a
strict Content-Security-Policy, and bookmarks with non-http(s) schemes such as `javascript:` are not made clickable.
