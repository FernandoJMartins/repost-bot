"""Configuração de logs (console + arquivo rotativo + memória p/ página web) com remoção do token."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
from pathlib import Path


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets: set[str] = {s for s in (secrets or []) if s}

    def add(self, secret: str) -> None:
        if secret:
            self.secrets.add(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(s in msg for s in self.secrets):
            for s in self.secrets:
                msg = msg.replace(s, "<REDACTED>")
            record.msg, record.args = msg, ()
        return True


class MemoryLogHandler(logging.Handler):
    """Mantém as últimas linhas de log para exibir na página web."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.lines: collections.deque[dict] = collections.deque(maxlen=capacity)
        self.counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.counter += 1
            self.lines.append({
                "n": self.counter,
                "t": record.created,
                "level": record.levelname,
                "msg": self.format(record),
            })
        except Exception:
            self.handleError(record)

    def since(self, n: int) -> list[dict]:
        return [l for l in self.lines if l["n"] > n]


def setup_logging(level: str, log_file: Path | None) -> tuple[RedactFilter, MemoryLogHandler]:
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    redact = RedactFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redact)
        root.addHandler(fh)

    mem = MemoryLogHandler()
    mem.setFormatter(logging.Formatter("%(message)s"))
    mem.addFilter(redact)
    mem.setLevel(logging.INFO)
    root.addHandler(mem)

    # httpx registra a URL completa (que contém o token) em nível INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return redact, mem
