"""
Django management command to publish all problems in an organization.
Tags them, saves points/types, and makes them public.
Usage: python manage.py publish_group_problems <org_slug> [--dry-run]
"""

import sys
import os
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judge.models import Problem
from judge.models.profile import Organization


class Command(BaseCommand):
    help = "Tag and publish all organization-private problems (save points/types, make public)"

    def add_arguments(self, parser):
        parser.add_argument(
            "org_slug",
            type=str,
            help="The organization slug (e.g., springcamp2024)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )

    def handle(self, *args, **options):
        org_slug = options["org_slug"]

        # Find the organization
        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            orgs = Organization.objects.filter(name__icontains=org_slug)
            if orgs.count() == 1:
                org = orgs.first()
            elif orgs.count() > 1:
                self.stdout.write(
                    self.style.ERROR(f"Multiple organizations match '{org_slug}':")
                )
                for o in orgs:
                    self.stdout.write(f"  - {o.slug}: {o.name}")
                raise CommandError("Please specify the exact organization slug")
            else:
                raise CommandError(f"Organization '{org_slug}' not found")

        self.stdout.write(f"Found organization: {org.slug} ({org.name})")

        # Get all problems that are private to this organization
        problems = Problem.objects.filter(
            organizations=org, is_organization_private=True
        ).order_by("code")

        if not problems.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"No organization-private problems found for '{org.slug}'"
                )
            )
            return

        self.stdout.write(f"Found {problems.count()} organization-private problems")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN - would process:"))
            for problem in problems:
                self.stdout.write(f"  {problem.code}: {problem.name}")
            return

        # Process each problem: tag, save, publish
        problems_list = list(problems)
        self.process_problems(problems_list)

    def process_problems(self, problems):
        """Tag, save, and publish each problem."""
        self.stdout.write("\nInitializing tagging service...")

        try:
            sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), "../../..", "..")
            )
            from problem_tag import get_problem_tag_service

            tag_service = get_problem_tag_service()
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to initialize tagging service: {e}")
            )
            return

        total = len(problems)
        published_count = 0
        failed_count = 0

        for i, problem in enumerate(problems, 1):
            self.stdout.write(f"\n[{i}/{total}] {problem.code}: {problem.name}")

            try:
                # Tag the problem
                result = tag_service.tag_single_problem(problem)

                if not result["success"]:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  Tagging failed: {result.get('error', 'Unknown')}"
                        )
                    )
                    failed_count += 1
                    continue

                is_valid = result.get("is_valid", False)
                if not is_valid:
                    self.stdout.write(
                        self.style.WARNING(f"  Invalid tagging result - skipping")
                    )
                    failed_count += 1
                    continue

                points = result.get("predicted_points")
                types = result.get("predicted_types", [])
                self.stdout.write(f"  Tagged: points={points}, types={types}")

                # Save and publish in one transaction
                with transaction.atomic():
                    # Save points
                    if points:
                        problem.points = float(points)

                    # Save types
                    tag_service.update_problem_with_tags(
                        problem, result, update_points=False, update_types=True
                    )

                    # Make public
                    had_orgs = problem.organizations.exists()
                    problem.is_organization_private = False
                    problem.is_public = True
                    problem.save(
                        update_fields=["points", "is_public", "is_organization_private"]
                    )

                    if had_orgs:
                        problem.organizations.clear()

                published_count += 1
                self.stdout.write(self.style.SUCCESS(f"  Published!"))

                # Sleep between requests
                if i < total:
                    time.sleep(tag_service.config.sleep_time)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Exception: {e}"))
                failed_count += 1

        # Summary
        self.stdout.write(f"\n{'='*50}")
        self.stdout.write(self.style.SUCCESS(f"Published: {published_count}/{total}"))
        if failed_count:
            self.stdout.write(self.style.WARNING(f"Failed: {failed_count}/{total}"))
