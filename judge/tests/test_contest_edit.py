"""
Tests for the unified /contest/<key>/edit page.

Covers the fixes layered on the standalone-edit feature branch:
  1. ContestEdit.dispatch returns 403 (not 500) for non-editors hitting
     org-private contests they cannot view.
  2. ContestEdit.post is atomic — row-formset deletions/saves roll back
     when the main form fails validation.
  3. ContestEditForm `is_visible` scoped/unscoped rule + clean() validator.
  4. Sidebar Admin tab is gated by is_superuser, not the broader
     `change_contest` perm.
  5. Old org-edit and course-edit URLs redirect to the new standalone page.
  6. is_organization_private auto-syncs on M2M signal (sanity check —
     this isn't new behavior but it underpins the form correctness).
"""

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from judge.models import (
    Contest,
    ContestProblem,
    Course,
    Language,
    Organization,
    Problem,
    ProblemGroup,
    Profile,
)
from judge.models.course import CourseContest, CourseRole, RoleInCourse


class ContestEditTestBase(TestCase):
    """Shared fixtures: language, users, contests of all three kinds."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test", defaults={"full_name": "Test Group"}
        )

    def _make_problem(self, code):
        return Problem.objects.create(
            code=code,
            name=f"Problem {code}",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=1.0,
        )

    def _make_user(self, username, *, is_superuser=False):
        user = User.objects.create_user(username=username, password="password123")
        if is_superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save()
        Profile.objects.get_or_create(user=user, defaults={"language": self.language})
        return user

    def _make_contest(self, key, *, author, **kwargs):
        now = timezone.now()
        contest = Contest.objects.create(
            key=key,
            name=f"Contest {key}",
            start_time=kwargs.pop("start_time", now),
            end_time=kwargs.pop("end_time", now + timezone.timedelta(hours=2)),
            **kwargs,
        )
        contest.authors.add(author.profile)
        contest._author_ids.dirty(contest)
        return contest

    def setUp(self):
        # Author + curator + outsider + superuser
        self.author = self._make_user("author1")
        self.curator = self._make_user("curator1")
        self.outsider = self._make_user("outsider1")
        self.superuser = self._make_user("super1", is_superuser=True)

        # Org with an admin
        self.org_admin = self._make_user("orgadmin1")
        self.org = Organization.objects.create(
            name="Test Org",
            slug="test-org",
            short_name="TO",
            about="org",
            registrant=self.org_admin.profile,
            is_open=True,
        )
        self.org.admins.add(self.org_admin.profile)

        # Course with teacher + student
        self.teacher = self._make_user("teacher1")
        self.student = self._make_user("student1")
        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course",
            about="course",
            is_open=True,
        )
        CourseRole.objects.create(
            course=self.course,
            user=self.teacher.profile,
            role=RoleInCourse.TEACHER,
        )
        CourseRole.objects.create(
            course=self.course,
            user=self.student.profile,
            role=RoleInCourse.STUDENT,
        )

        # Three contests: public/visible, org-private, course-private
        self.public_contest = self._make_contest(
            "publicc", author=self.author, is_visible=True
        )
        self.org_contest = self._make_contest(
            "orgc", author=self.author, is_visible=True
        )
        self.org_contest.organizations.add(self.org)
        self.org_contest._curator_ids.dirty(self.org_contest)

        self.course_contest = self._make_contest(
            "coursec", author=self.author, is_visible=True, is_in_course=True
        )
        CourseContest.objects.create(
            course=self.course,
            contest=self.course_contest,
            order=1,
            points=0,
        )

    def _refresh_contest(self, contest):
        contest._author_ids.dirty(contest)
        contest._curator_ids.dirty(contest)
        return Contest.objects.get(pk=contest.pk)


class ContestEditPermissionTests(ContestEditTestBase):
    def test_anonymous_redirected_to_login(self):
        url = reverse("contest_edit", args=[self.public_contest.key])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_author_can_edit_own_contest(self):
        self.client.force_login(self.author)
        resp = self.client.get(reverse("contest_edit", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 200)

    def test_curator_can_edit(self):
        self.public_contest.curators.add(self.curator.profile)
        self._refresh_contest(self.public_contest)
        self.client.force_login(self.curator)
        resp = self.client.get(reverse("contest_edit", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 200)

    def test_org_admin_can_edit_org_contest(self):
        self.client.force_login(self.org_admin)
        resp = self.client.get(reverse("contest_edit", args=[self.org_contest.key]))
        self.assertEqual(resp.status_code, 200)

    def test_course_teacher_can_edit_course_contest(self):
        self.client.force_login(self.teacher)
        resp = self.client.get(reverse("contest_edit", args=[self.course_contest.key]))
        self.assertEqual(resp.status_code, 200)

    def test_course_student_denied_on_course_contest(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse("contest_edit", args=[self.course_contest.key]))
        self.assertEqual(resp.status_code, 403)

    def test_outsider_denied_on_public_contest(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("contest_edit", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 403)

    def test_outsider_denied_on_org_private_returns_403_not_500(self):
        """Bug #1 regression test: PrivateContestError must be caught."""
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("contest_edit", args=[self.org_contest.key]))
        # Pre-fix this returned 500 with PrivateContestError stack trace.
        self.assertEqual(resp.status_code, 403)

    def test_superuser_can_edit_any_contest(self):
        self.client.force_login(self.superuser)
        for contest in (self.public_contest, self.org_contest, self.course_contest):
            resp = self.client.get(reverse("contest_edit", args=[contest.key]))
            self.assertEqual(
                resp.status_code, 200, f"superuser denied on {contest.key}"
            )


