"""Project rule: auth-required HTML routes redirect anonymous users to login.

Anonymous (not-logged-in) users hitting a login-required page must be redirected to
`/accounts/login/?next=...` (302), never shown a 403/404. Authenticated users who
lack permission keep their per-route status (e.g. 403). Covers the course access
mixins, CourseAdd, and NotificationList. (Quiz mixins are covered separately in
test_quiz_access_redirect.py.)
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import Course, CourseLesson, Language, Profile


class AuthRedirectTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        lang = Language.objects.first()
        user = User.objects.create_user("plainuser", "p@p.com", "pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": lang}
        )
        self.course = Course.objects.create(
            name="Auth Course",
            slug="authcourse",
            about="about",
            is_public=True,
            is_open=True,
        )
        CourseLesson.objects.create(
            course=self.course, title="L1", content="c", order=1, points=100
        )

    def _assert_login_redirect(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)
        self.assertIn("next=", resp.url)

    # --- CourseAccessibleMixin (course grades) ---
    def test_course_grades_anonymous_redirects_to_login(self):
        self._assert_login_redirect(reverse("course_grades", args=[self.course.slug]))

    def test_course_grades_authenticated_non_member_forbidden(self):
        self.client.force_login(self.profile.user)
        self.assertEqual(
            self.client.get(
                reverse("course_grades", args=[self.course.slug])
            ).status_code,
            403,
        )

    # --- CourseEditableMixin (edit lessons) ---
    def test_course_edit_lessons_anonymous_redirects_to_login(self):
        self._assert_login_redirect(
            reverse("edit_course_lessons", args=[self.course.slug])
        )

    # --- CourseAdd ---
    def test_course_add_anonymous_redirects_to_login(self):
        self._assert_login_redirect(reverse("course_add"))

    # --- NotificationList (LoginRequiredMixin) ---
    def test_notification_anonymous_redirects_to_login(self):
        self._assert_login_redirect(reverse("notification"))
