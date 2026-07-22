from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

from judge.models import UsernameModerationCase
from judge.tasks.username_moderation import moderate_username_task


class Command(BaseCommand):
    help = "Audit existing usernames with AI and optionally create moderation cases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Create moderation cases and queue AI checks. Safe allow results "
                "from this audit are deleted after classification."
            ),
        )
        parser.add_argument(
            "--active-only",
            action="store_true",
            help="Only audit active users.",
        )
        parser.add_argument(
            "--inactive-only",
            action="store_true",
            help="Only audit disabled users.",
        )
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--recent-days", type=int, default=0)

    def handle(self, *args, **options):
        should_apply = options["apply"]
        limit = options["limit"]
        recent_days = options["recent_days"]
        active_only = options["active_only"]
        inactive_only = options["inactive_only"]

        if active_only and inactive_only:
            raise CommandError("--active-only and --inactive-only cannot be combined.")

        users = User.objects.order_by("-date_joined")
        if recent_days:
            users = users.filter(
                date_joined__gte=timezone.now() - timedelta(days=recent_days)
            )
        if active_only:
            users = users.filter(is_active=True)
        if inactive_only:
            users = users.filter(is_active=False)

        users = users.exclude(username_moderation_cases__username=F("username"))

        candidates = []
        for user in users.iterator():
            candidates.append(user)
            if len(candidates) >= limit:
                break

        self.stdout.write(
            "%s %d AI username moderation candidate(s)"
            % ("Creating" if should_apply else "Found", len(candidates))
        )

        created = 0
        queued = 0
        for user in candidates:
            self.stdout.write(
                "#%d %s active=%s" % (user.id, user.username, user.is_active)
            )
            if not should_apply:
                continue
            case = UsernameModerationCase.objects.create(
                user=user,
                username=user.username,
                normalized_username=user.username.casefold(),
                source=UsernameModerationCase.SOURCE_AUDIT,
                decision=UsernameModerationCase.DECISION_PENDING,
                category=UsernameModerationCase.CATEGORY_OTHER,
                confidence=None,
                reason="Queued for AI username moderation audit.",
                is_automated=False,
            )
            created += 1
            moderate_username_task.delay(case.id, delete_safe_case=True)
            queued += 1

        if should_apply:
            self.stdout.write(
                self.style.SUCCESS(
                    "Created %d case(s); queued %d AI task(s)" % (created, queued)
                )
            )
