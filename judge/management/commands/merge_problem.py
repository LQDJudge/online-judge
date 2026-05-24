import json

from django.core.management.base import BaseCommand, CommandError

from judge.utils.problem_merge import ProblemMerge, ProblemMergeError


class Command(BaseCommand):
    help = "Hard-merge a duplicate problem into a canonical target problem"

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Duplicate problem code")
        parser.add_argument("--target", required=True, help="Canonical problem code")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the merge. Without this, only a dry-run report is printed.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Keep target values for conflicting metadata and continue.",
        )

    def handle(self, *args, **options):
        try:
            report = ProblemMerge(
                options["source"],
                options["target"],
                apply=options["apply"],
                force=options["force"],
            ).run()
        except ProblemMergeError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
