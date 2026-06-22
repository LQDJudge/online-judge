from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import Comment, Contest, Language, Problem, ProblemGroup, Profile
from judge.models.contest_review import ContestReviewRun
from judge.models.notification import Notification, NotificationCategory
from judge.models.problem_review import ProblemReviewRun
from judge.review.comment_notify import notify_review_comment


def _make_language():
    lang, _ = Language.objects.get_or_create(
        key="PY3",
        defaults={
            "name": "Python 3",
            "short_name": "PY3",
            "common_name": "Python",
            "ace": "python",
            "pygments": "python3",
            "template": "",
        },
    )
    return lang


def _profile(username, language, is_superuser=False):
    if is_superuser:
        user = User.objects.create_superuser(username, f"{username}@x.com", "pw")
    else:
        user = User.objects.create_user(username, f"{username}@x.com", "pw")
    profile, _ = Profile.objects.get_or_create(
        user=user, defaults={"language": language}
    )
    return profile


def _comment_on(target, author):
    ct = ContentType.objects.get_for_model(type(target))
    return Comment.objects.create(
        author=author,
        content_type=ct,
        object_id=target.id,
        body="some clarification",
        score=0,
        hidden=False,
    )


@override_settings(LANGUAGE_CODE="en")
class ReviewCommentNotifyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )
        cls.author = _profile("rcauthor", cls.language)
        cls.admin = _profile("rcadmin", cls.language, is_superuser=True)
        cls.outsider = _profile("rcoutsider", cls.language)

        cls.problem = Problem.objects.create(
            code="rcp1",
            name="RC Problem",
            description="x" * 50,
            group=cls.group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.author)
        cls.prun = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.author,
            input_hash="x" * 64,
            status=ProblemReviewRun.DONE,
            finished_at=timezone.now(),
        )

        now = timezone.now()
        cls.contest = Contest.objects.create(
            key="rcc1",
            name="RC Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        cls.contest.authors.add(cls.author)
        cls.crun = ContestReviewRun.objects.create(
            contest=cls.contest,
            triggered_by=cls.author,
            input_hash="y" * 64,
        )

    def _count(self, profile):
        return Notification.objects.filter(
            owner=profile, category=NotificationCategory.REVIEW_COMMENT
        ).count()

    def test_problem_review_comment_notifies_superuser_and_author(self):
        comment = _comment_on(self.prun, self.outsider)
        notify_review_comment(comment, self.prun, "/link")
        self.assertEqual(self._count(self.admin), 1)  # superuser audience
        self.assertEqual(self._count(self.author), 1)  # problem author
        self.assertEqual(self._count(self.outsider), 0)  # actor excluded

    def test_contest_review_comment_notifies_superuser_and_author(self):
        comment = _comment_on(self.crun, self.outsider)
        notify_review_comment(comment, self.crun, "/link")
        self.assertEqual(self._count(self.admin), 1)
        self.assertEqual(self._count(self.author), 1)
        self.assertEqual(self._count(self.outsider), 0)

    def test_actor_who_is_superuser_not_self_notified(self):
        # An admin commenting should notify the author but not themselves.
        comment = _comment_on(self.prun, self.admin)
        notify_review_comment(comment, self.prun, "/link")
        self.assertEqual(self._count(self.admin), 0)
        self.assertEqual(self._count(self.author), 1)

    def test_parent_comment_author_excluded(self):
        # Author posted first; admin replies. The author already gets a REPLY
        # notification, so the review-comment helper must not double-notify them.
        parent = _comment_on(self.prun, self.author)
        reply = Comment.objects.create(
            author=self.admin.user.profile,
            content_type=ContentType.objects.get_for_model(ProblemReviewRun),
            object_id=self.prun.id,
            body="reply",
            score=0,
            hidden=False,
            parent=parent,
        )
        notify_review_comment(reply, self.prun, "/link")
        self.assertEqual(self._count(self.author), 0)  # excluded (gets REPLY)
        self.assertEqual(self._count(self.admin), 0)  # excluded (is the commenter)

    def test_non_review_target_is_noop(self):
        # Commenting on a non-review object (a Problem itself) must not emit a
        # REVIEW_COMMENT notification.
        comment = _comment_on(self.problem, self.outsider)
        notify_review_comment(comment, self.problem, "/link")
        self.assertEqual(self._count(self.admin), 0)
        self.assertEqual(self._count(self.author), 0)
