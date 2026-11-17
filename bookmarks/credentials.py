"""Per-host credentials for the server-side link checker, kept in credentials.json.

Secrets are stored in plain JSON (like ~/.git-credentials). The file is written
atomically and restricted to the current user: icacls on Windows, mode 0600 elsewhere.
Secrets never leave this module except as HTTP headers for a matching host.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .errors import NotFound

TYPES = ("bearer", "basic", "header")
PUBLIC_FIELDS = ("id", "label", "host", "type", "username", "header")

_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST_RE = re.compile(rf"^(\*\.)?{_LABEL}(\.{_LABEL})*$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_FORBIDDEN_CHARS = ("\r", "\n", "\0")


def host_matches(pattern: str, host: str) -> int:
    """Specificity of ``pattern`` for ``host``: -1 if no match, higher is more specific."""
    if pattern.startswith("*."):
        return len(pattern) if host.endswith(pattern[1:]) else -1
    return 1000 + len(pattern) if host == pattern else -1


def _public(item: dict) -> dict:
    return {key: item.get(key, "") for key in PUBLIC_FIELDS}


@lru_cache(maxsize=1)
def _current_user_sid() -> str | None:
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r'"(S-1-[0-9-]+)"', out)
    return match.group(1) if match else None


def _restrict_to_owner(path: str) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    sid = _current_user_sid()
    result = None
    if sid:
        result = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"*{sid}:F"],
                                capture_output=True, text=True)
    if result is None or result.returncode != 0:
        print(f"warning: could not restrict permissions of {path}; it keeps the folder's default ACL",
              file=sys.stderr)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items: list[dict] = []
        if self.path.exists():
            self._items = list(json.loads(self.path.read_text(encoding="utf-8")).get("credentials", []))

    def adopt(self, other: "CredentialStore") -> None:
        """Take over ``other``'s credentials and persist them with this store's file protections."""
        with self._lock:
            self._items = list(other._items)
            self._save()

    def list(self) -> list[dict]:
        with self._lock:
            return [_public(item) for item in sorted(self._items, key=lambda i: (i["host"], i["label"]))]

    def add(self, data: dict) -> dict:
        with self._lock:
            item = self._validated(data, None)
            item["id"] = uuid.uuid4().hex[:12]
            self._items.append(item)
            self._save()
            return _public(item)

    def update(self, cred_id: str, data: dict) -> dict:
        with self._lock:
            index = self._index(cred_id)
            item = self._validated(data, self._items[index])
            item["id"] = cred_id
            self._items[index] = item
            self._save()
            return _public(item)

    def delete(self, cred_id: str) -> None:
        with self._lock:
            del self._items[self._index(cred_id)]
            self._save()

    def find(self, url: str) -> dict | None:
        """Public view of the credential that applies to ``url``, if any."""
        with self._lock:
            item = self._find(url)
            return _public(item) if item else None

    def headers_for(self, url: str) -> dict[str, str]:
        with self._lock:
            item = self._find(url)
        if item is None:
            return {}
        if item["type"] == "bearer":
            return {"Authorization": f"Bearer {item['secret']}"}
        if item["type"] == "basic":
            pair = f"{item['username']}:{item['secret']}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
        return {item["header"]: item["secret"]}

    # ---- internals (caller holds the lock) ---------------------------------

    def _find(self, url: str) -> dict | None:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return None
        best, best_rank = None, -1
        for item in self._items:
            rank = host_matches(item["host"], host)
            if rank > best_rank:
                best, best_rank = item, rank
        return best

    def _index(self, cred_id: str) -> int:
        for index, item in enumerate(self._items):
            if item["id"] == cred_id:
                return index
        raise NotFound(f"credential {cred_id} not found")

    def _validated(self, data: dict, existing: dict | None) -> dict:
        def text(key: str) -> str:
            value = data.get(key) or ""
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
            if any(ch in value for ch in _FORBIDDEN_CHARS):
                raise ValueError(f"{key} must not contain line breaks")
            return value

        host = text("host").lower()
        if "://" in host:  # accept a pasted URL
            host = (urlsplit(host).hostname or "").lower()
        if not _HOST_RE.match(host):
            raise ValueError("host must look like jira.example.com, 10.0.0.5 or *.example.com")
        kind = text("type")
        if kind not in TYPES:
            raise ValueError(f"type must be one of: {', '.join(TYPES)}")
        username = text("username") if kind == "basic" else ""
        if kind == "basic" and (not username or ":" in username):
            raise ValueError("basic auth needs a username without ':'")
        header = text("header") if kind == "header" else ""
        if kind == "header" and not _HEADER_NAME_RE.match(header):
            raise ValueError("custom header needs a valid header name, e.g. PRIVATE-TOKEN")
        secret = text("secret")
        if not secret:
            if existing is None:
                raise ValueError("token or password is required")
            secret = existing["secret"]
        if any(other["host"] == host and other is not existing for other in self._items):
            raise ValueError(f"a credential for {host} already exists")
        return {"label": text("label") or host, "host": host, "type": kind,
                "username": username, "header": header, "secret": secret}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "credentials": self._items}, fh, indent=2)
            _restrict_to_owner(tmp)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
