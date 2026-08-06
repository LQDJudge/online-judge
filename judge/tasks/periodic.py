import logging
import uuid

from django.core.cache import cache
from django.core.management import call_command

logger = logging.getLogger(__name__)


def run_locked_command(lock_key, command_name, *command_args, lock_timeout=3600):
    token = uuid.uuid4().hex
    if not cache.add(lock_key, token, lock_timeout):
        logger.info("Skipped %s because %s is already held", command_name, lock_key)
        return {"skipped": True, "reason": "locked"}

    try:
        call_command(command_name, *command_args)
    finally:
        if cache.get(lock_key) == token:
            cache.delete(lock_key)

    return {"success": True}
