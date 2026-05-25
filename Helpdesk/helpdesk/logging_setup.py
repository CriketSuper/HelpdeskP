import logging
import sys
from pathlib import Path

from loguru import logger as base_logger


def _patch_record(record):
    record["extra"].setdefault("logger_name", record["name"])
    record["extra"].setdefault("logger_func", record["function"])
    record["extra"].setdefault("logger_line", record["line"])


logger = base_logger.patch(_patch_record)


def _get_record_logger_name(record):
    return str(record["extra"].get("logger_name", record["name"]))


def _is_security_record(record):
    logger_name = _get_record_logger_name(record)
    return logger_name == "security" or logger_name.startswith("security.")


def _is_error_record(record):
    return record["level"].no >= logging.ERROR


def _is_general_record(record):
    return not _is_security_record(record)


class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.bind(
            logger_name=record.name,
            logger_func=record.funcName,
            logger_line=record.lineno,
        ).opt(exception=record.exc_info).log(level, record.getMessage())


def configure_loguru(log_dir, *, debug=False, level="INFO", rotation="5 MB", retention=3):
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    helpdesk_log_file = log_dir_path / "helpdesk.log"
    errors_log_file = log_dir_path / "errors.log"
    security_log_file = log_dir_path / "security.log"

    logger.remove()

    console_level = "DEBUG" if debug else level
    logger.add(
        sys.stderr,
        level=console_level,
        colorize=False,
        enqueue=True,
        backtrace=debug,
        diagnose=debug,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{extra[logger_name]}</cyan>:<cyan>{extra[logger_func]}</cyan>:<cyan>{extra[logger_line]}</cyan> - <level>{message}</level>",
    )
    logger.add(
        str(helpdesk_log_file),
        level=level,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=debug,
        diagnose=debug,
        filter=_is_general_record,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[logger_name]}:{extra[logger_func]}:{extra[logger_line]} - {message}",
    )
    logger.add(
        str(errors_log_file),
        level="ERROR",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=debug,
        diagnose=debug,
        filter=_is_error_record,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[logger_name]}:{extra[logger_func]}:{extra[logger_line]} - {message}",
    )
    logger.add(
        str(security_log_file),
        level="INFO",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=debug,
        diagnose=debug,
        filter=_is_security_record,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[logger_name]}:{extra[logger_func]}:{extra[logger_line]} - {message}",
    )
