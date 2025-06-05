"""
Django management command for batch problem tagging using LLM via fastapi-poe
Usage: python manage.py tag_problems [options]
"""

import sys
import os
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# Add llm_service to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", ".."))

from problem_tag import get_problem_tag_service
from judge.models import Problem


class Command(BaseCommand):
    help = "Tag problems using LLM to predict difficulty and types with image support"

    def add_arguments(self, parser):
        parser.add_argument(
            "--codes", type=str, help="Comma-separated list of problem codes to analyze"
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum number of problems to analyze (default: 10)",
        )
        parser.add_argument(
            "--update-db",
            action="store_true",
            help="Update database with analysis results",
        )
        parser.add_argument(
            "--update-points",
            action="store_true",
            help="Update problem points (difficulty) in database",
        )
        parser.add_argument(
            "--update-types",
            action="store_true",
            help="Update problem types in database",
        )
        parser.add_argument(
            "--output-file", type=str, help="Save results to specified file"
        )
        parser.add_argument(
            "--all-problems",
            action="store_true",
            help="Analyze all problems (overrides --limit)",
        )
        parser.add_argument(
            "--public-only",
            action="store_true",
            help="Only analyze public problems (visible to users)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be analyzed without making changes",
        )

    def handle(self, *args, **options):
        try:
            # Initialize problem tag service
            tag_service = get_problem_tag_service()

            # Get problems to tag
            problems = self.get_problems_to_tag(options)

            if not problems:
                self.stdout.write(self.style.WARNING("No problems found to tag"))
                return

            self.stdout.write(
                self.style.SUCCESS(f"Found {len(problems)} problems to tag")
            )

            if options["dry_run"]:
                self.stdout.write("DRY RUN - would tag:")
                for problem in problems:
                    self.stdout.write(f"  - {problem.code}: {problem.name}")
                return

            # Run tagging (synchronous)
            results = self.tag_problems(tag_service, problems)

            # Process results
            self.process_results(results, options)

        except Exception as e:
            raise CommandError(f"Analysis failed: {e}")

    def get_problems_to_tag(self, options):
        """Get list of problems to tag based on options"""
        if options["codes"]:
            # Analyze specific problem codes
            codes = [code.strip() for code in options["codes"].split(",")]
            queryset = Problem.objects.filter(code__in=codes)

            # Apply public-only filter if specified
            if options["public_only"]:
                queryset = queryset.filter(is_public=True)

            problems = queryset
            missing_codes = set(codes) - set(problems.values_list("code", flat=True))
            if missing_codes:
                self.stdout.write(
                    self.style.WARNING(f"Problem codes not found: {missing_codes}")
                )
        elif options["all_problems"]:
            # Analyze all problems
            queryset = Problem.objects.all()

            # Apply public-only filter if specified
            if options["public_only"]:
                queryset = queryset.filter(is_public=True)

            problems = queryset
        else:
            # Analyze first N problems
            queryset = Problem.objects.all()

            # Apply public-only filter if specified
            if options["public_only"]:
                queryset = queryset.filter(is_public=True)

            problems = queryset[: options["limit"]]

        return list(problems)

    def tag_problems(self, tag_service, problems):
        """Run LLM tagging on problems (synchronous)"""
        results = []
        total = len(problems)

        for i, problem in enumerate(problems, 1):
            self.stdout.write(f"Tagging {i}/{total}: {problem.code}")

            try:
                # Synchronous tagging call
                result = tag_service.tag_single_problem(problem)
                results.append(result)

                # Show progress
                if result["success"]:
                    is_valid = result.get("is_valid", False)
                    points = result.get("predicted_points", "N/A")
                    types = result.get("predicted_types", [])

                    if is_valid:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  ✓ Valid - Points: {points}, Types: {types}"
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(f"  ⚠ Invalid format - skipping updates")
                        )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f'  → Failed: {result.get("error", "Unknown error")}'
                        )
                    )

                # Sleep between requests to respect rate limits
                if i < total:  # Don't sleep after the last request
                    sleep_time = tag_service.config.sleep_time
                    self.stdout.write(f"  → Sleeping {sleep_time}s...")
                    time.sleep(sleep_time)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  → Exception: {e}"))
                results.append(
                    {
                        "problem_code": problem.code,
                        "success": False,
                        "is_valid": False,
                        "error": str(e),
                    }
                )

        return results

    def process_results(self, results, options):
        """Process and save analysis results"""
        successful_results = [r for r in results if r["success"]]
        failed_results = [r for r in results if not r["success"]]

        self.stdout.write(self.style.SUCCESS(f"\nAnalysis completed:"))
        self.stdout.write(f"  Successful: {len(successful_results)}")
        self.stdout.write(f"  Failed: {len(failed_results)}")

        # Save to file if requested
        if options["output_file"]:
            self.save_results_to_file(results, options["output_file"])

        # Update database if requested (only for valid problems)
        if options["update_db"] and successful_results:
            self.update_database(successful_results, options)

        # Show failed results
        if failed_results:
            self.stdout.write(self.style.ERROR("\nFailed analyses:"))
            for result in failed_results:
                self.stdout.write(
                    f'  {result["problem_code"]}: {result.get("error", "Unknown error")}'
                )

    def save_results_to_file(self, results, filename):
        """Save results to a file"""
        try:
            with open(filename, "w", encoding="utf-8") as f:
                for result in results:
                    if result["success"]:
                        code = result["problem_code"]
                        points = result.get("predicted_points")
                        types = result.get("predicted_types", [])
                        f.write(f"('{code}', {points}, {types})\n")
                    else:
                        code = result["problem_code"]
                        f.write(f"('{code}', None, ['Error'])\n")

            self.stdout.write(self.style.SUCCESS(f"Results saved to {filename}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to save results: {e}"))

    def update_database(self, successful_results, options):
        """Update database with tagging results (only for valid problems)"""
        tag_service = get_problem_tag_service()
        updated_count = 0
        skipped_invalid = 0

        update_points = options.get("update_points", False)
        update_types = options.get("update_types", False)

        # If neither is specified, update both
        if not update_points and not update_types:
            update_points = update_types = True

        self.stdout.write("\nUpdating database (only valid problems)...")

        for result in successful_results:
            try:
                problem = Problem.objects.get(code=result["problem_code"])

                with transaction.atomic():
                    success = tag_service.update_problem_with_tags(
                        problem, result, update_points, update_types
                    )
                    if success:
                        updated_count += 1
                        self.stdout.write(f"  ✓ Updated: {problem.code}")
                    elif result.get("is_valid"):
                        self.stdout.write(f"  - No changes: {problem.code}")
                    else:
                        skipped_invalid += 1
                        self.stdout.write(f"  ⚠ Skipped (invalid): {problem.code}")

            except Problem.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f'  Problem not found: {result["problem_code"]}')
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'  Error updating {result["problem_code"]}: {e}')
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Database updated: {updated_count} problems, {skipped_invalid} skipped (invalid format)"
            )
        )
