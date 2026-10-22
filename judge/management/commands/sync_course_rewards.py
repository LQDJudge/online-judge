from django.core.management.base import BaseCommand, CommandError

from judge.models import AvatarFrame, Course, CourseRole, Profile
from judge.utils.rewards import DEFAULT_COURSE_FRAMES, sync_course_rewards_batch


class Command(BaseCommand):
    help = "Award course avatar frames to existing enrolled users. Safe to rerun."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Only check this user")
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument(
            "--link-courses",
            action="store_true",
            help="Link previously unmapped default frames to matching course slugs",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size <= 0:
            raise CommandError("--batch-size must be positive")
        if options["link_courses"]:
            courses = dict(
                Course.objects.filter(
                    slug__in=DEFAULT_COURSE_FRAMES.values()
                ).values_list("slug", "pk")
            )
            for frame in AvatarFrame.objects.filter(
                slug__in=DEFAULT_COURSE_FRAMES,
                unlock_type="course",
                course__isnull=True,
            ):
                frame.course_id = courses.get(DEFAULT_COURSE_FRAMES[frame.slug])
                if frame.course_id:
                    frame.save(update_fields=["course"])
        profiles = Profile.objects.filter(
            pk__in=CourseRole.objects.filter(
                course__avatar_frames__is_active=True,
                course__avatar_frames__unlock_type="course",
            ).values("user_id")
        ).order_by("pk")
        if options["username"]:
            profiles = profiles.filter(user__username=options["username"])
        checked = 0
        last_pk = 0
        while True:
            batch = list(profiles.filter(pk__gt=last_pk)[:batch_size])
            if not batch:
                break
            sync_course_rewards_batch(batch)
            checked += len(batch)
            last_pk = batch[-1].pk
        self.stdout.write(
            self.style.SUCCESS(f"Checked course rewards for {checked} profiles.")
        )
