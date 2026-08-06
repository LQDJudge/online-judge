import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from judge.judgeapi import bridge_status
from judge.models import Judge, Language, Problem, Profile, Submission, SubmissionSource

AC_SOURCE = "import sys\nprint(sum(map(int, sys.stdin.read().split())))\n"
WA_SOURCE = "print(-1)\n"
SLOW_WA_SOURCE = "import time\ntime.sleep(0.2)\nprint(-1)\n"


class Command(BaseCommand):
    help = "Create disposable submissions to stress the bridge and judge dispatch path."

    def add_arguments(self, parser):
        parser.add_argument(
            "--problem",
            default="auto",
            help="Problem code to submit to, or 'auto' to pick one advertised by an online judge.",
        )
        parser.add_argument("--language", default="PY3")
        parser.add_argument("--user", default="admin")
        parser.add_argument("--count", type=int, default=200)
        parser.add_argument("--concurrency", type=int, default=20)
        parser.add_argument("--timeout", type=int, default=600)
        parser.add_argument("--poll-interval", type=float, default=1)
        parser.add_argument("--submit-delay-ms", type=int, default=0)
        parser.add_argument(
            "--expect-judges",
            type=int,
            default=0,
            help="Fail unless bridge_status reports at least this many connected judges before submitting.",
        )
        parser.add_argument(
            "--expect-min-active",
            type=int,
            default=0,
            help="Fail unless bridge_status reports at least this many active submissions during the run.",
        )
        parser.add_argument(
            "--source-mode",
            choices=("ac", "wa", "slow-wa", "mixed"),
            default="wa",
        )
        parser.add_argument(
            "--user-tier",
            action="store_true",
            help="Use normal user-tier priority instead of admin rejudge priority.",
        )
        parser.add_argument(
            "--judge-id",
            help="Target a specific online judge by name.",
        )
        parser.add_argument(
            "--keep-submissions",
            action="store_true",
            help="Keep created submissions instead of deleting them after the run.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow running when DEBUG is false.",
        )
        parser.add_argument(
            "--skip-compat-check",
            action="store_true",
            help="Skip online judge problem/language compatibility preflight.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to run outside DEBUG without --force.")
        if options["count"] <= 0:
            raise CommandError("--count must be positive.")
        if options["concurrency"] <= 0:
            raise CommandError("--concurrency must be positive.")
        if options["expect_judges"] < 0:
            raise CommandError("--expect-judges cannot be negative.")
        if options["expect_min_active"] < 0:
            raise CommandError("--expect-min-active cannot be negative.")

        try:
            language = Language.objects.get(key=options["language"])
        except Language.DoesNotExist:
            raise CommandError("Language %s does not exist." % options["language"])
        problem = self._get_problem(options["problem"], language)
        try:
            profile = Profile.objects.select_related("user").get(
                user__username=options["user"]
            )
        except Profile.DoesNotExist:
            raise CommandError("User %s does not exist." % options["user"])

        if not options["skip_compat_check"]:
            compatible_judges = problem.judges.filter(
                online=True,
                runtimes=language,
            )
            if options["judge_id"]:
                compatible_judges = compatible_judges.filter(name=options["judge_id"])
            compatible = compatible_judges.exists()
            if not compatible:
                if (
                    options["judge_id"]
                    and not Judge.objects.filter(name=options["judge_id"]).exists()
                ):
                    raise CommandError("Judge %s does not exist." % options["judge_id"])
                target = (
                    " judge %s" % options["judge_id"] if options["judge_id"] else ""
                )
                raise CommandError(
                    "No online%s currently advertises %s with %s. "
                    "Choose a supported problem/language or use --skip-compat-check."
                    % (target, problem.code, language.key)
                )

        before_status = self._print_bridge_status("before")
        if options["expect_judges"]:
            if (
                not before_status
                or before_status.get("judges", 0) < options["expect_judges"]
            ):
                raise CommandError(
                    "Expected at least %d connected judges before stress run."
                    % options["expect_judges"]
                )
        self.stdout.write("stress target: %s / %s" % (problem.code, language.key))

        ids = []
        started = time.monotonic()
        admin_priority = not options["user_tier"]

        def submit_one(index):
            close_old_connections()
            source = self._source_for(index, options["source_mode"])
            submission = Submission.objects.create(
                user_id=profile.id,
                problem_id=problem.id,
                language_id=language.id,
                status="QU",
            )
            SubmissionSource.objects.create(submission=submission, source=source)
            submission.judge(
                rejudge=admin_priority,
                batch_rejudge=admin_priority,
                judge_id=options["judge_id"],
            )
            return submission.id

        with ThreadPoolExecutor(max_workers=options["concurrency"]) as executor:
            futures = []
            for index in range(options["count"]):
                futures.append(executor.submit(submit_one, index))
                if options["submit_delay_ms"]:
                    time.sleep(options["submit_delay_ms"] / 1000)
            for future in as_completed(futures):
                ids.append(future.result())

        submit_elapsed = time.monotonic() - started
        self.stdout.write(
            "submitted %d submissions in %.2fs" % (len(ids), submit_elapsed)
        )

        deadline = time.monotonic() + options["timeout"]
        max_queued = 0
        max_active = 0
        terminal_ids = set()
        statuses = Counter()

        while time.monotonic() < deadline:
            statuses = self._status_counts(ids)
            terminal_ids = set(
                Submission.objects.filter(id__in=ids)
                .exclude(status__in=Submission.IN_PROGRESS_GRADING_STATUS)
                .values_list("id", flat=True)
            )
            try:
                status = bridge_status()
            except Exception:
                status = None
            if status and status.get("name") == "bridge-status":
                max_queued = max(max_queued, status["queued-submissions"])
                max_active = max(max_active, status["active-submissions"])
            if len(terminal_ids) == len(ids):
                break
            time.sleep(options["poll_interval"])

        elapsed = time.monotonic() - started
        timed_out = len(terminal_ids) != len(ids)
        result_counts = self._result_counts(ids)

        self.stdout.write("elapsed: %.2fs" % elapsed)
        self.stdout.write("status counts: %s" % dict(sorted(statuses.items())))
        self.stdout.write(
            "result counts: %s"
            % dict(sorted(result_counts.items(), key=lambda item: str(item[0])))
        )
        self.stdout.write("max bridge queue: %d" % max_queued)
        self.stdout.write("max bridge active: %d" % max_active)
        self._print_bridge_status("after")

        if timed_out and not options["keep_submissions"]:
            self.stdout.write(
                "kept %d stress submissions because the run timed out" % len(ids)
            )
        elif not options["keep_submissions"]:
            Submission.objects.filter(id__in=ids).delete()
            self.stdout.write("deleted %d stress submissions" % len(ids))

        if timed_out:
            raise CommandError(
                "%d submissions did not finish before timeout"
                % (len(ids) - len(terminal_ids))
            )
        if options["expect_min_active"] and max_active < options["expect_min_active"]:
            raise CommandError(
                "Expected at least %d active bridge submissions; saw %d."
                % (options["expect_min_active"], max_active)
            )

    def _source_for(self, index, mode):
        if mode == "ac":
            return AC_SOURCE
        if mode == "wa":
            return WA_SOURCE
        if mode == "slow-wa":
            return SLOW_WA_SOURCE
        return AC_SOURCE if index % 2 == 0 else WA_SOURCE

    def _get_problem(self, problem_code, language):
        if problem_code != "auto":
            try:
                return Problem.objects.get(code=problem_code)
            except Problem.DoesNotExist:
                raise CommandError("Problem %s does not exist." % problem_code)

        problem = (
            Problem.objects.filter(judges__online=True, judges__runtimes=language)
            .distinct()
            .order_by("code")
            .first()
        )
        if problem is None:
            raise CommandError(
                "No online judge currently advertises any problem with %s."
                % language.key
            )
        return problem

    def _status_counts(self, ids):
        return Counter(
            Submission.objects.filter(id__in=ids).values_list("status", flat=True)
        )

    def _result_counts(self, ids):
        return Counter(
            Submission.objects.filter(id__in=ids).values_list("result", flat=True)
        )

    def _print_bridge_status(self, label):
        try:
            status = bridge_status()
        except Exception as e:
            self.stdout.write("bridge %s: unreachable (%s)" % (label, e))
            return None
        if status.get("name") != "bridge-status":
            self.stdout.write("bridge %s: malformed %r" % (label, status))
            return None
        self.stdout.write(
            "bridge %s: judges=%d queued=%d active=%d validations=%d/%d"
            % (
                label,
                status["judges"],
                status["queued-submissions"],
                status["active-submissions"],
                status["queued-validations"],
                status["active-validations"],
            )
        )
        return status
