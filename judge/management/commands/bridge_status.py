import json

from django.core.management.base import BaseCommand, CommandError

from judge.judgeapi import bridge_status


class Command(BaseCommand):
    help = "Report read-only status from the running bridge daemon."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print the raw bridge status response as JSON.",
        )
        parser.add_argument(
            "--detail",
            action="store_true",
            help="Include in-memory bridge queue, active work, and per-judge state.",
        )
        parser.add_argument(
            "--include-problems",
            action="store_true",
            help="Include every advertised problem code for each judge in detailed status.",
        )

    def handle(self, *args, **options):
        try:
            status = bridge_status(
                detail=options["detail"],
                include_problems=options["include_problems"],
            )
        except Exception as e:
            raise CommandError("bridge: unreachable (%s)" % e)

        if status.get("name") != "bridge-status":
            raise CommandError("bridge: malformed response %r" % status)

        if options["json"]:
            self.stdout.write(json.dumps(status, sort_keys=True))
            return

        self.stdout.write("bridge: ok")
        self.stdout.write("judges: %d connected" % status["judges"])
        self.stdout.write(
            "submissions: %d queued, %d active"
            % (status["queued-submissions"], status["active-submissions"])
        )
        self.stdout.write(
            "validations: %d queued, %d active"
            % (status["queued-validations"], status["active-validations"])
        )
        if options["detail"]:
            self._print_detail(status, include_problems=options["include_problems"])

    def _print_detail(self, status, include_problems=False):
        self.stdout.write("running users: %s" % status.get("running-users", []))
        self.stdout.write(
            "maps: node=%d submissions=%d validations=%d"
            % (
                status.get("node-map-size", 0),
                status.get("submission-map-size", 0),
                status.get("validate-map-size", 0),
            )
        )

        self.stdout.write("judges:")
        for judge in status.get("judges-detail", []):
            self.stdout.write(
                "  %(name)s working=%(working)s load=%(load)s latency=%(latency)s "
                "current_submission=%(current-submission)s "
                "current_validation=%(current-validation)s "
                "problems=%(problem-count)s executors=%(executor-count)s" % judge
            )
            self.stdout.write(
                "    address=%s client=%s"
                % (judge.get("address"), judge.get("client-address"))
            )
            self.stdout.write("    executors=%s" % judge.get("executors", []))
            if include_problems:
                self.stdout.write("    problems=%s" % judge.get("problems", []))

        self.stdout.write("active submissions:")
        for submission in status.get("active-submissions-detail", []):
            self.stdout.write(
                "  %(submission-id)s judge=%(judge)s problem=%(problem)s "
                "language=%(language)s user=%(user-id)s user_tier=%(user-tier)s "
                "source_bytes=%(source-bytes)s" % submission
            )

        self.stdout.write("active validations:")
        for validation in status.get("active-validations-detail", []):
            self.stdout.write(
                "  %(validate-id)s judge=%(judge)s problem=%(problem-id)s" % validation
            )

        self.stdout.write("queue:")
        for item in status.get("queue", []):
            if item["type"] == "submission":
                self.stdout.write(
                    "  p%(priority)s submission %(submission-id)s "
                    "problem=%(problem)s language=%(language)s judge_id=%(judge-id)s "
                    "user=%(user-id)s user_tier=%(user-tier)s "
                    "source_bytes=%(source-bytes)s" % item
                )
            else:
                self.stdout.write(
                    "  p%(priority)s validation %(validate-id)s problem=%(problem-id)s"
                    % item
                )
