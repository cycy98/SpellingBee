from __future__ import annotations

from copy import copy

from uvicorn.logging import AccessFormatter as _Base


class TimestampedAccessFormatter(_Base):
    """Extends uvicorn's AccessFormatter to fix client_addr for unix socket proxying.

    - Empty client_addr (unix socket, no proxy headers) → "unix"
    - "1.2.3.4:0" (X-Forwarded-For extracted, port placeholder) → "1.2.3.4"
    """

    def formatMessage(self, record: object) -> str:
        if getattr(record, "args", None):  # type: ignore[union-attr]
            client_addr = record.args[0]  # type: ignore[union-attr]
            if not client_addr:
                client_addr = "unix"
            elif client_addr.endswith(":0"):
                client_addr = client_addr[:-2]
            if client_addr != record.args[0]:  # type: ignore[union-attr]
                record = copy(record)
                record.args = (client_addr, *record.args[1:])  # type: ignore[union-attr]
        return super().formatMessage(record)  # type: ignore[arg-type]
