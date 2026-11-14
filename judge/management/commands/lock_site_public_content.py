"""Explicit, opt-in lock rollout; restricted content must remain untouched."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judge.models import Contest, Problem
from judge.utils.public_visibility import site_public_contest_q, site_public_problem_q


class Command(BaseCommand):
    help = "Lock existing site-public problems/contests in batches. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        size = options["batch_size"]
        if not 1 <= size <= 5000:
            raise CommandError("Use a batch-size between 1 and 5000.")
        scopes = (
            (Problem, site_public_problem_q()),
            (Contest, site_public_contest_q()),
        )
        for model, public_scope in scopes:
            total = 0
            last_id = 0
            while True:
                with transaction.atomic():
                    rows = list(
                        model.objects.select_for_update()
                        .filter(public_scope, pk__gt=last_id, is_locked=False)
                        .order_by("pk")
                        .values_list("pk", flat=True)[:size]
                    )
                    if not rows:
                        break
                    if options["apply"]:
                        model.objects.filter(public_scope, pk__in=rows).update(
                            is_locked=True
                        )
                        if model is Problem:
                            Problem.dirty_cache(*rows)
                    total += len(rows)
                    last_id = rows[-1]
            verb = "Locked" if options["apply"] else "Would lock"
            self.stdout.write(f"{verb} {total} {model._meta.verbose_name_plural}.")
