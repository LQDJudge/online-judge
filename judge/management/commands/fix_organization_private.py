from django.core.management.base import BaseCommand

from judge.models import BlogPost, Contest, Problem


class Command(BaseCommand):
    help = "Sync is_organization_private with organizations.exists() for Problem, BlogPost, and Contest"

    def handle(self, *args, **options):
        models = [
            ("Problem", Problem),
            ("BlogPost", BlogPost),
            ("Contest", Contest),
        ]

        for name, model in models:
            # Set True where organizations exist but flag is False
            should_be_true = (
                model.objects.filter(
                    is_organization_private=False,
                    organizations__isnull=False,
                )
                .distinct()
                .values_list("id", flat=True)
            )
            count_true = model.objects.filter(id__in=should_be_true).update(
                is_organization_private=True
            )

            # Set False where no organizations but flag is True
            has_orgs = (
                model.objects.filter(organizations__isnull=False)
                .distinct()
                .values_list("id", flat=True)
            )
            count_false = (
                model.objects.filter(is_organization_private=True)
                .exclude(id__in=has_orgs)
                .update(is_organization_private=False)
            )

            self.stdout.write(
                f"{name}: {count_true} set to True, {count_false} set to False"
            )
