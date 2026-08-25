"""Project logging helpers that keep diagnostic output on stderr."""

from __future__ import annotations

import logging
import sys

_LOGGER_PREFIX = "wms_config_agent"


def get_logger(name: str) -> logging.Logger:
    """Return a consistently formatted project logger."""
    logger_name = name if name.startswith(_LOGGER_PREFIX) else f"{_LOGGER_PREFIX}.{name}"
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger
