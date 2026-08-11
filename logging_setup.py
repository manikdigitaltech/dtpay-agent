"""
Shared agent.log setup for analyze.py and chat.py.

RotatingFileHandler instead of a plain FileHandler - a service that
runs indefinitely with an unrotated log file will eventually fill the
disk. Caps agent.log at MAX_BYTES per file, keeps BACKUP_COUNT old
copies (agent.log.1, agent.log.2, ...) and discards anything older -
60 MB total ceiling at the defaults below, tune via the constants if
that's not the right size for your traffic.
"""
import logging
from logging.handlers import RotatingFileHandler

MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT = 5  # + the active file = 6 files, 60 MB ceiling total


def get_agent_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RotatingFileHandler("agent.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger