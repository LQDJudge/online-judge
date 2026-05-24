import json

from django.core.management.base import BaseCommand, CommandError

from judge.utils.problem_equivalence import (
    ProblemEquivalenceError,
    ProblemEquivalenceVerifier,
)


class Command(BaseCommand):
    help = "Verify duplicate problems by cross-submitting accepted solutions"

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Source problem code")
        parser.add_argument("--target", required=True, help="Target problem code")
        parser.add_argument(
            "--submission",
            type=int,
            help="Specific accepted source submission to copy",
        )
        parser.add_argument(
            "--both",
            action="store_true",
            help="Also submit an accepted solution from target back to source",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create the verification submission(s) and queue judging.",
        )
        parser.add_argument("--judge-id", help="Specific judge id to use")
        parser.add_argument(
            "--wait",
            type=int,
            default=0,
            help="Seconds to wait for final judging result after queueing.",
        )

    def handle(self, *args, **options):
        try:
            reports = [
                ProblemEquivalenceVerifier(
                    options["source"],
                    options["target"],
                    source_submission_id=options.get("submission"),
                    apply=options["apply"],
                    judge_id=options.get("judge_id"),
                    wait_seconds=options["wait"],
                ).run()
            ]
            if options["both"]:
                reports.append(
                    ProblemEquivalenceVerifier(
                        options["target"],
                        options["source"],
                        apply=options["apply"],
                        judge_id=options.get("judge_id"),
                        wait_seconds=options["wait"],
                    ).run()
                )
        except ProblemEquivalenceError as exc:
            raise CommandError(str(exc)) from exc

        output = reports if options["both"] else reports[0]
        self.stdout.write(json.dumps(output, indent=2, ensure_ascii=False, default=str))