class ContestEditFormVisibilityTests(ContestEditTestBase):
    """is_visible scoped/unscoped rule from ContestEditForm."""

    def _form(self, contest, user, **post_overrides):
        from judge.forms import ContestEditForm

        post = {
            "key": contest.key,
            "name": contest.name,
            "authors": [str(p.id) for p in contest.authors.all()],
            "curators": [],
            "testers": [],
            "start_time": contest.start_time.isoformat(),
            "end_time": contest.end_time.isoformat(),
            "format_name": contest.format_name,
            "format_config": "{}",
            "is_visible": "on" if contest.is_visible else "",
            "scoreboard_visibility": contest.scoreboard_visibility,
            "points_precision": str(contest.points_precision),
            "description": contest.description or "",
            "organizations": [str(o.id) for o in contest.organizations.all()],
            "private_contestants": [],
            "view_contest_scoreboard": [],
            "banned_users": [],
        }
        post.update(post_overrides)
        return ContestEditForm(data=post, instance=contest, user=user)

    def test_visibility_disabled_for_unscoped_hidden_non_superuser(self):
        self.public_contest.is_visible = False
        self.public_contest.save()
        form = self._form(self.public_contest, self.author)
        self.assertTrue(form.fields["is_visible"].disabled)

    def test_visibility_editable_for_unscoped_visible_non_superuser(self):
        # visible+unscoped → editor can demote (field is NOT disabled)
        form = self._form(self.public_contest, self.author)
        self.assertFalse(form.fields["is_visible"].disabled)

    def test_visibility_editable_for_scoped_hidden_non_superuser(self):
        # hidden+scoped (org-private) → editor can promote within scope
        self.org_contest.is_visible = False
        self.org_contest.save()
        form = self._form(self.org_contest, self.author)
        self.assertFalse(form.fields["is_visible"].disabled)

    def test_visibility_always_editable_for_superuser(self):
        self.public_contest.is_visible = False
        self.public_contest.save()
        form = self._form(self.public_contest, self.superuser)
        self.assertFalse(form.fields["is_visible"].disabled)

    def test_clean_rejects_promotion_attempt_for_unscoped(self):
        """Server-side safety net catches the case where `disabled` was
        somehow bypassed (e.g. programmatic save, race)."""
        self.public_contest.is_visible = False
        self.public_contest.save()
        # Hand-craft form bypass: build form with user but force-enable the
        # field so cleaned_data carries the promotion attempt.
        with translation.override("en"):
            form = self._form(self.public_contest, self.author, is_visible="on")
            form.fields["is_visible"].disabled = False
            self.assertFalse(form.is_valid())
            self.assertIn("Only administrators", str(form.errors))

    def test_organizations_disabled_for_non_superuser(self):
        form = self._form(self.org_contest, self.author)
        self.assertTrue(form.fields["organizations"].disabled)

    def test_organizations_editable_for_superuser(self):
        form = self._form(self.org_contest, self.superuser)
        self.assertFalse(form.fields["organizations"].disabled)


