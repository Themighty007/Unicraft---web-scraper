"""Structured logging setup with rotating file output, Rich console, and live job log streaming."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.logging import RichHandler

console = Console()


class JobLogBuffer(logging.Handler):
    """In-memory ring buffer capturing log records for a running job / web dashboard."""

    def __init__(
        self,
        maxlen: int = 500,
        on_record: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=maxlen)
        self.on_record = on_record

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name.split(".")[-1],
            "message": record.getMessage(),
        }
        self.records.append(entry)
        if self.on_record is not None:
            try:
                self.on_record(entry)
            except Exception:
                pass


def configure_logging(
    level: str = "INFO",
    log_dir: str | Path = "logs",
    enable_console: bool = True,
) -> logging.Logger:
    """Configure application logging cleanly without duplicating handlers."""
    logger = logging.getLogger("unicraft")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path / "scraper.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    logger.addHandler(file_handler)

    if enable_console:
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(rich_handler)

    return logger


def get_logger(name: str = "unicraft") -> logging.Logger:
    """Return a child logger under the `unicraft` namespace."""
    base = logging.getLogger("unicraft")
    if not base.handlers:
        configure_logging()
    return logging.getLogger(name if name.startswith("unicraft") else f"unicraft.{name}")
