from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from judge.models import Profile, ProfileModerationCase
from judge.tasks.username_moderation import moderate_profile_case_task


class Command(BaseCommand):
    help = "Audit public profile fields with AI and optionally create moderation cases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--target",
            choices=[
                ProfileModerationCase.TARGET_USERNAME,
                ProfileModerationCase.TARGET_ABOUT,
            ],
            default=ProfileModerationCase.TARGET_USERNAME,
            help="Profile field to audit.",
        )
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
        target = options["target"]
        limit = options["limit"]
        recent_days = options["recent_days"]
        active_only = options["active_only"]
        inactive_only = options["inactive_only"]

        if active_only and inactive_only:
            raise CommandError("--active-only and --inactive-only cannot be combined.")

        if target == ProfileModerationCase.TARGET_ABOUT:
            candidates = self.get_about_candidates(
                limit=limit,
                recent_days=recent_days,
                active_only=active_only,
                inactive_only=inactive_only,
            )
        else:
            candidates = self.get_username_candidates(
                limit=limit,
                recent_days=recent_days,
                active_only=active_only,
                inactive_only=inactive_only,
            )

        self.stdout.write(
            "%s %d AI profile %s moderation candidate(s)"
            % ("Creating" if should_apply else "Found", len(candidates), target)
        )

        created = 0
        queued = 0
        for candidate in candidates:
            user = (
                candidate.user
                if target == ProfileModerationCase.TARGET_ABOUT
                else candidate
            )
            value_snapshot = (
                candidate.about
                if target == ProfileModerationCase.TARGET_ABOUT
                else user.username
            )
            self.stdout.write(
                "#%d %s target=%s active=%s"
                % (user.id, user.username, target, user.is_active)
            )
            if not should_apply:
                continue
            case = ProfileModerationCase.objects.create(
                user=user,
                target=target,
                username=user.username,
                normalized_username=(
                    user.username.casefold()
                    if target == ProfileModerationCase.TARGET_USERNAME
                    else ""
                ),
                value_snapshot=value_snapshot,
                source=ProfileModerationCase.SOURCE_AUDIT,
                decision=ProfileModerationCase.DECISION_PENDING,
                category=ProfileModerationCase.CATEGORY_OTHER,
                confidence=None,
                reason="Queued for AI profile %s moderation audit." % target,
                is_automated=False,
            )
            created += 1
            moderate_profile_case_task.delay(case.id, delete_safe_case=True)
            queued += 1

        if should_apply:
            self.stdout.write(
                self.style.SUCCESS(
                    "Created %d case(s); queued %d AI task(s)" % (created, queued)
                )
            )

    def get_username_candidates(self, limit, recent_days, active_only, inactive_only):
        users = User.objects.order_by("-date_joined")
        if recent_days:
            users = users.filter(
                date_joined__gte=timezone.now() - timedelta(days=recent_days)
            )
        if active_only:
            users = users.filter(is_active=True)
        if inactive_only:
            users = users.filter(is_active=False)

        users = users.annotate(
            has_current_username_case=Exists(
                ProfileModerationCase.objects.filter(
                    user_id=OuterRef("id"),
                    target=ProfileModerationCase.TARGET_USERNAME,
                    username=OuterRef("username"),
                )
            )
        ).filter(has_current_username_case=False)

        return list(users[:limit])

    def get_about_candidates(self, limit, recent_days, active_only, inactive_only):
        profiles = (
            Profile.objects.select_related("user")
            .exclude(about__isnull=True)
            .exclude(about="")
            .order_by("-user__date_joined")
        )
        if recent_days:
            profiles = profiles.filter(
                user__date_joined__gte=timezone.now() - timedelta(days=recent_days)
            )
        if active_only:
            profiles = profiles.filter(user__is_active=True)
        if inactive_only:
            profiles = profiles.filter(user__is_active=False)

        profiles = profiles.annotate(
            has_current_about_case=Exists(
                ProfileModerationCase.objects.filter(
                    user_id=OuterRef("user_id"),
                    target=ProfileModerationCase.TARGET_ABOUT,
                    value_snapshot=OuterRef("about"),
                )
            )
        ).filter(has_current_about_case=False)

        return list(profiles[:limit])