class ContestEditAtomicityTests(ContestEditTestBase):
    """Bug #2: post() validates both forms before any DB write,
    and wraps the row saves in transaction.atomic()."""

    def test_invalid_form_rolls_back_row_deletions(self):
        # Add a problem-row so we have something to delete.
        problem = self._make_problem("atomicprob1")
        cp = ContestProblem.objects.create(
            contest=self.public_contest,
            problem=problem,
            order=1,
            points=100,
        )
        self.client.force_login(self.author)
        url = reverse("contest_edit", args=[self.public_contest.key])

        post = {
            "key": self.public_contest.key,
            "name": self.public_contest.name,
            "authors": [str(p.id) for p in self.public_contest.authors.all()],
            "curators": [],
            "testers": [],
            "start_time": self.public_contest.start_time.isoformat(),
            "end_time": self.public_contest.end_time.isoformat(),
            "format_name": self.public_contest.format_name,
            "format_config": "this is NOT valid JSON {{",  # main form fails
            "is_visible": "on",
            "scoreboard_visibility": self.public_contest.scoreboard_visibility,
            "points_precision": "2",
            "description": "",
            "organizations": [],
            "private_contestants": [],
            "view_contest_scoreboard": [],
            "banned_users": [],
            "rows-TOTAL_FORMS": "1",
            "rows-INITIAL_FORMS": "1",
            "rows-MIN_NUM_FORMS": "0",
            "rows-MAX_NUM_FORMS": "1000",
            "rows-0-id": str(cp.id),
            "rows-0-order": "1",
            "rows-0-problem": "",  # row marked for delete
            "rows-0-quiz": "",
            "rows-0-points": "100",
            "rows-0-DELETE": "on",
        }

        resp = self.client.post(url, post)
        # Form re-rendered with errors (200), not redirected (302).
        self.assertEqual(resp.status_code, 200)
        # Row deletion was rolled back.
        self.assertTrue(ContestProblem.objects.filter(id=cp.id).exists())

    def test_organization_signal_flips_is_organization_private(self):
        """Sanity: adding an org via signal flips is_organization_private.
        Underpins ContestEditForm's correctness — the form doesn't manage
        this flag itself but relies on the m2m_changed signal."""
        contest = self.public_contest
        self.assertFalse(contest.is_organization_private)
        contest.organizations.add(self.org)
        contest.refresh_from_db()
        self.assertTrue(contest.is_organization_private)
        contest.organizations.clear()
        contest.refresh_from_db()
        self.assertFalse(contest.is_organization_private)


class ContestEditRedirectTests(ContestEditTestBase):
    """The old org-edit and course-edit URLs must redirect to the new
    standalone page (perm enforcement happens at the destination)."""

    def test_org_contest_edit_redirects_to_standalone(self):
        self.client.force_login(self.org_admin)
        old_url = reverse(
            "organization_contest_edit",
            args=[self.org.id, self.org.slug, self.org_contest.key],
        )
        resp = self.client.get(old_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.url,
            reverse("contest_edit", args=[self.org_contest.key]),
        )

    def test_course_contest_edit_redirects_to_standalone(self):
        self.client.force_login(self.teacher)
        old_url = reverse(
            "edit_course_contest",
            args=[self.course.slug, self.course_contest.key],
        )
        resp = self.client.get(old_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.url,
            reverse("contest_edit", args=[self.course_contest.key]),
        )


class ContestSidebarAdminTabTests(ContestEditTestBase):
    """The Admin tab on the contest sidebar must be visible only to
    superusers, even when the user has the `change_contest` perm."""

    def test_admin_tab_hidden_for_non_superuser_with_change_contest_perm(self):
        # Grant change_contest perm via group, like the prod Contest Setter.
        perm = Permission.objects.get(
            content_type__app_label="judge", codename="change_contest"
        )
        group, _ = Group.objects.get_or_create(name="TestContestSetter")
        group.permissions.add(perm)
        self.author.groups.add(group)
        self.assertTrue(self.author.has_perm("judge.change_contest"))
        self.assertFalse(self.author.is_superuser)

        self.client.force_login(self.author)
        resp = self.client.get(reverse("contest_view", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 200)
        # Edit tab visible, Admin tab NOT visible.
        edit_url = reverse("contest_edit", args=[self.public_contest.key])
        admin_url = reverse("admin:judge_contest_change", args=[self.public_contest.id])
        self.assertContains(resp, f'href="{edit_url}"')
        self.assertNotContains(resp, f'href="{admin_url}"')

    def test_admin_tab_visible_for_superuser(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("contest_view", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 200)
        admin_url = reverse("admin:judge_contest_change", args=[self.public_contest.id])
        self.assertContains(resp, f'href="{admin_url}"')

    def test_neither_tab_visible_for_outsider(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("contest_view", args=[self.public_contest.key]))
        self.assertEqual(resp.status_code, 200)
        edit_url = reverse("contest_edit", args=[self.public_contest.key])
        admin_url = reverse("admin:judge_contest_change", args=[self.public_contest.id])
        self.assertNotContains(resp, f'href="{edit_url}"')
        self.assertNotContains(resp, f'href="{admin_url}"')
