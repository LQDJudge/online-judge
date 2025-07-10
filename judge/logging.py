import logging

error_log = logging.getLogger("judge.errors")
debug_log = logging.getLogger("judge.debug")


def log_exception(msg):
    error_log.exception(msg)


def log_debug(category, data):
    debug_log.info(f"{category}: {data}")
