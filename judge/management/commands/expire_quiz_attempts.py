import json
import logging
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from judge.models import QuizAttempt
from judge.utils.quiz_attempts import finalize_locked_attempt

logger = logging.getLogger(__name__)
CURSOR_KEY = "quiz-expiry:cursor:v2"


class Command(BaseCommand):
    help = "Finalize a bounded batch of overdue, deadline-snapshotted quiz attempts."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--max-seconds", type=int, default=20)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        batch_size = max(1, min(options["batch_size"], 1000))
        now = timezone.now()
        due = QuizAttempt.objects.filter(
            is_submitted=False, deadline_initialized=True, deadline_at__lte=now
        )
        oldest = (
            due.order_by("deadline_at").values_list("deadline_at", flat=True).first()
        )
        result = {
            "overdue": due.count(),
            "oldest_overdue_seconds": (now - oldest).total_seconds() if oldest else 0,
            "finalized": 0,
            "failed": 0,
            "skipped": 0,
            "dry_run": options["dry_run"],
        }
        # A rotating operational cursor prevents repeatedly failing low IDs from
        # starving later attempts. Correctness depends on row locks, not this cache.
        cursor = None if options["dry_run"] else cache.get(CURSOR_KEY)
        candidates = due
        if cursor:
            candidates = candidates.filter(
                Q(deadline_at__gt=cursor[0])
                | Q(deadline_at=cursor[0], pk__gt=cursor[1])
            )
        rows = list(
            candidates.order_by("deadline_at", "pk").values_list("pk", "deadline_at")[
                :batch_size
            ]
        )
        if not rows and cursor:
            rows = list(
                due.order_by("deadline_at", "pk").values_list("pk", "deadline_at")[
                    :batch_size
                ]
            )
        if options["dry_run"]:
            result["candidate_ids"] = [row[0] for row in rows]
        else:
            started = time.monotonic()
            last_cursor = None
            for attempt_id, deadline in rows:
                if time.monotonic() - started >= max(1, options["max_seconds"]):
                    break
                try:
                    # One short transaction per candidate deliberately isolates
                    # grading failures and concurrent submissions from the batch.
                    with transaction.atomic():
                        attempt = (
                            QuizAttempt.objects.select_for_update(skip_locked=True)
                            .select_related("quiz", "contest_participation__contest")
                            .filter(pk=attempt_id)
                            .first()
                        )
                        captured_now = timezone.now()
                        if (
                            attempt is None
                            or attempt.is_submitted
                            or not attempt.deadline_initialized
                            or attempt.deadline_at is None
                            or captured_now < attempt.deadline_at
                        ):
                            result["skipped"] += 1
                        else:
                            finalize_locked_attempt(attempt, now=captured_now)
                            result["finalized"] += 1
                except Exception:
                    result["failed"] += 1
                    logger.exception("Failed to expire quiz attempt %s", attempt_id)
                last_cursor = (deadline, attempt_id)
            if last_cursor:
                cache.set(CURSOR_KEY, last_cursor, timeout=3600)
        self.stdout.write(json.dumps(result))
