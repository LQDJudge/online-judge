#!/usr/bin/env python3
"""
One-off migration script to move files from old locations to MEDIA_ROOT.

This script handles the migration of:
1. Problem PDFs from DMOJ_PROBLEM_DATA_ROOT/{code}/ to MEDIA_ROOT/problem_pdfs/{code}/
2. Submission files from DMOJ_SUBMISSION_ROOT/ to MEDIA_ROOT/submissions/
3. Rendered PDF cache from DMOJ_PDF_PROBLEM_CACHE/ to MEDIA_ROOT/pdf_cache/

Usage:
    # From the online-judge directory with virtualenv activated:
    python judge/scripts/migrate_files_to_media.py [--dry-run]

After running this script, sync MEDIA_ROOT to S3 if using S3 storage:
    aws s3 sync /path/to/media s3://bucket-name/
"""

import os
import sys
import shutil
import argparse

# Setup Django environment
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dmoj.settings")

import django

django.setup()

from django.conf import settings
from judge.models import Problem


def ensure_dir(path):
    """Create directory if it doesn't exist."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def migrate_problem_pdfs(dry_run=False):
    """
    Migrate problem PDF descriptions from DMOJ_PROBLEM_DATA_ROOT to MEDIA_ROOT.

    Old location: DMOJ_PROBLEM_DATA_ROOT/{code}/*.pdf
    New location: MEDIA_ROOT/problem_pdfs/{code}/*.pdf
    """
    print("\n=== Migrating Problem PDFs ===")

    problem_data_root = getattr(settings, "DMOJ_PROBLEM_DATA_ROOT", None)
    if not problem_data_root:
        print("DMOJ_PROBLEM_DATA_ROOT not set, skipping problem PDFs")
        return 0

    media_pdf_root = os.path.join(settings.MEDIA_ROOT, "problem_pdfs")

    problems_with_pdf = Problem.objects.exclude(pdf_description="").exclude(
        pdf_description__isnull=True
    )
    migrated_count = 0

    for problem in problems_with_pdf:
        if not problem.pdf_description:
            continue

        # Get the filename from the current db path
        db_path = problem.pdf_description.name
        filename = os.path.basename(db_path)

        # Check if file exists at old location (DMOJ_PROBLEM_DATA_ROOT/{code}/{filename})
        old_path = os.path.join(problem_data_root, problem.code, filename)

        # Also check if old path matches {code}/{filename} pattern (legacy)
        if not os.path.exists(old_path):
            # Try using the db_path directly under problem_data_root
            alt_old_path = os.path.join(problem_data_root, db_path)
            if os.path.exists(alt_old_path):
                old_path = alt_old_path

        new_dir = os.path.join(media_pdf_root, problem.code)
        new_path = os.path.join(new_dir, filename)
        new_db_path = f"problem_pdfs/{problem.code}/{filename}"

        # Skip if already migrated (file exists at new location)
        if os.path.exists(new_path):
            continue

        if os.path.exists(old_path):
            print(f"  {problem.code}: {old_path} -> {new_path}")

            if not dry_run:
                ensure_dir(new_dir)
                shutil.copy2(old_path, new_path)

                # Update database
                problem.pdf_description.name = new_db_path
                problem.save(update_fields=["pdf_description"])

                # Remove old file after successful copy
                os.remove(old_path)

            migrated_count += 1

    print(f"Migrated {migrated_count} problem PDFs")
    return migrated_count


def migrate_submission_files(dry_run=False):
    """
    Migrate submission source files from DMOJ_SUBMISSION_ROOT to MEDIA_ROOT.

    Old location: DMOJ_SUBMISSION_ROOT/*.ext
    New location: MEDIA_ROOT/submissions/*.ext
    """
    print("\n=== Migrating Submission Files ===")

    submission_root = getattr(settings, "DMOJ_SUBMISSION_ROOT", "/tmp")
    if submission_root == "/tmp":
        print("DMOJ_SUBMISSION_ROOT is /tmp (temporary), skipping")
        return 0

    media_submissions = os.path.join(settings.MEDIA_ROOT, "submissions")

    if not os.path.exists(submission_root):
        print(f"Submission root {submission_root} does not exist, skipping")
        return 0

    migrated_count = 0

    for filename in os.listdir(submission_root):
        old_path = os.path.join(submission_root, filename)
        if os.path.isfile(old_path):
            new_path = os.path.join(media_submissions, filename)

            print(f"  {filename}")

            if not dry_run:
                ensure_dir(media_submissions)
                shutil.copy2(old_path, new_path)
                os.remove(old_path)

            migrated_count += 1

    print(f"Migrated {migrated_count} submission files")
    return migrated_count


def migrate_pdf_cache(dry_run=False):
    """
    Migrate rendered PDF cache from DMOJ_PDF_PROBLEM_CACHE to MEDIA_ROOT.

    Old location: DMOJ_PDF_PROBLEM_CACHE/*.pdf
    New location: MEDIA_ROOT/pdf_cache/*.pdf
    """
    print("\n=== Migrating PDF Cache ===")

    pdf_cache_root = getattr(settings, "DMOJ_PDF_PROBLEM_CACHE", None)
    if not pdf_cache_root:
        print("DMOJ_PDF_PROBLEM_CACHE not set, skipping")
        return 0

    media_pdf_cache = os.path.join(settings.MEDIA_ROOT, "pdf_cache")

    if not os.path.exists(pdf_cache_root):
        print(f"PDF cache {pdf_cache_root} does not exist, skipping")
        return 0

    migrated_count = 0

    for filename in os.listdir(pdf_cache_root):
        if filename.endswith(".pdf"):
            old_path = os.path.join(pdf_cache_root, filename)
            new_path = os.path.join(media_pdf_cache, filename)

            print(f"  {filename}")

            if not dry_run:
                ensure_dir(media_pdf_cache)
                shutil.copy2(old_path, new_path)
                os.remove(old_path)

            migrated_count += 1

    print(f"Migrated {migrated_count} cached PDFs")
    return migrated_count


def main():
    parser = argparse.ArgumentParser(
        description="Migrate files from old locations to MEDIA_ROOT for S3 compatibility"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually moving files",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN MODE - No files will be moved ===")

    print(f"MEDIA_ROOT: {settings.MEDIA_ROOT}")

    total = 0
    total += migrate_problem_pdfs(dry_run=args.dry_run)
    total += migrate_submission_files(dry_run=args.dry_run)
    total += migrate_pdf_cache(dry_run=args.dry_run)

    print(f"\n=== Migration Complete ===")
    print(f"Total files processed: {total}")

    if args.dry_run:
        print("\nThis was a dry run. Run without --dry-run to actually migrate files.")
    else:
        print("\nFiles have been migrated to MEDIA_ROOT.")
        print("If using S3, sync MEDIA_ROOT to your bucket:")
        print(f"  aws s3 sync {settings.MEDIA_ROOT} s3://your-bucket-name/")


if __name__ == "__main__":
    main()
