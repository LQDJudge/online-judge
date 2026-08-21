from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Course, Language, Profile


class CourseJoinabilityTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.first()
        self.course = Course.objects.create(
            name="Joinable Course",
            slug="joinable-course",
            about="about",
            is_public=True,
            is_open=True,
        )

    def make_profile(self, username, is_superuser=False):
        user = User.objects.create_user(
            username=username,
            password="password123",
            is_superuser=is_superuser,
            is_staff=is_superuser,
        )
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        return profile

    def test_regular_user_can_join_public_open_course(self):
        profile = self.make_profile("regular")

        self.assertTrue(Course.is_joinable(self.course, profile))
        self.assertIn(self.course, Course.get_joinable_courses(profile))

    def test_superuser_can_join_public_open_course_as_student(self):
        profile = self.make_profile("admin", is_superuser=True)

        self.assertTrue(Course.is_accessible_by(self.course, profile))
        self.assertTrue(Course.is_joinable(self.course, profile))
        self.assertIn(self.course, Course.get_joinable_courses(profile))
