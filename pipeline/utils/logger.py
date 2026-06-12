import sys
from pathlib import Path

from loguru import logger


def setup_logger(log_level: str = "INFO", service: str | None = None) -> None:
    logger.remove()  # remove default handler

    # Per-service log file. The pipeline's services run as DIFFERENT users — radar-poller
    # and persist-worker as root, premarket-scheduler and premarket-api as premarket — but
    # historically every entrypoint wrote the SAME logs/premarket_<date>.log. At the
    # midnight rotation whichever user's service created the new file owned it 0644, and
    # the other user's services then crash-looped with PermissionError on open-for-append
    # (this took down premarket-scheduler, so morning_briefing never ran). Give each
    # entrypoint its own file (default: derived from the running script name) so root-owned
    # and premarket-owned logs can never collide on the same path.
    if not service:
        service = Path(sys.argv[0]).stem or "premarket"

    # Console — human readable in dev
    logger.add(
        sys.stdout,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )

    # File — JSON structured for prod parsing. One file per service per day.
    logger.add(
        f"logs/{service}_{{time:YYYY-MM-DD}}.log",
        level=log_level,
        format="{time} | {level} | {name}:{line} | {message}",
        rotation="00:00",       # new file at midnight
        retention="30 days",
        compression="gz",
        serialize=True,         # JSON output
    )
