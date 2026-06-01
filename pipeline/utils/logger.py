import sys
from loguru import logger


def setup_logger(log_level: str = "INFO") -> None:
    logger.remove()  # remove default handler

    # Console — human readable in dev
    logger.add(
        sys.stdout,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )

    # File — JSON structured for prod parsing
    logger.add(
        "logs/premarket_{time:YYYY-MM-DD}.log",
        level=log_level,
        format="{time} | {level} | {name}:{line} | {message}",
        rotation="00:00",       # new file at midnight
        retention="30 days",
        compression="gz",
        serialize=True,         # JSON output
    )
