"""Common logging helpers for fetch workflows."""

import logging, sys
from pathlib import Path


def get_logger(name="fetch", log_fp=None, level="INFO"):
    """Build a console and file logger for one fetch workflow run."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_fp is not None:
        log_fp = Path(log_fp)
        log_fp.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_fp, mode="w")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
