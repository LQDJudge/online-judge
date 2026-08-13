from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import RequestMetric


class Command(BaseCommand):
    help = "Delete expired request metrics"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Delete request metrics older than this many days",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Delete request metrics in batches of this size",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without deleting rows",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is None:
            days = getattr(settings, "REQUEST_METRICS_RETENTION_DAYS", 7)

        if days <= 0:
            self.stdout.write(
                self.style.WARNING(
                    _("Request metric retention is disabled; nothing was deleted.")
                )
            )
            return

        batch_size = options["batch_size"]
        cutoff = timezone.now() - timedelta(days=days)
        queryset = RequestMetric.objects.filter(time__lt=cutoff)
        total_count = queryset.count()

        self.stdout.write(
            _("Found %(count)s expired request metric rows.") % {"count": total_count}
        )
        if options["dry_run"] or total_count == 0:
            return

        deleted_total = 0
        while True:
            metric_ids = list(
                queryset.order_by("id").values_list("id", flat=True)[:batch_size]
            )
            if not metric_ids:
                break

            deleted_count, _deleted_by_model = RequestMetric.objects.filter(
                id__in=metric_ids
            ).delete()
            deleted_total += deleted_count

        self.stdout.write(
            self.style.SUCCESS(
                _("Deleted %(count)s expired request metric rows.")
                % {"count": deleted_total}
            )
        )
