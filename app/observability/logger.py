import logging
import logging.handlers
import os
import structlog


def mask_phone(phone: str) -> str:
    if len(phone) < 9:
        return "****"
    return phone[:5] + "****" + phone[-4:]


def setup_logging():
    os.makedirs("logs", exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # App log (all events, 10MB, 7 backups)
    app_handler = logging.handlers.RotatingFileHandler(
        "logs/app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
    )
    app_handler.setLevel(logging.INFO)

    # Error log (errors only, 5MB, 30 backups)
    error_handler = logging.handlers.RotatingFileHandler(
        "logs/errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=30,
    )
    error_handler.setLevel(logging.ERROR)

    # Watchdog log
    watchdog_handler = logging.handlers.RotatingFileHandler(
        "logs/watchdog.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
    )
    watchdog_handler.setLevel(logging.INFO)

    for handler in [console_handler, app_handler, error_handler]:
        root_logger.addHandler(handler)

    watchdog_logger = logging.getLogger("watchdog")
    watchdog_logger.addHandler(watchdog_handler)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str):
    return structlog.get_logger(name)
