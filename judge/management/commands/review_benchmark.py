"""Read-only offline benchmark export/scoring. Never triggers paid reviews."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _

from judge.models import ContestReviewRun, ProblemReviewRun
from judge.review.benchmark import export_observation, score_benchmark


class Command(BaseCommand):
    help = _(
        "Export captured review observations or score adjudicated benchmark results (no API calls)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--suite",
            default=str(
                Path(__file__).resolve().parents[2] / "review" / "benchmark_cases.json"
            ),
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--results", help=_("JSON list of captured, adjudicated observations")
        )
        mode.add_argument("--export", action="append", metavar="CASE=problem:RUN_ID")
        parser.add_argument(
            "--require-pass",
            action="store_true",
            help=_("Exit unsuccessfully for missing, unassessed or failed cases"),
        )

    def handle(self, *args, **options):
        try:
            suite = json.loads(Path(options["suite"]).read_text())
            if options["export"]:
                known = {case["id"] for case in suite["cases"]}
                bindings = []
                for binding in options["export"]:
                    case, reference = binding.split("=", 1)
                    kind, run_id = reference.split(":", 1)
                    if case not in known or kind not in ("problem", "contest"):
                        raise ValueError("Unknown case or run kind")
                    bindings.append((case, kind, int(run_id)))
                if len({case for case, _, _ in bindings}) != len(bindings):
                    raise ValueError("Repeated benchmark case")
                problems = {
                    run.pk: run
                    for run in ProblemReviewRun.objects.filter(
                        pk__in=[pk for _, kind, pk in bindings if kind == "problem"],
                        workflow_version=2,
                    ).prefetch_related("coordinator_stages")
                }
                contests = {
                    run.pk: run
                    for run in ContestReviewRun.objects.filter(
                        pk__in=[pk for _, kind, pk in bindings if kind == "contest"],
                        workflow_version=2,
                    ).prefetch_related("coordinator_stages")
                }
                results = []
                for case, kind, pk in bindings:
                    run = (problems if kind == "problem" else contests).get(pk)
                    if run is None:
                        raise ValueError("Review run not found")
                    results.append(export_observation(case, run))
                self.stdout.write(json.dumps(results, ensure_ascii=False, indent=2))
                return
            results = json.loads(Path(options["results"]).read_text())
            report = score_benchmark(suite, results)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            ProblemReviewRun.DoesNotExist,
            ContestReviewRun.DoesNotExist,
        ) as exc:
            raise CommandError(
                _("Invalid benchmark input: %(error)s") % {"error": exc}
            ) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if options["require_pass"] and not report["passed"]:
            raise CommandError(
                _("Benchmark is incomplete or did not pass; see the report above.")
            )
