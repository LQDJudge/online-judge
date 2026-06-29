"""My-rank button + ?focus jump on course grades / lesson grades pages."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Contest,
    Course,
    CourseContest,
    CourseLesson,
    CourseRole,
    Language,
    Profile,
)
from judge.models.course import RoleInCourse
from judge.views.course import _locate_student


class FakeStudent:
    def __init__(self, username, id):
        self.username = username
        self.id = id


class LocateStudentUnitTest(TestCase):
    def test_locate_computes_page_and_id(self):
        students = [FakeStudent(f"u{i}", 100 + i) for i in range(5)]
        with mock.patch("judge.views.course.GRADES_PAGE_SIZE", 2):
            # u0,u1 -> page1 ; u2,u3 -> page2 ; u4 -> page3
            self.assertEqual(_locate_student(students, "u3"), (2, 103))
            self.assertEqual(_locate_student(students, "u0"), (1, 100))
            self.assertEqual(_locate_student(students, "u4"), (3, 104))

    def test_locate_missing_or_none(self):
        students = [FakeStudent("a", 1)]
        self.assertEqual(_locate_student(students, "nobody"), (None, None))
        self.assertEqual(_locate_student(students, None), (None, None))


@mock.patch("judge.views.course.GRADES_PAGE_SIZE", 2)
class GradesMyRankIntegrationTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        # all 0% -> sorted by username; page size 2 -> sc lands on page 2
        self.sa = self._student("sa")
        self.sb = self._student("sb")
        self.sc = self._student("sc")
        self.teacher = self._profile("teach")
        self.course = Course.objects.create(
            name="C", slug="c", about="a", is_public=True, is_open=True
        )
        for p in (self.sa, self.sb, self.sc):
            CourseRole.objects.create(
                course=self.course, user=p, role=RoleInCourse.STUDENT
            )
        CourseRole.objects.create(
            course=self.course, user=self.teacher, role=RoleInCourse.TEACHER
        )
        self.lesson = CourseLesson.objects.create(
            course=self.course, title="L1", content="c", order=1, points=100
        )
        now = timezone.now()
        self.contest = Contest.objects.create(
            key="gradecontest",
            name="Grade Contest",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_visible=True,
        )
        self.course_contest = CourseContest.objects.create(
            course=self.course, contest=self.contest, order=1, points=100
        )

    def _profile(self, name):
        u = User.objects.create_user(name, f"{name}@x.com", "pw")
        p, _ = Profile.objects.get_or_create(user=u, defaults={"language": self.lang})
        return p

    _student = _profile

    def _grades_url(self):
        return reverse("course_grades", args=[self.course.slug])

    def test_my_rank_button_for_ranked_student_on_page2(self):
        self.client.force_login(self.sc.user)  # sc is on page 2
        resp = self.client.get(self._grades_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="my-rank-btn"')
        self.assertContains(resp, 'data-page="2"')

    def test_no_button_for_non_student_viewer(self):
        self.client.force_login(self.teacher.user)
        resp = self.client.get(self._grades_url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="my-rank-btn"')

    def test_focus_lands_on_students_page_and_highlights(self):
        self.client.force_login(self.sa.user)
        resp = self.client.get(self._grades_url() + "?focus=sc")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="highlight"')
        self.assertContains(resp, "/user/sc")  # sc row present (page 2)
        self.assertNotContains(resp, "/user/sa")  # page-1 students absent

    def test_focus_nonexistent_is_graceful(self):
        self.client.force_login(self.sa.user)
        resp = self.client.get(self._grades_url() + "?focus=ghost")
        self.assertEqual(resp.status_code, 200)

    def test_lesson_cell_links_to_lesson_with_focus(self):
        self.client.force_login(self.sa.user)
        resp = self.client.get(self._grades_url())
        expected = reverse(
            "course_grades_lesson", args=[self.course.slug, self.lesson.id]
        )
        self.assertContains(resp, expected + "?focus=sa")

    def test_contest_cell_links_to_ranking_with_user_focus(self):
        self.client.force_login(self.sa.user)
        resp = self.client.get(self._grades_url())
        expected = reverse("contest_ranking", args=[self.contest.key])
        self.assertContains(resp, expected + "?user=sa")

    def _lesson_url(self):
        return reverse("course_grades_lesson", args=[self.course.slug, self.lesson.id])

    def test_lesson_my_rank_button_for_ranked_student(self):
        self.client.force_login(self.sc.user)  # sc on page 2
        resp = self.client.get(self._lesson_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="my-rank-btn"')
        self.assertContains(resp, 'data-page="2"')

    def test_lesson_focus_lands_and_highlights(self):
        self.client.force_login(self.sa.user)
        resp = self.client.get(self._lesson_url() + "?focus=sc")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="highlight"')
        self.assertContains(resp, "/user/sc")
        self.assertNotContains(resp, "/user/sa")

    # --- the viewer's own row carries the scroll-target id (JS relies on it) ---
    def test_own_row_has_my_grades_row_id(self):
        self.client.force_login(self.sa.user)  # sa is on page 1 with their own row
        resp = self.client.get(self._grades_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="my-grades-row"')

    # --- button data-page reflects the viewer's actual page (page 1 case) ---
    def test_my_rank_button_data_page_1_for_first_page_student(self):
        self.client.force_login(self.sa.user)  # sa sorts first -> page 1
        resp = self.client.get(self._grades_url())
        self.assertContains(resp, 'data-page="1"')

    # --- ?focus=<self> (the My-rank reload path) marks the viewer's own row ---
    def test_focus_self_marks_own_row_highlight_and_id(self):
        self.client.force_login(self.sc.user)  # sc on page 2
        resp = self.client.get(self._grades_url() + "?focus=sc")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="highlight"')
        self.assertContains(resp, 'id="my-grades-row"')
