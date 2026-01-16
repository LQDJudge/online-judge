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
from django.db.models import Q

from judge.models import Problem, Contest
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

        # Get problems from contests belonging to this organization
        # (same logic as OrganizationProblems view)
        contest_problems = (
            Contest.objects.filter(organizations=org)
            .values_list("contest_problems__problem__id")
            .distinct()
        )

        # Get all private problems:
        # 1. Problems directly assigned to this organization (is_organization_private=True)
        # 2. Problems in contests of this organization (may have is_public=False)
        problems = Problem.objects.filter(
            Q(organizations=org, is_organization_private=True)
            | Q(id__in=contest_problems, is_public=False)
        ).order_by("code")

        if not problems.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"No private problems found for '{org.slug}' (checked org problems and contest problems)"
                )
            )
            return

        self.stdout.write(f"Found {problems.count()} private problems to publish")

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
        failed_problems = []  # Track failed problems with reasons

        for i, problem in enumerate(problems, 1):
            self.stdout.write(f"\n[{i}/{total}] {problem.code}: {problem.name}")

            try:
                # Tag the problem
                result = tag_service.tag_single_problem(problem)

                if not result["success"]:
                    reason = f"Tagging failed: {result.get('error', 'Unknown')}"
                    self.stdout.write(self.style.ERROR(f"  {reason}"))
                    failed_problems.append((problem, reason))
                    continue

                is_valid = result.get("is_valid", False)
                if not is_valid:
                    reason = "Invalid tagging result"
                    self.stdout.write(self.style.WARNING(f"  {reason} - skipping"))
                    failed_problems.append((problem, reason))
                    continue

                points = result.get("predicted_points")
                types = result.get("predicted_types", [])
                self.stdout.write(f"  Tagged: points={points}, types={types}")

                # Save and publish in one transaction
                with transaction.atomic():
                    # Set all fields first (is_public=True before save to avoid points cap)
                    if points:
                        problem.points = float(points)
                    problem.is_organization_private = False
                    problem.is_public = True

                    # Save main fields first (before clearing orgs to avoid signal issues)
                    problem.save(
                        update_fields=["points", "is_public", "is_organization_private"]
                    )

                    # Clear organizations AFTER save (signal will set is_organization_private=False again, which is fine)
                    problem.organizations.clear()

                    # Update types
                    if types:
                        from judge.models import ProblemType

                        type_objects = ProblemType.objects.filter(name__in=types)
                        for type_obj in type_objects:
                            problem.types.add(type_obj)

                published_count += 1
                self.stdout.write(self.style.SUCCESS(f"  Published!"))

                # Sleep between requests
                if i < total:
                    time.sleep(tag_service.config.sleep_time)

            except Exception as e:
                reason = f"Exception: {e}"
                self.stdout.write(self.style.ERROR(f"  {reason}"))
                failed_problems.append((problem, reason))

        # Summary
        self.stdout.write(f"\n{'='*50}")
        self.stdout.write(self.style.SUCCESS(f"Published: {published_count}/{total}"))
        if failed_problems:
            self.stdout.write(
                self.style.WARNING(f"Failed: {len(failed_problems)}/{total}")
            )
            self.stdout.write(self.style.WARNING("\nFailed problems:"))
            for problem, reason in failed_problems:
                self.stdout.write(f"  - {problem.code}: {reason}")
