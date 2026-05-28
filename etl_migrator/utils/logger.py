"""Centralised logging setup — console (UTF-8 safe) + rotating file."""

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR   = os.environ.get("ETL_LOG_DIR",   "./logs")
LOG_LEVEL = os.environ.get("ETL_LOG_LEVEL", "INFO").upper()

_configured: set = set()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"etl_migrator.{name}")

    if name in _configured:
        return logger
    _configured.add(name)
    logger.setLevel(LOG_LEVEL)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler — force UTF-8 on Windows (avoids CP1252 crash) ───────
    # On Python 3.7+ we can wrap sys.stdout in a UTF-8 TextIOWrapper.
    # errors="replace" means any char that still can't encode becomes '?'
    # instead of raising an exception.
    if sys.platform == "win32":
        utf8_stdout = io.TextIOWrapper(
            sys.stdout.buffer,
            encoding="utf-8",
            errors="replace",
            line_buffering=True,
        )
        ch = logging.StreamHandler(utf8_stdout)
    else:
        ch = logging.StreamHandler(sys.stdout)

    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # ── File handler (always UTF-8) ───────────────────────────────────────────
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "etl_migrator.log"),
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    return logger
