import json
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Course,
    Contest,
    ContestProblem,
    ContestParticipation,
    CourseContest,
    CourseLesson,
    CourseLessonPrerequisite,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Organization,
    Profile,
    Quiz,
    QuizAttempt,
    BestQuizAttempt,
)
from judge.models.course import get_course_role_profile_ids
from judge.utils.course_prerequisites import (
    get_lesson_lock_status,
    reorder_course_items,
)
from judge.views.course import CourseMemberForm


class CourseWorkflowTests(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.client.cookies["django_language"] = "en"
        language = Language.objects.first()
        self.profiles = {}
        for name in ["teacher", "ta", "student", "peer", "outsider", "admin"]:
            user = User.objects.create(
                username="workflow_" + name, is_superuser=name == "admin"
            )
            self.profiles[name], _ = Profile.objects.get_or_create(
                user=user, defaults={"language": language}
            )
        self.course = Course.objects.create(
            name="Workflow",
            slug="workflow",
            about="Description",
            is_public=True,
            is_open=True,
        )
        for name, role in [
            ("teacher", "TE"),
            ("ta", "AS"),
            ("student", "ST"),
            ("peer", "ST"),
        ]:
            CourseRole.objects.create(
                course=self.course, user=self.profiles[name], role=role
            )
        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="First",
            content="Lesson body",
            order=1,
            points=100,
        )
        self.locked = CourseLesson.objects.create(
            course=self.course,
            title="Locked",
            content="Locked body",
            order=2,
            points=100,
        )
        self.draft = CourseLesson.objects.create(
            course=self.course,
            title="Draft",
            content="Draft body",
            order=3,
            points=100,
            is_visible=False,
        )
        self.edge = CourseLessonPrerequisite.objects.create(
            course=self.course, source_order=1, target_order=2, required_percentage=95
        )
        self.quiz = Quiz.objects.create(code="workflowquiz", title="Workflow Quiz")
        self.link = CourseLessonQuiz.objects.create(
            lesson=self.lesson, quiz=self.quiz, points=100, is_result_hidden=True
        )
        self.locked_link = CourseLessonQuiz.objects.create(
            lesson=self.locked, quiz=self.quiz, points=100
        )
        self.draft_link = CourseLessonQuiz.objects.create(
            lesson=self.draft, quiz=self.quiz, points=100
        )
        self.attempt = QuizAttempt.objects.create(
            user=self.profiles["student"],
            quiz=self.quiz,
            lesson_quiz=self.link,
            is_submitted=True,
            score=9,
            max_score=10,
        )
        BestQuizAttempt.objects.create(
            user=self.profiles["student"],
            lesson_quiz=self.link,
            attempt=self.attempt,
            score=9,
            max_score=10,
        )

    def login(self, role):
        self.client.force_login(self.profiles[role].user)

    def url(self, name, *args):
        return reverse(name, args=[self.course.slug, *args])

    def test_denied_posts_do_not_run_handlers(self):
        self.login("student")
        CourseRole.objects.filter(course=self.course).update(
            needs_progress_recalculation=False
        )
        response = self.client.post(self.url("course_refresh_progress"))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            CourseRole.objects.filter(
                course=self.course, needs_progress_recalculation=True
            ).exists()
        )
        response = self.client.post(
            self.url("course_members"),
            {"usernames": self.profiles["outsider"].username, "role": "TE"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            CourseRole.objects.filter(
                course=self.course, user=self.profiles["outsider"]
            ).exists()
        )
        response = self.client.post(
            self.url("edit_course_lessons"),
            {
                "action": "reorder_lessons",
                "order_data": json.dumps(
                    [self.draft.pk, self.locked.pk, self.lesson.pk]
                ),
            },
        )
        self.assertEqual(response.status_code, 404)
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.order, 1)

    def test_drafts_and_locks_apply_to_grades_and_quizzes(self):
        self.login("student")
        for lesson, status in [(self.locked, 403), (self.draft, 404)]:
            for name, args in [
                ("course_lesson_detail", [lesson.pk]),
                ("course_grades_lesson", [lesson.pk]),
                ("lesson_quiz_detail", [lesson.pk, self.quiz.code]),
            ]:
                with self.subTest(name=name, lesson=lesson.pk):
                    self.assertEqual(
                        self.client.get(self.url(name, *args)).status_code, status
                    )
        self.login("ta")
        self.assertEqual(
            self.client.get(
                self.url("course_lesson_detail", self.draft.pk)
            ).status_code,
            200,
        )

    def test_anonymous_html_redirect_and_popup_error(self):
        self.assertEqual(
            self.client.get(
                self.url("course_lesson_detail", self.lesson.pk)
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.get(
                self.url(
                    "course_lesson_user_quiz_attempts_ajax",
                    self.lesson.pk,
                    self.profiles["student"].username,
                    self.link.pk,
                )
            ).status_code,
            401,
        )

    def test_hidden_scores_absent_from_all_course_totals(self):
        self.login("student")
        home = self.client.get(self.url("course_detail"))
        self.assertEqual(home.status_code, 200)
        total = home.context_data["total_progress"]
        self.assertTrue(total["is_result_hidden"])
        self.assertEqual(total["achieved_points"], 0)
        lesson = self.client.get(self.url("course_lesson_detail", self.lesson.pk))
        self.assertIsNone(lesson.context_data["lesson_quizzes"][0]["best_score"])
        grades = self.client.get(self.url("course_grades_lesson", self.lesson.pk))
        row = next(iter(grades.context_data["grades"].values()))
        self.assertTrue(row["total"]["is_result_hidden"])
        self.assertEqual(grades.context_data["global_rank"], {})
        popup = self.client.get(
            self.url(
                "course_lesson_user_quiz_attempts_ajax",
                self.lesson.pk,
                self.profiles["student"].username,
                self.link.pk,
            )
        )
        self.assertContains(popup, "Results hidden")
        self.assertNotContains(popup, 'class="case-')
        # Internal prerequisite calculation still knows the real score.
        self.assertTrue(
            get_lesson_lock_status(self.profiles["student"], self.course)[
                self.locked.pk
            ]
        )

    def test_own_grades_scope_rows_and_popups(self):
        self.course.show_classmates_grades = False
        self.course.save()
        self.login("student")
        response = self.client.get(
            self.url("course_grades"), {"focus": self.profiles["peer"].username}
        )
        self.assertEqual(
            [p.pk for p in response.context_data["grade_total"]],
            [self.profiles["student"].pk],
        )
        self.assertEqual(response.context_data["global_rank"], {})
        popup = self.url(
            "course_lesson_user_quiz_attempts_ajax",
            self.lesson.pk,
            self.profiles["peer"].username,
            self.link.pk,
        )
        self.assertEqual(self.client.get(popup).status_code, 403)
        self.login("ta")
        response = self.client.get(self.url("course_grades"))
        self.assertEqual(len(response.context_data["grade_total"]), 2)

    def test_ta_can_review_attempt_without_global_quiz_edit(self):
        self.login("ta")
        self.assertFalse(self.quiz.is_editable_by(self.profiles["ta"].user))
        self.assertEqual(
            self.client.get(
                self.url(
                    "lesson_quiz_result",
                    self.lesson.pk,
                    self.quiz.code,
                    self.attempt.pk,
                )
            ).status_code,
            200,
        )

    def test_existing_attempt_remains_recoverable_after_unpublish(self):
        attempt = QuizAttempt.objects.create(
            user=self.profiles["student"], quiz=self.quiz, lesson_quiz=self.link
        )
        self.lesson.is_visible = False
        self.lesson.save()
        self.login("student")
        self.assertEqual(
            self.client.get(
                self.url("lesson_quiz_detail", self.lesson.pk, self.quiz.code)
            ).status_code,
            404,
        )
        response = self.client.get(
            self.url("lesson_quiz_take", self.lesson.pk, self.quiz.code, attempt.pk)
        )
        self.assertEqual(response.status_code, 200)
        self.login("peer")
        self.assertEqual(
            self.client.get(
                self.url("lesson_quiz_take", self.lesson.pk, self.quiz.code, attempt.pk)
            ).status_code,
            404,
        )

    def test_ta_bulk_add_preview_confirm_and_duplicate_skip(self):
        self.login("ta")
        payload = {
            "usernames": self.profiles["outsider"].username
            + ", "
            + self.profiles["student"].username,
            "role": "AS",
            "action": "preview",
        }
        response = self.client.post(self.url("course_members"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CourseRole.objects.filter(
                course=self.course, user=self.profiles["outsider"]
            ).exists()
        )
        payload.update(
            action="confirm", confirmation=response.context_data["batch_confirmation"]
        )
        response = self.client.post(self.url("course_members"), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CourseRole.objects.get(
                course=self.course, user=self.profiles["outsider"]
            ).role,
            "AS",
        )
        self.assertEqual(
            CourseRole.objects.get(
                course=self.course, user=self.profiles["student"]
            ).role,
            "ST",
        )
        self.assertIn(
            self.profiles["outsider"].pk,
            get_course_role_profile_ids(self.course.pk, "AS"),
        )

    def test_stale_bulk_preview_cannot_apply(self):
        self.login("ta")
        payload = {"usernames": self.profiles["outsider"].username, "role": "ST"}
        response = self.client.post(self.url("course_members"), payload)
        payload.update(
            action="confirm",
            confirmation=response.context_data["batch_confirmation"],
            role="TE",
        )
        response = self.client.post(self.url("course_members"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context_data["form"].errors)
        self.assertFalse(
            CourseRole.objects.filter(
                course=self.course, user=self.profiles["outsider"]
            ).exists()
        )

    def test_bulk_limit_counts_unique_accounts_across_both_inputs(self):
        users = User.objects.bulk_create(
            [User(username="unique_batch_%d" % n) for n in range(199)]
        )
        language = Language.objects.first()
        Profile.objects.bulk_create(
            [Profile(user=user, language=language) for user in users]
        )
        selected = self.profiles["outsider"]
        data = {
            "users": [selected.pk],
            "role": "ST",
            "usernames": ",".join(
                [selected.username.upper()] + [user.username for user in users]
            ),
        }
        form = CourseMemberForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.cleaned_data["profiles"]), 200)
        data["usernames"] += ",another_account"
        form = CourseMemberForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn("Add at most 200", str(form.errors))

    def test_student_preview_encodes_unicode_username_in_navigation(self):
        user = User.objects.create(username="preview_đặng")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": Language.objects.first()}
        )
        CourseRole.objects.create(course=self.course, user=profile, role="ST")
        self.login("teacher")
        response = self.client.get(
            self.url("course_detail"), {"preview": user.username}
        )
        suffix = response.context_data["preview_suffix"]
        self.assertEqual(suffix, "?preview=preview_%C4%91%E1%BA%B7ng")
        response = self.client.get(
            self.url("course_lesson_detail", self.lesson.pk) + suffix
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["preview_profile"].pk, profile.pk)
        for page in (
            self.url("course_grades"),
            self.url("course_grades_lesson", self.lesson.pk),
        ):
            response = self.client.get(page)
            self.assertContains(
                response, 'href="%s%s"' % (self.course.get_absolute_url(), suffix)
            )

    def test_grade_tables_offer_student_view_only_to_teaching_staff(self):
        pages = [
            self.url("course_grades"),
            self.url("course_grades_lesson", self.lesson.pk),
        ]
        for role in ("teacher", "ta", "admin", "student"):
            self.login(role)
            for page in pages:
                with self.subTest(role=role, page=page):
                    response = self.client.get(page)
                    self.assertEqual(response.status_code, 200)
                    if role == "student":
                        self.assertNotContains(
                            response, 'class="course-view-as-student"'
                        )
                    else:
                        self.assertContains(
                            response, 'class="course-view-as-student"', count=2
                        )
                        for name in ("student", "peer"):
                            self.assertContains(
                                response,
                                'aria-label="View as student: %s"'
                                % self.profiles[name].username,
                            )
                            self.assertContains(
                                response,
                                'href="%s?preview=%s"'
                                % (
                                    self.course.get_absolute_url(),
                                    self.profiles[name].username,
                                ),
                            )
        self.login("teacher")
        # Draft and locked lesson gradebooks must not link to a student-inaccessible lesson.
        for lesson in (self.draft, self.locked):
            response = self.client.get(self.url("course_grades_lesson", lesson.pk))
            self.assertContains(response, 'class="course-view-as-student"', count=2)
            self.assertContains(
                response,
                'href="%s?preview=%s"'
                % (
                    self.course.get_absolute_url(),
                    self.profiles["student"].username,
                ),
            )
        response = self.client.get(self.url("course_members"))
        self.assertContains(response, "View as student")

    def test_make_role_saves_and_invalidates_both_roles(self):
        user = self.profiles["peer"]
        self.assertIn(user.pk, get_course_role_profile_ids(self.course.pk, "ST"))
        get_course_role_profile_ids(self.course.pk, "AS")
        CourseRole.make_role(self.course, user, "AS")
        self.assertNotIn(user.pk, get_course_role_profile_ids(self.course.pk, "ST"))
        self.assertIn(user.pk, get_course_role_profile_ids(self.course.pk, "AS"))

    def test_reorder_keeps_prerequisite_positions_and_recalculates_access(self):
        self.login("teacher")
        original = list(self.course.lessons.order_by("pk").values_list("pk", "order"))
        response = self.client.post(
            self.url("edit_course_lessons"),
            {"action": "reorder_lessons", "order_data": json.dumps([self.lesson.pk])},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            original,
            list(self.course.lessons.order_by("pk").values_list("pk", "order")),
        )
        BestQuizAttempt.objects.filter(attempt=self.attempt).update(score=10)
        self.assertFalse(
            get_lesson_lock_status(self.profiles["student"], self.course)[
                self.locked.pk
            ]
        )
        response = self.client.post(
            self.url("edit_course_lessons"),
            {
                "action": "reorder_lessons",
                "order_data": json.dumps(
                    [self.locked.pk, self.lesson.pk, self.draft.pk]
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.edge.refresh_from_db()
        self.assertEqual((self.edge.source_order, self.edge.target_order), (1, 2))
        locks = get_lesson_lock_status(self.profiles["student"], self.course)
        self.assertFalse(locks[self.locked.pk])
        self.assertTrue(locks[self.lesson.pk])
        response = self.client.get(
            self.url("edit_course_lessons"), {"tab": "prerequisites"}
        )
        self.assertEqual(
            response.context_data["lessons_by_order"][1].pk, self.locked.pk
        )
        self.assertEqual(
            response.context_data["lessons_by_order"][2].pk, self.lesson.pk
        )
        self.assertContains(response, "Prerequisites follow lesson positions.")

    def test_contest_list_reordering_requires_the_complete_course_set(self):
        now = timezone.now()
        items = []
        for index in range(2):
            contest = Contest.objects.create(
                key="reordercontest%d" % index,
                name="Reorder contest",
                start_time=now,
                end_time=now + timezone.timedelta(hours=1),
            )
            items.append(
                CourseContest.objects.create(
                    course=self.course,
                    contest=contest,
                    order=index + 1,
                    points=100,
                )
            )
        self.login("teacher")
        for ids in ([items[0].pk], [items[0].pk, items[0].pk], [items[0].pk, 999999]):
            response = self.client.post(
                self.url("course_contest_list"),
                {
                    "action": "reorder_contests",
                    "order_data": json.dumps(ids),
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                list(
                    self.course.contests.order_by("order").values_list("pk", flat=True)
                ),
                [item.pk for item in items],
            )
        response = self.client.post(
            self.url("course_contest_list"),
            {
                "action": "reorder_contests",
                "order_data": json.dumps([item.pk for item in reversed(items)]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(self.course.contests.order_by("order").values_list("pk", flat=True)),
            [item.pk for item in reversed(items)],
        )

    def test_superuser_discovery_and_search_metadata(self):
        self.login("admin")
        response = self.client.get(reverse("course_list"), {"tab": "manage"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["current_tab"], "my")
        self.assertFalse(response.context_data["courses"])
        self.assertNotContains(response, "Manage courses")
        self.assertEqual(self.client.get(self.url("course_detail")).status_code, 200)

        CourseRole.objects.create(
            course=self.course, user=self.profiles["admin"], role="TE"
        )
        response = self.client.get(
            reverse("course_list"),
            {"tab": "my", "sort": "students", "search": "Description"},
        )
        self.assertTrue(response.context_data["can_create_course"])
        self.assertEqual(response.context_data["courses"][0].student_count, 2)
        self.assertEqual(response.context_data["courses"][0].visible_lesson_count, 2)
        organization = Organization.objects.create(
            name="Discovery organization",
            slug="discovery-organization",
            short_name="Discovery",
            registrant=self.profiles["teacher"],
        )
        self.course.organizations.add(organization)
        response = self.client.get(
            reverse("organization_courses", args=[organization.pk, organization.slug]),
            {"tab": "manage"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["current_tab"], "my")
        self.assertNotContains(response, "Manage courses")

    def test_course_sidebar_and_direct_access_follow_membership(self):
        for role in (None, "outsider", "student"):
            with self.subTest(role=role):
                self.client.logout()
                if role:
                    self.login(role)
                response = self.client.get(self.url("course_detail"))
                self.assertEqual(response.status_code, 200)
                if role == "student":
                    self.assertContains(response, self.url("course_grades"))
                else:
                    self.assertNotContains(response, self.url("course_grades"))
                    self.assertContains(response, 'class="course-enrollment-page"')
                    self.assertNotContains(response, "Lesson body")
                for route in ("course_members", "course_edit", "edit_course_lessons"):
                    self.assertNotContains(response, self.url(route))
                for route, args in (
                    ("course_grades", []),
                    ("course_lesson_detail", [self.lesson.pk]),
                    ("course_grades_lesson", [self.lesson.pk]),
                ):
                    direct = self.client.get(self.url(route, *args))
                    self.assertEqual(
                        direct.status_code,
                        200 if role == "student" else 403 if role else 302,
                    )

    def test_anonymous_closed_course_explains_enrollment_before_login(self):
        self.course.is_open = False
        self.course.save(update_fields=["is_open"])
        response = self.client.get(self.url("course_detail"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Enrollment is closed. Contact the instructor for access."
        )
        self.assertContains(response, ">Log in</a>")
        self.assertNotContains(response, "Log in to join")
        self.assertNotContains(response, self.url("course_grades"))

    def test_private_preview_does_not_expose_description(self):
        self.course.is_public = False
        self.course.save()
        self.assertEqual(self.client.get(self.url("course_detail")).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.url("course_detail")).status_code, 404)

    def test_locked_grade_columns_do_not_offer_forbidden_links(self):
        target = self.url("course_grades_lesson", self.locked.pk)
        self.login("student")
        response = self.client.get(self.url("course_grades"))
        self.assertNotContains(response, target)
        self.assertIn(self.locked.pk, response.context_data["locked_lesson_ids"])
        self.login("teacher")
        self.assertContains(self.client.get(self.url("course_grades")), target)

    def test_invalid_new_lesson_preserves_input_and_errors(self):
        self.login("teacher")
        response = self.client.post(
            self.url("course_lesson_create"),
            {
                "title": "Keep my title",
                "content": "Keep my content",
                "points": -1,
                "order": 0,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Keep my title")
        self.assertContains(response, "Keep my content")
        self.assertTrue(response.context_data["lesson_field"].errors)
        self.assertEqual(self.course.lessons.count(), 3)
        self.assertContains(response, 'type="hidden" name="order"')
        self.assertNotContains(response, 'for="id_order"')
        response = self.client.get(self.url("edit_course_lessons_new", self.lesson.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="hidden" name="order"')
        self.assertNotContains(response, 'for="id_order"')

    def test_lesson_creation_enforces_course_limit_before_saving(self):
        self.login("teacher")
        with patch.object(CourseLesson, "MAX_LESSONS_PER_COURSE", 3):
            response = self.client.post(
                self.url("course_lesson_create"),
                {"title": "Too many", "content": "Body", "points": 100, "order": 0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context_data["lesson_field"].non_field_errors())
        self.assertContains(response, "A course cannot have more than 3 lessons.")
        self.assertEqual(self.course.lessons.count(), 3)

    def test_lesson_quiz_limit_is_visible_and_does_not_partially_save(self):
        other = Quiz.objects.create(code="workflow-other-quiz", title="Other Quiz")
        self.login("teacher")
        problem_prefix = "problems_%s" % self.lesson.pk
        quiz_prefix = "quizzes_%s" % self.lesson.pk
        data = {
            "title": "Keep this edit",
            "content": "Keep this content",
            "points": 100,
            "order": 1,
            problem_prefix + "-TOTAL_FORMS": 0,
            problem_prefix + "-INITIAL_FORMS": 0,
            quiz_prefix + "-TOTAL_FORMS": 2,
            quiz_prefix + "-INITIAL_FORMS": 1,
        }
        for index, quiz in enumerate([self.quiz, other]):
            for field, value in {
                "quiz": quiz.pk,
                "lesson": self.lesson.pk,
                "order": index + 1,
                "points": 100,
                "max_attempts": 2,
                "is_visible": "on",
            }.items():
                data["%s-%d-%s" % (quiz_prefix, index, field)] = value
        data[quiz_prefix + "-0-id"] = self.link.pk
        with patch("judge.views.course.MAX_COURSE_LESSON_QUIZZES", 1):
            response = self.client.post(
                self.url("edit_course_lessons_new", self.lesson.pk), data
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A lesson may contain at most 1 quizzes.")
        self.assertContains(response, "Keep this edit")
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.title, "First")
        self.assertEqual(self.lesson.lesson_quizzes.count(), 1)

    def test_course_creation_accepts_grade_sharing_checkbox(self):
        self.login("admin")
        response = self.client.get(reverse("course_add"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context_data["form"]["show_classmates_grades"].value())
        self.assertContains(response, 'type="checkbox" name="show_classmates_grades"')
        for checked in (True, False):
            with self.subTest(checked=checked):
                slug = "workflow-sharing-" + str(checked).lower()
                payload = {"name": slug, "slug": slug, "about": ""}
                if checked:
                    payload["show_classmates_grades"] = "on"
                response = self.client.post(reverse("course_add"), payload)
                self.assertEqual(response.status_code, 302)
                course = Course.objects.get(slug=slug)
                self.assertEqual(course.show_classmates_grades, checked)
                self.assertEqual(
                    CourseRole.objects.get(
                        course=course, user=self.profiles["admin"]
                    ).role,
                    "TE",
                )

    def test_superuser_can_create_course_for_another_organization(self):
        organization = Organization.objects.create(
            name="Workflow organization",
            slug="workflow-organization",
            short_name="Workflow",
            about="",
            registrant=self.profiles["teacher"],
        )
        self.login("admin")
        search = self.client.get(
            reverse("organization_select2"), {"term": organization.name}
        )
        self.assertEqual(
            [row["id"] for row in search.json()["results"]], [organization.pk]
        )
        response = self.client.get(
            reverse("course_add"), {"organization": organization.pk}
        )
        form = response.context_data["form"]
        self.assertIn(organization, form.fields["organizations"].queryset)
        response = self.client.post(
            reverse("course_add"),
            {
                "name": "New workflow course",
                "slug": "workflow-new",
                "about": "",
                "is_public": True,
                "is_open": True,
                "organizations": [organization.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        course = Course.objects.get(slug="workflow-new")
        self.assertTrue(course.organizations.filter(pk=organization.pk).exists())
        self.assertEqual(
            CourseRole.objects.get(course=course, user=self.profiles["admin"]).role,
            "TE",
        )

    def test_organization_creation_shows_actionable_form_error(self):
        organization = Organization.objects.create(
            name="Workflow organization",
            slug="workflow-organization",
            short_name="Workflow",
            about="",
            registrant=self.profiles["teacher"],
        )
        organization.admins.add(self.profiles["teacher"])
        self.login("teacher")
        response = self.client.post(
            reverse("course_add"),
            {"name": "New course", "slug": "workflow-new", "about": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "You must select at least one organization when creating a course.",
        )
        self.assertFalse(Course.objects.filter(slug="workflow-new").exists())

    def test_repeated_join_preserves_existing_role(self):
        self.login("ta")
        response = self.client.post(self.url("course_join"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CourseRole.objects.get(course=self.course, user=self.profiles["ta"]).role,
            "AS",
        )

    def test_join_and_leave_update_membership_and_cached_students(self):
        self.login("outsider")
        outsider = self.profiles["outsider"]
        self.assertNotIn(outsider.pk, [p.pk for p in self.course.get_students()])
        response = self.client.post(self.url("course_join"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CourseRole.objects.get(course=self.course, user=outsider).role, "ST"
        )
        self.assertIn(outsider.pk, [p.pk for p in self.course.get_students()])
        response = self.client.post(self.url("course_leave"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            CourseRole.objects.filter(course=self.course, user=outsider).exists()
        )
        self.assertNotIn(outsider.pk, [p.pk for p in self.course.get_students()])

    def test_course_staff_cannot_leave_their_roles(self):
        for name, role in [("teacher", "TE"), ("ta", "AS")]:
            with self.subTest(role=role):
                self.login(name)
                response = self.client.post(self.url("course_leave"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    CourseRole.objects.get(
                        course=self.course, user=self.profiles[name]
                    ).role,
                    role,
                )

    def test_csv_uses_same_own_grade_and_hidden_score_policy(self):
        self.course.show_classmates_grades = False
        self.course.save()
        self.login("student")
        response = self.client.get(self.url("course_grades"), {"export": "csv"})
        text = response.content.decode()
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(self.profiles["student"].username, text)
        self.assertNotIn(self.profiles["peer"].username, text)
        self.assertIn("Results hidden", text)
        self.assertNotIn("90.0", text)

    def test_grade_sharing_checkbox_saves_both_values_and_resists_ta_changes(self):
        self.assertTrue(self.course.show_classmates_grades)
        payload = {
            "name": self.course.name,
            "about": self.course.about,
            "slug": self.course.slug,
            "is_public": "on",
            "is_open": "on",
        }
        for role, checked, expected in [
            ("teacher", False, False),
            ("ta", True, False),
            ("teacher", True, True),
            ("ta", False, True),
        ]:
            with self.subTest(role=role, checked=checked):
                self.login(role)
                data = dict(payload)
                if checked:
                    data["show_classmates_grades"] = "on"
                response = self.client.post(self.url("course_edit"), data)
                self.assertEqual(response.status_code, 302)
                self.course.refresh_from_db()
                self.assertEqual(self.course.show_classmates_grades, expected)
        self.login("teacher")
        response = self.client.get(self.url("course_edit"))
        self.assertContains(response, 'type="checkbox" name="show_classmates_grades"')
        self.assertContains(response, "Students can see classmates&#39; grades")

    def test_role_hierarchy_and_teacher_only_privacy_setting(self):
        self.login("ta")
        teacher_role = CourseRole.objects.get(
            course=self.course, user=self.profiles["teacher"]
        )
        self.client.post(
            self.url("course_update_member_role"),
            {"member_id": teacher_role.pk, "role": "ST"},
        )
        teacher_role.refresh_from_db()
        self.assertEqual(teacher_role.role, "TE")
        response = self.client.get(self.url("course_edit"))
        self.assertTrue(
            response.context_data["form"].fields["show_classmates_grades"].disabled
        )
        self.login("teacher")
        response = self.client.get(self.url("course_edit"))
        self.assertFalse(
            response.context_data["form"].fields["show_classmates_grades"].disabled
        )

    def test_grade_focus_does_not_override_explicit_next_page(self):
        self.login("student")
        with patch("judge.views.course.GRADES_PAGE_SIZE", 1):
            response = self.client.get(
                self.url("course_grades"),
                {"focus": self.profiles["peer"].username, "page": 2},
            )
        self.assertEqual(response.context_data["page_obj"].number, 2)

    def test_bulk_preview_rejects_membership_changes_and_unknown_users(self):
        self.login("teacher")
        payload = {"usernames": self.profiles["outsider"].username, "role": "ST"}
        response = self.client.post(self.url("course_members"), payload)
        payload.update(
            action="confirm", confirmation=response.context_data["batch_confirmation"]
        )
        CourseRole.objects.create(
            course=self.course, user=self.profiles["outsider"], role="AS"
        )
        response = self.client.post(self.url("course_members"), payload)
        self.assertTrue(response.context_data["form"].non_field_errors())
        self.assertEqual(
            CourseRole.objects.get(
                course=self.course, user=self.profiles["outsider"]
            ).role,
            "AS",
        )
        response = self.client.post(
            self.url("course_members"),
            {"usernames": "no_such_workflow_user", "role": "ST"},
        )
        self.assertTrue(response.context_data["form"].non_field_errors())

    def test_cross_course_ids_cannot_be_reordered_or_reviewed(self):
        other = Course.objects.create(slug="workflow-other", name="Other", about="")
        lesson = CourseLesson.objects.create(
            course=other, title="Other", content="", points=100
        )
        self.login("teacher")
        response = self.client.post(
            self.url("edit_course_lessons"),
            {
                "action": "reorder_lessons",
                "order_data": json.dumps([self.lesson.pk, self.locked.pk, lesson.pk]),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.get(self.url("course_lesson_detail", lesson.pk)).status_code,
            404,
        )

    def test_bulk_prerequisites_reject_duplicate_edges_before_mutation(self):
        self.login("teacher")
        original = list(
            self.course.lesson_prerequisites.values_list(
                "pk", "source_order", "target_order", "required_percentage"
            )
        )
        response = self.client.post(
            self.url("edit_course_lessons"),
            {
                "action": "bulk_update_prereqs",
                "prerequisites_json": json.dumps([[1, 2, 80], [1, 2, 70]]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertIn("Duplicate pair", response.json()["error"])
        self.assertEqual(
            original,
            list(
                self.course.lesson_prerequisites.values_list(
                    "pk", "source_order", "target_order", "required_percentage"
                )
            ),
        )

    def test_bulk_prerequisites_reject_non_integer_and_nonfinite_orders(self):
        self.login("teacher")
        original = list(
            self.course.lesson_prerequisites.values_list(
                "pk", "source_order", "target_order", "required_percentage"
            )
        )
        for source in [1.5, True, float("inf")]:
            with self.subTest(source=source):
                response = self.client.post(
                    self.url("edit_course_lessons"),
                    {
                        "action": "bulk_update_prereqs",
                        "prerequisites_json": json.dumps([[source, 2, 70]]),
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(
                    original,
                    list(
                        self.course.lesson_prerequisites.values_list(
                            "pk",
                            "source_order",
                            "target_order",
                            "required_percentage",
                        )
                    ),
                )

    def test_missing_order_prerequisite_does_not_block_reordering(self):
        CourseLessonPrerequisite.objects.filter(pk=self.edge.pk).update(source_order=99)
        reorder_course_items(
            self.course,
            CourseLesson,
            json.dumps([self.locked.pk, self.lesson.pk, self.draft.pk]),
        )
        self.edge.refresh_from_db()
        self.assertEqual((self.edge.source_order, self.edge.target_order), (99, 2))
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.order, 2)
        self.assertFalse(
            get_lesson_lock_status(self.profiles["student"], self.course)[
                self.lesson.pk
            ]
        )

    def test_clone_copies_quiz_configuration_without_attempts(self):
        self.login("teacher")
        response = self.client.post(
            self.url("clone_course_lesson", self.lesson.pk),
            {"title": "Copied lesson", "course": self.course.slug},
        )
        self.assertEqual(response.status_code, 302)
        cloned = self.course.lessons.get(title="Copied lesson")
        self.assertFalse(cloned.is_visible)
        copied_quiz = cloned.lesson_quizzes.get()
        self.assertEqual(copied_quiz.quiz_id, self.quiz.pk)
        self.assertTrue(copied_quiz.is_result_hidden)
        self.assertFalse(QuizAttempt.objects.filter(lesson_quiz=copied_quiz).exists())

    def test_lesson_sidebars_use_contextual_navigation_in_one_container(self):
        class SidebarParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.containers = 0
                self.links = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "div":
                    if "left-sidebar" in attrs.get("class", "").split():
                        self.containers += 1
                        self.depth = 1
                    elif self.depth:
                        self.depth += 1
                if tag == "a" and self.depth:
                    self.links.append(attrs)

            def handle_endtag(self, tag):
                if tag == "div" and self.depth:
                    self.depth -= 1

        for role in ("teacher", "student"):
            self.login(role)
            for route in ("course_lesson_detail", "course_grades_lesson"):
                response = self.client.get(self.url(route, self.lesson.pk))
                self.assertEqual(response.status_code, 200)
                sidebar = SidebarParser()
                sidebar.feed(response.content.decode())
                self.assertEqual(sidebar.containers, 1)
                links = {link["href"] for link in sidebar.links}
                self.assertIn(self.url("course_detail"), links)
                if route == "course_grades_lesson":
                    self.assertIn(self.url("course_grades"), links)
                else:
                    self.assertNotIn(self.url("course_grades"), links)
                self.assertIn(self.url(route, self.lesson.pk), links)
                self.assertEqual(
                    sum(link.get("aria-current") == "page" for link in sidebar.links), 1
                )
                if role == "student":
                    self.assertNotIn(self.url(route, self.locked.pk), links)
                    self.assertNotIn(self.url(route, self.draft.pk), links)
                self.assertNotIn(self.url("course_members"), links)
                self.assertNotIn(self.url("edit_course_lessons"), links)

    def test_staff_student_preview_keeps_hidden_scores_and_locks(self):
        self.login("teacher")
        query = {"preview": self.profiles["student"].username}
        response = self.client.get(self.url("course_detail"), query)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context_data["is_editable"])
        self.assertContains(response, "View as student")
        self.assertContains(
            response, "Read-only student view. Quiz actions are unavailable."
        )
        self.assertContains(response, "Exit student view")
        self.assertTrue(response.context_data["lesson_lock_status"][self.locked.pk])
        self.assertTrue(response.context_data["total_progress"]["is_result_hidden"])
        response = self.client.get(
            self.url("course_lesson_detail", self.lesson.pk), query
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context_data["lesson_quizzes"][0]["best_score"])
        self.assertEqual(
            self.client.get(
                self.url("course_lesson_detail", self.draft.pk), query
            ).status_code,
            404,
        )
        self.login("student")
        self.assertEqual(
            self.client.get(self.url("course_detail"), query).status_code, 403
        )
        self.client.logout()
        response = self.client.get(self.url("course_detail"), query)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("auth_login") + "?next="))
        self.assertIn("preview%3D", response.url)
        self.assertIn(self.profiles["student"].username, response.url)

    def test_prerequisite_json_uses_orders_and_rejects_backward_edges(self):
        self.login("teacher")
        response = self.client.post(
            self.url("edit_course_lessons"),
            {
                "action": "bulk_update_prereqs",
                "prerequisites_json": json.dumps([[1, 2, 80]]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        edge = self.course.lesson_prerequisites.get()
        self.assertEqual(
            (edge.source_order, edge.target_order, edge.required_percentage), (1, 2, 80)
        )
        for source, target in [(2, 1), (1, 1)]:
            for action in ["add_prereq", "bulk_update_prereqs"]:
                with self.subTest(source=source, target=target, action=action):
                    payload = {
                        "action": action,
                        "source_order": source,
                        "target_order": target,
                        "required_percentage": 90,
                        "prerequisites_json": json.dumps([[source, target, 90]]),
                    }
                    response = self.client.post(
                        self.url("edit_course_lessons"), payload
                    )
                    self.assertFalse(response.json()["success"])
                    unchanged = self.course.lesson_prerequisites.get()
                    self.assertEqual(unchanged.pk, edge.pk)
                    self.assertEqual(unchanged.required_percentage, 80)

    def test_hidden_contest_totals_are_not_exposed_by_course_progress(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="workflowhidden",
            name="Hidden contest",
            start_time=now,
            end_time=now + timezone.timedelta(hours=1),
            is_visible=True,
        )
        CourseContest.objects.create(
            course=self.course, contest=contest, order=1, points=100
        )
        ContestProblem.objects.create(
            contest=contest, quiz=self.quiz, points=100, order=0, is_result_hidden=True
        )
        ContestParticipation.objects.create(
            contest=contest, user=self.profiles["student"], virtual=0, score=75
        )
        self.login("student")
        response = self.client.get(self.url("course_detail"))
        row = next(
            value
            for key, value in response.context_data["contest_progress"].items()
            if key != "total"
        )
        self.assertTrue(row["is_result_hidden"])
        self.assertEqual(row["achieved_points"], 0)
        self.assertTrue(response.context_data["total_progress"]["is_result_hidden"])

    def test_stable_prerequisites_do_not_mix_grades_for_duplicate_display_orders(self):
        CourseLesson.objects.filter(pk__in=[self.lesson.pk, self.locked.pk]).update(
            order=7
        )
        BestQuizAttempt.objects.filter(
            user=self.profiles["student"], lesson_quiz=self.link
        ).update(score=10)
        CourseRole.objects.filter(
            course=self.course, user=self.profiles["student"]
        ).update(needs_progress_recalculation=True)
        self.assertFalse(
            get_lesson_lock_status(self.profiles["student"], self.course)[
                self.locked.pk
            ]
        )
