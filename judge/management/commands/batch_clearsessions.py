from django.core.management.base import BaseCommand
from django.contrib.sessions.models import Session
from django.utils import timezone
import time


class Command(BaseCommand):
    help = "Clears expired sessions in small batches to prevent timeouts"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Number of sessions to delete in each batch",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.5,
            help="Sleep time between batches in seconds",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        sleep_time = options["sleep"]

        self.stdout.write("Finding expired sessions...")
        expired_count = Session.objects.filter(expire_date__lt=timezone.now()).count()
        self.stdout.write(f"Found {expired_count} expired sessions to delete")

        deleted_count = 0
        while True:
            # Get a batch of expired sessions
            expired_sessions = Session.objects.filter(
                expire_date__lt=timezone.now()
            ).order_by("expire_date")[:batch_size]

            # Convert to list to execute the query
            session_ids = list(expired_sessions.values_list("pk", flat=True))

            # Break if no more expired sessions
            if not session_ids:
                break

            # Delete this batch
            Session.objects.filter(pk__in=session_ids).delete()
            batch_count = len(session_ids)
            deleted_count += batch_count

            self.stdout.write(
                f"Deleted batch of {batch_count} sessions. Total: {deleted_count}/{expired_count}"
            )

            # Sleep to reduce database load
            time.sleep(sleep_time)

        self.stdout.write(
            self.style.SUCCESS(f"Successfully deleted {deleted_count} expired sessions")
        )
