"""Structured logging configuration using structlog."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"


def configure_logging(run_id: str, stage: str, log_level: str = "INFO") -> None:
    """Configure structlog for a pipeline run. Logs to both stdout and a file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = LOGS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    log_file = run_dir / f"{stage}.log"

    # File handler
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        handlers=[file_handler, console_handler],
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "pulse"):
    return structlog.get_logger(name)
