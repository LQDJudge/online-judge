"""
Django management command to make all problems in an organization public and optionally auto-tag them.
Usage: python manage.py publish_group_problems <org_slug> [options]
"""

import sys
import os
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judge.models import Problem
from judge.models.profile import Organization


class Command(BaseCommand):
    help = "Make all organization-private problems public and optionally auto-tag them"

    def add_arguments(self, parser):
        parser.add_argument(
            "org_slug",
            type=str,
            help="The organization slug (e.g., springcamp2024)",
        )
        parser.add_argument(
            "--auto-tag",
            action="store_true",
            help="Also run auto-tagging on the problems",
        )
        parser.add_argument(
            "--update-db",
            action="store_true",
            help="Update database with tagging results (only applies with --auto-tag)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--skip-publish",
            action="store_true",
            help="Skip making problems public (only run auto-tag)",
        )

    def handle(self, *args, **options):
        org_slug = options["org_slug"]

        # Find the organization
        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            # Try finding by name as fallback
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
            # Show all problems linked to this org (even if already public)
            all_org_problems = Problem.objects.filter(organizations=org).order_by(
                "code"
            )
            if all_org_problems.exists():
                self.stdout.write(
                    f"\nAll problems linked to this organization ({all_org_problems.count()}):"
                )
                for p in all_org_problems[:20]:
                    status = "private" if p.is_organization_private else "public"
                    self.stdout.write(f"  {p.code}: {p.name} [{status}]")
                if all_org_problems.count() > 20:
                    self.stdout.write(f"  ... and {all_org_problems.count() - 20} more")
            return

        self.stdout.write(f"Found {problems.count()} organization-private problems")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN - would process:"))
            for problem in problems:
                self.stdout.write(f"  {problem.code}: {problem.name}")
            return

        problems_list = list(problems)

        # If auto-tag is enabled, tag first and only publish valid ones
        if options["auto_tag"]:
            valid_problems = self.auto_tag_problems(problems_list, options)

            # Only publish problems with valid tagging results
            if not options["skip_publish"]:
                if valid_problems:
                    self.make_problems_public(valid_problems)
                else:
                    self.stdout.write(
                        self.style.WARNING("No valid problems to publish")
                    )
        else:
            # No tagging - just publish all
            if not options["skip_publish"]:
                self.make_problems_public(problems_list)

    def make_problems_public(self, problems):
        """Make all problems public (is_organization_private=False)"""
        self.stdout.write("\nMaking problems public...")

        updated_count = 0
        with transaction.atomic():
            for problem in problems:
                if problem.is_organization_private:
                    problem.is_organization_private = False
                    problem.is_public = True
                    problem.save(update_fields=["is_public", "is_organization_private"])
                    updated_count += 1
                    self.stdout.write(f"  Updated: {problem.code}")

        self.stdout.write(self.style.SUCCESS(f"Made {updated_count} problems public"))

    def auto_tag_problems(self, problems, options):
        """Run auto-tagging on problems. Returns list of valid problems."""
        self.stdout.write("\nRunning auto-tagging...")

        try:
            # Add llm_service to Python path
            sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), "../../..", "..")
            )
            from problem_tag import get_problem_tag_service

            tag_service = get_problem_tag_service()
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to initialize tagging service: {e}")
            )
            return []

        total = len(problems)
        results = []
        valid_problems = []

        for i, problem in enumerate(problems, 1):
            self.stdout.write(f"Tagging {i}/{total}: {problem.code}")

            try:
                result = tag_service.tag_single_problem(problem)
                result["problem"] = problem  # Store problem reference
                results.append(result)

                if result["success"]:
                    is_valid = result.get("is_valid", False)
                    points = result.get("predicted_points", "N/A")
                    types = result.get("predicted_types", [])

                    if is_valid:
                        valid_problems.append(problem)
                        self.stdout.write(
                            self.style.SUCCESS(f"  Points: {points}, Types: {types}")
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(f"  Invalid format - will not publish")
                        )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f'  Failed: {result.get("error", "Unknown")} - will not publish'
                        )
                    )

                # Update database if requested
                if (
                    options["update_db"]
                    and result["success"]
                    and result.get("is_valid")
                ):
                    with transaction.atomic():
                        success = tag_service.update_problem_with_tags(
                            problem, result, update_points=True, update_types=True
                        )
                        if success:
                            self.stdout.write(f"  Database updated")

                # Sleep between requests
                if i < total:
                    sleep_time = tag_service.config.sleep_time
                    time.sleep(sleep_time)

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  Exception: {e} - will not publish")
                )
                results.append(
                    {
                        "problem_code": problem.code,
                        "problem": problem,
                        "success": False,
                        "error": str(e),
                    }
                )

        # Summary
        self.stdout.write(
            self.style.SUCCESS(
                f"\nTagging complete: {len(valid_problems)}/{total} valid"
            )
        )

        return valid_problems
