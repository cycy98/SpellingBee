"""Spelling Bee — shared exception types."""

from __future__ import annotations


class HtmxError(Exception):
    """Raised in route handlers to abort with an HTML error fragment."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
