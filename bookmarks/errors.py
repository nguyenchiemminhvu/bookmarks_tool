"""Exceptions mapped to HTTP status codes by the server."""


class NotFound(LookupError):
    """404: the referenced link, tag or credential does not exist."""


class DuplicateURL(Exception):
    """409: another link already has this URL."""
