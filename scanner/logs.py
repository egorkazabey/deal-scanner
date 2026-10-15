"""Logging: console (rich) + rotating files in data/logs (on the host's persistent volume).

scanner.log has everything from INFO, errors.log only warnings and errors (with tracebacks);
/logs in the bot shows the end of errors.log.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
QUIET_LOGGERS = ("httpx", "google_genai", "aiogram.event", "apscheduler")


def setup_logging(level: str, log_dir: Path | None, console: Console) -> None:
    handlers: list[logging.Handler] = [RichHandler(console=console, show_path=False)]
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            everything = RotatingFileHandler(log_dir / "scanner.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
            errors = RotatingFileHandler(log_dir / "errors.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
        except OSError as exc:  # a read-only disk must not stop the bot
            console.print(f"[yellow]log files disabled: {exc}[/]")
        else:
            errors.setLevel(logging.WARNING)
            for h in (everything, errors):
                h.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
            handlers += [everything, errors]
    logging.basicConfig(level=level, format="%(message)s", handlers=handlers, force=True)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def tail(path: Path, lines: int = 30) -> list[str]:
    """Last `lines` lines of a log file (plus its rotated predecessor when the file is short)."""
    out: list[str] = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            out += p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    return out[-lines:]
