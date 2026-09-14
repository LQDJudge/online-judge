from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from judge.models import Organization, Profile


class Command(BaseCommand):
    help = "Read-only audit of official-school membership and configuration."

    def handle(self, *args, **options):
        duplicates = list(
            Profile.objects.annotate(
                school_count=Count(
                    "organizations",
                    filter=Q(organizations__official_school__isnull=False),
                    distinct=True,
                ),
            )
            .filter(school_count__gt=1)
            .values_list("pk", flat=True)
        )
        invalid = list(
            Organization.objects.filter(official_school__isnull=False)
            .filter(
                Q(is_open=True)
                | Q(is_community=True)
                | Q(admins__isnull=True)
                | Q(moderators__isnull=False),
            )
            .values_list("pk", flat=True)
            .distinct()
        )
        self.stdout.write("Duplicate affiliations: %s" % duplicates)
        self.stdout.write("Invalid schools: %s" % invalid)
        if duplicates or invalid:
            raise CommandError("School invariants failed; no data was changed.")
        self.stdout.write(
            self.style.SUCCESS("School invariants hold; no data was changed.")
        )
