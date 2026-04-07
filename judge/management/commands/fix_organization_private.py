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
            should_be_true = list(
                model.objects.filter(
                    is_organization_private=False,
                    organizations__isnull=False,
                )
                .distinct()
                .values_list("id", flat=True)
            )
            if should_be_true:
                model.objects.filter(id__in=should_be_true).update(
                    is_organization_private=True
                )
                for obj_id in should_be_true:
                    self.stdout.write(f"  {name} {obj_id}: False -> True")

            # Set False where no organizations but flag is True
            has_orgs = set(
                model.objects.filter(organizations__isnull=False)
                .distinct()
                .values_list("id", flat=True)
            )
            should_be_false = list(
                model.objects.filter(is_organization_private=True)
                .exclude(id__in=has_orgs)
                .values_list("id", flat=True)
            )
            if should_be_false:
                model.objects.filter(id__in=should_be_false).update(
                    is_organization_private=False
                )
                for obj_id in should_be_false:
                    self.stdout.write(f"  {name} {obj_id}: True -> False")

            self.stdout.write(
                f"{name}: {len(should_be_true)} set to True, {len(should_be_false)} set to False"
            )
