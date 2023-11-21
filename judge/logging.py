import logging

error_log = logging.getLogger("judge.errors")


def log_exception(msg):
    error_log.exception(msg)
