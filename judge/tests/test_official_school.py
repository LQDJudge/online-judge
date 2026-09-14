from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs

from django.contrib.admin.models import LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from judge.forms import EditOrganizationForm
from judge.admin.organization import OfficialSchoolForm
from judge.admin.contest import ContestsSummaryAdmin
from judge.models import (
    Contest,
    Block,
    ContestParticipation,
    ContestsSummary,
    Language,
    Notification,
    NotificationProfile,
    OfficialSchool,
    Organization,
    OrganizationProfile,
    Profile,
    Course,
    CourseRole,
    Problem,
    ProblemGroup,
    UsernameModerationCase,
)
from chat_box.models import UserRoom
from chat_box.services.rooms import create_organization_channel
from chat_box.services.memberships import rejoin_organization_channel
from chat_box.services.organization_sync import organization_role, organization_user_ids
from judge.services.contest_summary import (
    calculate_summary,
    read_summary,
    validate_scores,
)
from judge.services.official_school import (
    bulk_enroll_school,
    configure_school,
    create_school,
    enroll_school,
    remove_school_member,
)
from judge.tasks.import_users import import_users
from judge.views.select2 import _get_user_queryset
from judge.views.organization import OrganizationList


class SchoolFixtures:
    def setUp(self):
        cache.clear()
        self.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        self.root = self.user("schoolroot", superuser=True)
        self.teacher = self.user("schoolteacher")
        self.student = self.user("schoolstudent")
        self.other = self.user("schoolother")
        self.a = self.school("schoola")
        self.b = self.school("schoolb")

    def user(self, username, superuser=False):
        user = User.objects.create_user(
            username,
            password="school-test-password",
            is_superuser=superuser,
            is_staff=superuser,
        )
        Profile.objects.get_or_create(user=user, defaults={"language": self.language})
        return user

    def school(self, slug):
        org = create_school(
            self.root,
            name=slug,
            short_name=slug,
            slug=slug,
            teachers=[self.teacher.profile],
        ).organization
        org.about = "PRIVATE DESCRIPTION"
        org.access_code = "abc1234"
        org.save(update_fields=["about", "access_code"])
        org.refresh_from_db()
        return org

    def enroll(self, org=None, user=None):
        return enroll_school(
            self.teacher, (org or self.a).pk, (user or self.student).profile.pk
        )

    def school_admin_data(self, org, **updates):
        data = {
            "name": org.name,
            "slug": org.slug,
            "short_name": org.short_name,
            "about": org.about,
            "slots": "" if org.slots is None else org.slots,
            "admins": list(org.admins.values_list("pk", flat=True)),
            "moderators": [],
            "is_active": "on",
            "_save": "Save",
        }
        data.update(updates)
        return data


class SchoolTestCase(SchoolFixtures, TestCase):

    def test_mine_badges_only_official_schools_for_teachers_and_students(self):
        ordinary = Organization.objects.create(
            name="Ordinary group",
            slug="ordinary-badge",
            short_name="Ordinary",
            registrant=self.root.profile,
        )
        ordinary.members.add(self.teacher.profile, self.student.profile)
        self.enroll()
        for user, badge_count in ((self.teacher, 2), (self.student, 1)):
            self.client.force_login(user)
            response = self.client.get(reverse("organization_list"), {"tab": "mine"})
            self.assertContains(
                response, 'class="official-school-badge"', count=badge_count
            )
            self.assertContains(response, ordinary.name)
            schools = {
                org.pk: org.is_official_school
                for org in response.context["organizations"]
            }
            self.assertTrue(schools[self.a.pk])
            self.assertFalse(schools[ordinary.pk])

    def test_school_badge_annotation_has_no_per_card_queries(self):
        request = RequestFactory().get("/organizations/?tab=mine")
        request.user = self.teacher
        request.profile = self.teacher.profile
        view = OrganizationList()
        view.setup(request)
        view.get(request)
        queryset = view.get_queryset()
        with self.assertNumQueries(1):
            schools = {org.pk: org.is_official_school for org in queryset}
        self.assertEqual(schools, {self.a.pk: True, self.b.pk: True})

    def test_teacher_is_not_student(self):
        self.assertFalse(self.teacher.profile.organizations.exists())
        self.assertTrue(self.a.admins.filter(pk=self.teacher.profile.pk).exists())
        self.assertEqual(
            set(self.teacher.profile.get_content_organizations()), {self.a, self.b}
        )

    def test_first_enrollment_and_idempotence(self):
        self.assertTrue(self.enroll())
        self.assertFalse(self.enroll())
        self.assertEqual(list(self.student.profile.organizations.all()), [self.a])
        self.assertEqual(
            LogEntry.objects.filter(object_id=str(self.student.profile.pk)).count(), 1
        )

    def test_unauthorized_enrollment(self):
        with self.assertRaises(PermissionDenied):
            enroll_school(self.other, self.a.pk, self.student.profile.pk)
        self.assertFalse(self.a.members.exists())

    def test_transfer_requires_student_confirmation(self):
        self.enroll()
        with self.assertRaises(ValidationError):
            self.enroll(self.b)
        enroll_school(
            self.student,
            self.b.pk,
            self.student.profile.pk,
            code="abc1234",
            expected_school=self.a.pk,
        )
        self.assertEqual(list(self.student.profile.organizations.all()), [self.b])

    def test_transfer_does_not_disclose_destination_to_former_school_teachers(self):
        former_teacher = self.user("formerschoolteacher")
        self.enroll()
        self.a.admins.add(former_teacher.profile)
        self.a.admins.remove(self.teacher.profile)

        with self.captureOnCommitCallbacks(execute=True):
            enroll_school(
                self.student,
                self.b.pk,
                self.student.profile.pk,
                code="abc1234",
                expected_school=self.a.pk,
            )

        notification = Notification.objects.get(owner=former_teacher.profile)
        self.assertIn(self.a.name, notification.html_link)
        self.assertNotIn(self.b.name, notification.html_link)
        self.assertEqual(notification.extra_data, {"official_school": self.a.pk})

    def test_stale_confirmation_and_revoked_invitation(self):
        self.enroll()
        with self.assertRaises(ValidationError):
            enroll_school(
                self.student,
                self.b.pk,
                self.student.profile.pk,
                code="abc1234",
                expected_school=None,
            )
        with self.assertRaises(PermissionDenied):
            enroll_school(
                self.student,
                self.b.pk,
                self.student.profile.pk,
                code="revoked",
                expected_school=self.a.pk,
            )
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())

    def test_raw_forward_and_reverse_add_rejected(self):
        for manager, value in (
            (self.student.profile.organizations, self.a),
            (self.a.members, self.student.profile),
        ):
            with self.assertRaises(ValidationError), transaction.atomic():
                manager.add(value)
        self.assertFalse(self.a.members.exists())

    def test_raw_remove_clear_set_rejected(self):
        self.enroll()
        for mutation in (
            lambda: self.a.members.remove(self.student.profile),
            lambda: self.student.profile.organizations.clear(),
            lambda: self.a.members.set([]),
        ):
            with self.assertRaises(ValidationError), transaction.atomic():
                mutation()
        self.assertTrue(self.a.members.exists())

    def test_leave_preserves_teacher_role(self):
        self.enroll(user=self.teacher)
        remove_school_member(self.teacher, self.a.pk, self.teacher.profile.pk)
        self.assertFalse(self.a.members.exists())
        self.assertTrue(self.a.admins.filter(pk=self.teacher.profile.pk).exists())

    def test_archive_blocks_enrollment_not_leaving(self):
        self.enroll()
        configure_school(self.root, self.a, is_active=False)
        with self.assertRaises(ValidationError):
            self.enroll(user=self.other)
        remove_school_member(self.student, self.a.pk, self.student.profile.pk)
        self.assertFalse(self.a.members.exists())

    def test_last_admin_and_school_deletion_protected(self):
        with self.assertRaises(ValidationError), transaction.atomic():
            self.a.admins.clear()
        with self.assertRaises(ValidationError), transaction.atomic():
            OfficialSchool.objects.filter(pk=self.a.pk).delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.a.delete()
        self.assertTrue(Organization.objects.filter(pk=self.a.pk).exists())

    def test_school_admin_profile_must_be_unassigned_before_deletion(self):
        self.a.admins.add(self.other.profile)
        with self.assertRaises(ValidationError), transaction.atomic():
            Profile.objects.filter(
                pk__in=[self.teacher.profile.pk, self.other.profile.pk]
            ).delete()

        self.b.admins.add(self.other.profile)
        self.a.admins.remove(self.teacher.profile)
        self.b.admins.remove(self.teacher.profile)
        teacher_profile_id = self.teacher.profile.pk
        self.teacher.profile.delete()
        self.assertFalse(Profile.objects.filter(pk=teacher_profile_id).exists())

    def test_csv_import_logs_skipped_school_enrollment(self):
        row = {
            "username": self.student.username,
            "password": "",
            "name": "",
            "school": "",
            "email": "",
            "organizations": self.a.slug,
        }
        with override("en"), patch.object(import_users, "update_state"):
            self.assertEqual(
                import_users.run([row], profile_id=self.other.profile.pk, muted=True),
                1,
            )
        self.assertFalse(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertIn(
            "School enrollment skipped",
            cache.get("import_users_log_%d" % self.other.profile.pk),
        )

    def test_official_school_rejects_and_hides_moderators(self):
        with self.assertRaises(ValidationError), transaction.atomic():
            self.a.moderators.add(self.other.profile)
        with self.assertRaises(ValidationError), transaction.atomic():
            self.other.profile.moderated_organizations.add(self.a)

        site_form = EditOrganizationForm(
            instance=self.a,
            profile=self.teacher.profile,
            org_id=self.a.pk,
        )
        internal_form = OfficialSchoolForm(
            instance=OfficialSchool.objects.get(pk=self.a.pk)
        )
        self.assertNotIn("moderators", site_form.fields)
        self.assertNotIn("moderators", internal_form.fields)

    def test_invalid_school_moderator_row_does_not_grant_access(self):
        Organization.moderators.through.objects.create(
            organization_id=self.a.pk,
            profile_id=self.other.profile.pk,
        )
        self.assertFalse(self.a.school_accessible_by(self.other))
        self.assertEqual(
            Organization.visible_instances([self.a.pk], self.other),
            [],
        )
        self.assertIsNone(organization_role(self.a, self.other.profile.pk))
        self.assertNotIn(self.other.profile.pk, organization_user_ids(self.a))

    def test_cannot_reopen_school(self):
        self.a.is_open = True
        with self.assertRaises(ValidationError), transaction.atomic():
            self.a.save(update_fields=["is_open"])

    def test_configure_school_cannot_convert_ordinary_group(self):
        self.enroll()
        org = Organization.objects.create(
            name="ordinary",
            slug="ordinary",
            short_name="ordinary",
            registrant=self.root.profile,
        )
        org.admins.add(self.teacher.profile)
        org.members.add(self.student.profile)
        with self.assertRaises(OfficialSchool.DoesNotExist):
            configure_school(self.root, org)
        self.assertFalse(OfficialSchool.objects.filter(pk=org.pk).exists())
        self.assertTrue(org.members.filter(pk=self.student.profile.pk).exists())

    def test_bulk_add_reports_without_transferring(self):
        self.enroll(self.b)
        result = bulk_enroll_school(
            self.teacher,
            self.a.pk,
            [
                self.student.username,
                self.other.username,
                "missing",
                self.other.username,
            ],
        )
        self.assertEqual(result["added"], [self.other.username])
        self.assertEqual(result["unknown"], ["missing"])
        self.assertEqual(result["conflict"], [self.student.username])
        self.assertTrue(self.b.members.filter(pk=self.student.profile.pk).exists())

    def test_bulk_capacity_is_not_a_transfer_conflict(self):
        self.a.slots = 0
        self.a.save(update_fields=["slots"])
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("add_organization_member", args=[self.a.pk, self.a.slug]),
            {"new_users": self.student.username},
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertContains(response, "school has reached its member limit")
        self.assertNotContains(response, "they belong to another official school")
        self.assertNotContains(
            response, "They remain in their current school until then."
        )
        self.assertEqual(
            response.context["form"]["new_users"].value(), self.student.username
        )
        self.assertFalse(self.a.members.exists())

    def test_bulk_capacity_respects_teacher_input_order(self):
        self.a.slots = 1
        self.a.save(update_fields=["slots"])
        result = bulk_enroll_school(
            self.teacher,
            self.a.pk,
            [self.other.username, self.student.username],
        )
        self.assertEqual(result["added"], [self.other.username])
        self.assertEqual(result["full"], [self.student.username])

    def test_blocked_former_student_can_unblock_without_private_access(self):
        self.enroll()
        self.client.force_login(self.student)
        self.client.post(
            reverse("block_organization", args=[self.a.pk, self.a.slug]),
            {"confirm": "yes"},
        )
        self.assertFalse(self.a.members.exists())
        self.assertTrue(Block.is_blocked(self.student.profile, self.a))
        self.assertEqual(self.client.get(self.a.get_absolute_url()).status_code, 404)
        response = self.client.post(
            reverse("unblock_organization", args=[self.a.pk, self.a.slug])
        )
        self.assertRedirects(response, reverse("organization_list") + "?tab=blocked")
        self.assertFalse(Block.is_blocked(self.student.profile, self.a))
        self.assertFalse(self.a.members.exists())
        self.assertEqual(self.client.get(self.a.get_absolute_url()).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse("unblock_organization", args=[self.b.pk, self.b.slug])
            ).status_code,
            404,
        )

    def test_teacher_admin_picker_searches_beyond_school_roster(self):
        self.client.force_login(self.teacher)
        response = self.client.get(
            reverse("edit_organization", args=[self.a.pk, self.a.slug])
        )
        picker = response.context["form"].fields["admins"].widget
        self.assertEqual(str(picker.get_url()), reverse("profile_select2"))
        results = self.client.get(
            picker.get_url(), {"term": self.other.username}
        ).json()["results"]
        self.assertIn(self.other.profile.pk, [result["id"] for result in results])

    def test_private_routes_and_denied_post_has_no_side_effect(self):
        self.enroll()
        url = reverse("organization_home", args=[self.a.pk, self.a.slug])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        kick = reverse("organization_user_kick", args=[self.a.pk, self.a.slug])
        self.assertEqual(
            self.client.post(kick, {"user": self.student.profile.pk}).status_code, 404
        )
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertFalse(
            OrganizationProfile.objects.filter(
                organization=self.a, profile=self.other.profile
            ).exists()
        )

    def test_invitation_get_only_exposes_identity(self):
        self.client.force_login(self.student)
        url = reverse("join_organization", args=[self.a.pk, self.a.slug])
        response = self.client.get(url, {"code": "abc1234"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE DESCRIPTION")
        self.assertContains(response, 'id="school-confirm-panel"')
        self.assertContains(response, 'class="school-invitation-page"')
        self.assertNotContains(response, "<dialog")
        self.assertNotContains(response, "school-confirm.js")
        self.assertFalse(self.a.members.exists())
        response = self.client.post(url, {"code": "abc1234", "expected_school": ""})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())

    def test_current_school_invitation_opens_school_without_join_form(self):
        self.enroll()
        self.client.force_login(self.student)
        response = self.client.get(
            reverse("join_organization", args=[self.a.pk, self.a.slug]),
            {"code": "abc1234"},
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You already belong to this school.")
        self.assertContains(response, "Open school")
        self.assertContains(response, 'href="%s"' % self.a.get_absolute_url())
        self.assertNotContains(response, 'name="expected_school"')
        self.assertNotContains(response, 'name="confirm"')
        self.assertNotContains(response, "PRIVATE DESCRIPTION")
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())

    def test_school_creation_teacher_selector_has_explicit_width(self):
        form = OfficialSchoolForm()
        self.assertEqual(form.fields["teachers"].widget.attrs["data-width"], "100%")

    def test_transfer_confirmation_renders_in_english_and_vietnamese(self):
        self.enroll()
        self.a.name = 'School A <script>alert("school")</script>'
        self.a.save(update_fields=["name"])
        self.b.name = "School B & friends"
        self.b.save(update_fields=["name"])
        self.client.force_login(self.student)
        url = reverse("join_organization", args=[self.b.pk, self.b.slug])
        for language, expected in (("en", "You will leave"), ("vi", "Bạn sẽ rời")):
            with self.subTest(language=language), override(language):
                response = self.client.get(
                    url, {"code": "abc1234"}, HTTP_ACCEPT_LANGUAGE=language
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)
                self.assertContains(response, "School A &lt;script&gt;")
                self.assertContains(response, "School B &amp; friends")
                self.assertNotContains(response, '<script>alert("school")</script>')
                self.assertNotContains(response, "PRIVATE DESCRIPTION")
                self.assertTrue(
                    self.a.members.filter(pk=self.student.profile.pk).exists()
                )
                self.assertFalse(
                    self.b.members.filter(pk=self.student.profile.pk).exists()
                )
        response = self.client.post(
            url, {"code": "abc1234", "expected_school": self.a.pk, "confirm": "yes"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertTrue(self.b.members.filter(pk=self.student.profile.pk).exists())

    def test_roster_lookup_requires_access(self):
        with self.assertRaises(PermissionDenied):
            _get_user_queryset("", self.a.pk, self.other)
        self.enroll()
        self.assertEqual(_get_user_queryset("", self.a.pk, self.teacher).count(), 1)

    def test_teacher_form_cannot_edit_identity(self):
        form = EditOrganizationForm(
            instance=self.a, profile=self.teacher.profile, org_id=self.a.pk
        )
        self.assertFalse({"name", "slug", "short_name", "is_open"} & set(form.fields))
        self.assertIn("admins", form.fields)

    def test_transaction_rollback_does_not_publish_membership_cache(self):
        self.student.profile.get_organization_ids()
        try:
            with transaction.atomic():
                self.enroll()
                self.assertIn(self.a.pk, self.student.profile.get_organization_ids())
                raise ValueError
        except ValueError:
            pass
        self.assertNotIn(self.a.pk, self.student.profile.get_organization_ids())

    def test_course_roles_survive_leaving(self):
        course = Course.objects.create(
            name="School course", slug="schoolcourse", about="Course"
        )
        course.organizations.add(self.a)
        self.enroll()
        CourseRole.objects.create(course=course, user=self.student.profile)
        self.assertFalse(Course.is_accessible_by(course, self.teacher.profile))
        self.assertFalse(Course.is_editable_by(course, self.teacher.profile))
        remove_school_member(self.student, self.a.pk, self.student.profile.pk)
        self.assertTrue(Course.is_accessible_by(course, self.student.profile))

    def test_teacher_school_content_access_not_editing_or_visibility_bypass(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="schoolaccess",
            name="Access",
            start_time=now,
            end_time=now + timedelta(hours=1),
            is_visible=True,
        )
        contest.organizations.add(self.a)
        contest.refresh_from_db()
        contest.access_check(self.teacher)
        self.assertIn(contest, Contest.get_visible_contests(self.teacher))
        contest.is_visible = False
        contest.save()
        with self.assertRaises(Contest.Inaccessible):
            contest.access_check(self.teacher)
        group = ProblemGroup.objects.create(
            name="schooltests", full_name="School tests"
        )
        problem = Problem.objects.create(
            code="schoolaccess",
            name="Access",
            group=group,
            is_public=True,
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        problem.organizations.add(self.a)
        problem.refresh_from_db()
        self.assertTrue(problem.is_accessible_by(self.teacher))
        self.assertFalse(problem.is_accessible_by(self.other))
        self.assertFalse(problem.is_editable_by(self.teacher))

    def test_school_creation_requires_identity_and_teachers(self):
        form = OfficialSchoolForm(data={})
        self.assertFalse(form.is_valid())
        for field in ("new_name", "new_slug", "new_short_name", "teachers"):
            self.assertIn(field, form.errors)
        for field in ("organization", "confirm_roster", "roster_token"):
            self.assertNotIn(field, form.fields)

    def test_teacher_bulk_add_route_and_hidden_identity_fields(self):
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("add_organization_member", args=[self.a.pk, self.a.slug]),
            {"new_users": self.student.username},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())
        response = self.client.get(
            reverse("edit_organization", args=[self.a.pk, self.a.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="is_open"')
        self.assertNotContains(response, 'name="slug"')

    def test_bulk_add_feedback_is_inline_and_preserves_only_skipped_users(self):
        self.enroll(self.b)
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("add_organization_member", args=[self.a.pk, self.a.slug]),
            {"new_users": self.student.username + " " + self.other.username},
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Added students: " + self.other.username)
        self.assertContains(
            response,
            "Not added because they belong to another official school: "
            + self.student.username,
        )
        self.assertContains(response, "They remain in their current school until then.")
        self.assertContains(response, 'class="school-add-feedback"')
        self.assertContains(response, ">Add members </button>")
        self.assertNotContains(response, "Usernames not found:")
        self.assertEqual(
            response.context["form"]["new_users"].value(), self.student.username
        )
        self.assertEqual(list(get_messages(response.wsgi_request)), [])
        self.assertTrue(self.a.members.filter(pk=self.other.profile.pk).exists())
        self.assertTrue(self.b.members.filter(pk=self.student.profile.pk).exists())
        self.assertFalse(self.a.members.filter(pk=self.student.profile.pk).exists())
        response = self.client.get(
            reverse("add_organization_member", args=[self.a.pk, self.a.slug])
        )
        self.assertNotContains(response, 'class="school-add-feedback"')

    def test_bulk_add_success_clears_input_without_queued_notifications(self):
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("add_organization_member", args=[self.a.pk, self.a.slug]),
            {"new_users": self.student.username},
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["new_users"].value(), "")
        self.assertContains(response, "Added students: " + self.student.username)
        self.assertNotContains(response, "Not added because")
        self.assertEqual(list(get_messages(response.wsgi_request)), [])

    def test_school_admin_pages_and_rejected_conversion(self):
        self.client.force_login(self.root)
        for url in (
            reverse("admin:judge_officialschool_add"),
            reverse("admin:judge_officialschool_change", args=[self.a.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertRedirects(
            self.client.get(
                reverse("admin:judge_organization_change", args=[self.a.pk])
            ),
            reverse("admin:judge_officialschool_change", args=[self.a.pk]),
        )
        org = Organization.objects.create(
            name="New school",
            slug="newschool",
            short_name="New",
            registrant=self.root.profile,
        )
        org.admins.add(self.teacher.profile)
        url = reverse("admin:judge_officialschool_add")
        data = {
            "organization": org.pk,
            "is_active": "on",
            "confirm_roster": "on",
            "_save": "Save",
        }
        first = self.client.post(url, data)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(OfficialSchool.objects.filter(pk=org.pk).exists())
        for field in ("organization", "confirm_roster", "roster_token", "is_active"):
            self.assertNotContains(first, 'name="%s"' % field)

    def test_school_admin_view_on_site_links_to_school_group(self):
        self.client.force_login(self.root)
        response = self.client.get(
            reverse("admin:judge_officialschool_change", args=[self.a.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="%s"' % self.a.get_absolute_url())
        self.assertContains(response, 'class="viewsitelink"')
        response = self.client.get(reverse("admin:judge_officialschool_add"))
        self.assertNotContains(response, 'class="viewsitelink"')

    def test_school_admin_can_pause_enrollment_without_roster_confirmation(self):
        self.client.force_login(self.root)
        self.enroll()
        response = self.client.post(
            reverse("admin:judge_officialschool_change", args=[self.a.pk]),
            self.school_admin_data(self.a, is_active="", organization=self.b.pk),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OfficialSchool.objects.get(pk=self.a.pk).is_active)
        self.assertTrue(OfficialSchool.objects.get(pk=self.b.pk).is_active)
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())

    def test_teacher_cannot_create_school(self):
        with self.assertRaises(PermissionDenied):
            create_school(
                self.teacher,
                name="Denied",
                slug="denied",
                short_name="Denied",
                teachers=[self.teacher.profile],
            )
        self.assertFalse(Organization.objects.filter(slug="denied").exists())

    def test_direct_school_creation_keeps_teachers_out_of_student_roster(self):
        self.client.force_login(self.root)
        response = self.client.post(
            reverse("admin:judge_officialschool_add"),
            {
                "new_name": "New official school",
                "new_slug": "directschool",
                "new_short_name": "Direct",
                "teachers": [self.teacher.profile.pk],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        school = OfficialSchool.objects.get(organization__slug="directschool")
        self.assertTrue(
            school.organization.admins.filter(pk=self.teacher.profile.pk).exists()
        )
        self.assertFalse(school.organization.members.exists())
        self.assertFalse(school.organization.is_open)
        self.assertFalse(school.organization.is_community)
        self.assertTrue(school.is_active)

    def test_anonymous_homepage_and_private_school_discovery(self):
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        response = self.client.get(reverse("organization_list"), {"tab": "private"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.a.slug)

    def test_closed_flags_cannot_be_changed_by_queryset_update(self):
        with self.assertRaises(ValidationError):
            Organization.objects.filter(pk=self.a.pk).update(is_open=True)

    def test_no_membership_requests_and_leave_requires_confirmation(self):
        self.client.force_login(self.student)
        request_url = reverse("request_organization", args=[self.a.pk, self.a.slug])
        self.assertEqual(
            self.client.post(request_url, {"reason": "Join"}).status_code, 404
        )
        self.enroll()
        leave_url = reverse("leave_organization", args=[self.a.pk, self.a.slug])
        self.assertEqual(self.client.post(leave_url).status_code, 400)
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertEqual(
            self.client.post(leave_url, {"confirm": "yes"}).status_code, 302
        )
        self.assertFalse(self.a.members.exists())

    def test_leave_and_block_require_explicit_confirmation_without_a_separate_page(
        self,
    ):
        self.enroll()
        self.client.force_login(self.student)
        leave = reverse("leave_organization", args=[self.a.pk, self.a.slug])
        self.assertRedirects(self.client.get(leave), self.a.get_absolute_url())
        page = self.client.get(self.a.get_absolute_url())
        self.assertContains(page, 'id="school-departure-dialog"')
        self.assertContains(page, 'class="school-departure-form"', count=2)
        for name in ("leave_organization", "block_organization"):
            response = self.client.post(reverse(name, args=[self.a.pk, self.a.slug]))
            self.assertEqual(response.status_code, 400)
            self.assertTemplateUsed(response, "organization/school-notice.html")
        self.assertTrue(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertFalse(Block.is_blocked(self.student.profile, self.a))

    def test_revoked_invitation_and_former_school_have_friendly_private_notices(self):
        self.enroll()
        enroll_school(
            self.student,
            self.b.pk,
            self.student.profile.pk,
            code="abc1234",
            expected_school=self.a.pk,
        )
        self.client.force_login(self.student)
        for path in (
            self.a.get_absolute_url(),
            reverse("join_organization", args=[self.a.pk, self.a.slug])
            + "?code=revoked",
            reverse("join_organization", args=[self.a.pk, self.a.slug])
            + "?code=không-hợp-lệ",
        ):
            response = self.client.get(path, HTTP_ACCEPT_LANGUAGE="en")
            self.assertEqual(response.status_code, 404)
            self.assertTemplateUsed(response, "organization/school-notice.html")
            self.assertNotContains(response, "PRIVATE DESCRIPTION", status_code=404)
            self.assertNotContains(response, "Request Method:", status_code=404)
            self.assertNotContains(response, self.teacher.username, status_code=404)
            self.assertContains(response, "My groups", status_code=404)

    def test_revoked_invitation_post_is_friendly_and_does_not_enroll(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("join_organization", args=[self.a.pk, self.a.slug]),
            {"code": "revoked", "expected_school": "", "confirm": "yes"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "organization/school-notice.html")
        self.assertFalse(self.a.members.exists())

    def test_unified_school_admin_saves_roles_identity_and_enrollment_together(self):
        self.client.force_login(self.root)
        self.enroll(self.b, self.other)
        response = self.client.post(
            reverse("admin:judge_officialschool_change", args=[self.a.pk]),
            self.school_admin_data(
                self.a,
                name="Renamed school",
                admins=[self.other.profile.pk],
                is_active="",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.name, "Renamed school")
        self.assertEqual(list(self.a.admins.all()), [self.other.profile])
        self.assertFalse(self.a.members.exists())
        self.assertTrue(self.b.members.filter(pk=self.other.profile.pk).exists())
        self.assertFalse(OfficialSchool.objects.get(pk=self.a.pk).is_active)
        response = self.client.get(reverse("admin:judge_organization_changelist"))
        self.assertNotIn(
            self.a.pk, response.context["cl"].queryset.values_list("pk", flat=True)
        )

    def test_unified_admin_invalid_changes_do_not_partially_save(self):
        self.client.force_login(self.root)
        url = reverse("admin:judge_officialschool_change", args=[self.a.pk])
        for updates in ({"slug": self.b.slug}, {"admins": []}):
            response = self.client.post(
                url, self.school_admin_data(self.a, is_active="", **updates)
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["adminform"].form.errors)
            self.assertTrue(OfficialSchool.objects.get(pk=self.a.pk).is_active)
            self.assertTrue(self.a.admins.filter(pk=self.teacher.profile.pk).exists())
        old_slug = self.a.slug
        response = self.client.post(
            reverse("admin:judge_organization_change", args=[self.a.pk]),
            {"slug": "bypass"},
        )
        self.assertEqual(response.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.slug, old_slug)

    def test_old_school_admin_link_routes_staff_teachers_without_elevating_permissions(
        self,
    ):
        permission = Permission.objects.get(
            content_type__app_label="judge", codename="change_organization"
        )
        for user in (self.teacher, self.other):
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            user.user_permissions.add(permission)
        old = reverse("admin:judge_organization_change", args=[self.a.pk])
        self.client.force_login(self.teacher)
        self.assertRedirects(
            self.client.get(old),
            reverse("edit_organization", args=[self.a.pk, self.a.slug]),
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:judge_officialschool_change", args=[self.a.pk])
            ).status_code,
            403,
        )
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(old, {"name": "Not allowed"}).status_code, 403
        )
        self.a.refresh_from_db()
        self.assertEqual(self.a.name, "schoola")

    def test_teacher_roles_across_schools_survive_leaving_student_membership(self):
        self.enroll(self.a, self.teacher)
        remove_school_member(self.teacher, self.a.pk, self.teacher.profile.pk)
        self.client.force_login(self.teacher)
        for org in (self.a, self.b):
            self.assertTrue(org.admins.filter(pk=self.teacher.profile.pk).exists())
            self.assertEqual(self.client.get(org.get_absolute_url()).status_code, 200)
        self.assertFalse(self.teacher.profile.organizations.exists())

    def test_admin_replacement_and_role_independence(self):
        form = EditOrganizationForm(
            data={
                "about": "Updated",
                "admins": [self.other.profile.pk],
                "moderators": [],
            },
            instance=self.a,
            profile=self.teacher.profile,
            org_id=self.a.pk,
        )
        self.assertTrue(form.is_valid(), form.errors)
        with transaction.atomic():
            form.save()
        self.assertEqual(list(self.a.admins.all()), [self.other.profile])
        self.assertFalse(self.a.members.exists())

    def test_school_chat_is_opt_in_and_transfer_revokes_old_eligibility(self):
        room_a = create_organization_channel(self.teacher, self.teacher.profile, self.a)
        room_b = create_organization_channel(self.teacher, self.teacher.profile, self.b)
        self.enroll()
        self.assertFalse(
            UserRoom.objects.filter(room=room_a, user=self.student.profile).exists()
        )
        rejoin_organization_channel(room_a, self.student.profile)
        enroll_school(
            self.student,
            self.b.pk,
            self.student.profile.pk,
            code="abc1234",
            expected_school=self.a.pk,
        )
        self.assertFalse(
            UserRoom.objects.filter(
                room=room_a, user=self.student.profile, state=UserRoom.State.ACTIVE
            ).exists()
        )
        self.assertFalse(
            UserRoom.objects.filter(
                room=room_b, user=self.student.profile, state=UserRoom.State.ACTIVE
            ).exists()
        )

    def test_bulk_query_count_is_not_per_student(self):
        people = [self.user("bulkstudent%d" % i) for i in range(8)]
        with CaptureQueriesContext(connection) as small:
            with self.captureOnCommitCallbacks(execute=True):
                bulk_enroll_school(self.teacher, self.a.pk, [people[0].username])
        with CaptureQueriesContext(connection) as larger:
            with self.captureOnCommitCallbacks(execute=True):
                bulk_enroll_school(
                    self.teacher, self.a.pk, [p.username for p in people[1:]]
                )
        self.assertLessEqual(len(larger), len(small) + 3)
        notifications = Notification.objects.filter(
            owner__in=[p.profile for p in people]
        )
        self.assertEqual(notifications.count(), len(people))
        self.assertFalse(
            notifications.exclude(extra_data={"official_school": self.a.pk}).exists()
        )
        self.assertEqual(
            NotificationProfile.objects.filter(
                user__in=[p.profile for p in people], unread_count=1
            ).count(),
            len(people),
        )


class SummaryTests(SchoolFixtures, TestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.contest = Contest.objects.create(
            key="schoolsummary",
            name="School summary contest",
            start_time=now,
            end_time=now + timedelta(hours=2),
            is_visible=False,
            is_private=True,
        )
        self.summary = ContestsSummary.objects.create(
            key="schoolsummary", scores=[10, 5]
        )
        self.summary.contests.add(self.contest)

    def participation(self, user, score=100, **kwargs):
        return ContestParticipation.objects.create(
            contest=self.contest,
            user=user.profile,
            score=score,
            cumtime=0,
            tiebreaker=0,
            **kwargs
        )

    def test_ties_disqualification_virtual_and_score_source(self):
        self.participation(self.student, score_final=999)
        self.participation(self.other, score_final=0)
        self.participation(self.teacher, is_disqualified=True)
        self.participation(self.root, virtual=1)
        result = calculate_summary(self.summary)
        self.assertEqual(result["contest_ids"], [self.contest.pk])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual([row[1]["points"] for row in result["rows"]], [7.5, 7.5])
        self.assertEqual([row[0] for row in result["rows"]], [1, 1])

    def test_filters_preserve_global_ranks_and_all_highlights_without_shortcuts(self):
        self.enroll()
        self.enroll(self.b, self.other)
        self.participation(self.student)
        self.participation(self.other, score=90)
        self.participation(self.teacher, score=80)
        self.summary.results = calculate_summary(self.summary)
        self.summary.save()
        url = reverse("contests_summary", args=[self.summary.key])
        response = self.client.get(
            url,
            {
                "school": self.b.pk,
                "search": self.other.username,
                "highlight": [self.a.pk, "99999999"],
            },
        )
        self.assertEqual(response.context["summary_total"], 3)
        self.assertEqual(response.context["object_list"][0][0], 2)
        self.assertNotContains(response, "Show highlighted only")
        self.assertNotContains(response, "Compare in full ranking")
        response = self.client.get(url, {"highlight": [self.a.pk, self.b.pk]})
        self.assertEqual(len(response.context["object_list"]), 3)
        self.assertEqual(response.context["summary_highlight_count"], 2)
        response = self.client.get(url, {"school": [self.a.pk, self.b.pk]})
        self.assertEqual([row[0] for row in response.context["object_list"]], [1, 2])
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.results["rows"][0][1]["points"], 10)

    def sorting_board(self):
        self.enroll()
        self.enroll(self.b, self.other)
        self.summary.results = {
            "version": 1,
            "contest_ids": [self.contest.pk],
            "rows": [
                [
                    1,
                    {
                        "user_id": self.student.profile.pk,
                        "points": 20,
                        "point_contests": [[2.5, 2]],
                    },
                ],
                [
                    2,
                    {
                        "user_id": self.other.profile.pk,
                        "points": 10,
                        "point_contests": [[8, 1]],
                    },
                ],
                [
                    3,
                    {
                        "user_id": self.teacher.profile.pk,
                        "points": 10,
                        "point_contests": [[2.5, 2]],
                    },
                ],
            ],
        }
        self.summary.save()
        return reverse("contests_summary", args=[self.summary.key])

    def test_sorting_points_contests_rank_and_invalid_input(self):
        url = self.sorting_board()
        for sorting, ranks in (
            ("-points", [1, 2, 3]),
            ("points", [2, 3, 1]),
            ("-contest1", [2, 1, 3]),
            ("contest1", [1, 3, 2]),
            ("rank", [1, 2, 3]),
            ("-rank", [3, 2, 1]),
            ("contest999", [1, 2, 3]),
            ("--points", [1, 2, 3]),
        ):
            with self.subTest(sorting=sorting):
                response = self.client.get(url, {"sort": sorting})
                self.assertEqual(
                    [row[0] for row in response.context["object_list"]], ranks
                )
        self.summary.refresh_from_db()
        self.assertEqual([row[0] for row in self.summary.results["rows"]], [1, 2, 3])

    def test_school_sort_uses_official_names_and_keeps_unknown_last(self):
        url = self.sorting_board()
        for sorting, ranks in (("school", [1, 2, 3]), ("-school", [2, 1, 3])):
            response = self.client.get(url, {"sort": sorting})
            self.assertEqual([row[0] for row in response.context["object_list"]], ranks)

    @patch("judge.views.contests.ContestsSummaryView.paginate_by", 1)
    def test_sorting_before_pagination_and_filter_preserving_links(self):
        url = self.sorting_board()
        response = self.client.get(
            url,
            {
                "sort": "-contest1",
                "school": [self.a.pk, self.b.pk],
                "highlight": self.b.pk,
                "page": 2,
            },
        )
        self.assertEqual(response.context["object_list"][0][0], 1)
        sorting = parse_qs(
            response.context["summary_sort_headers"]["points"]["href"][1:]
        )
        self.assertNotIn("page", sorting)
        self.assertEqual(sorting["sort"], ["-points"])
        self.assertEqual(sorting["school"], [str(self.a.pk), str(self.b.pk)])
        self.assertEqual(sorting["highlight"], [str(self.b.pk)])
        self.assertIn("sort=-contest1", response.context["page_prefix"])
        self.assertEqual(
            response.context["summary_sort_headers"]["contest1"]["direction"],
            "descending",
        )

    def test_numbered_headers_identity_logo_and_fractional_awards(self):
        url = self.sorting_board()
        self.participation(self.student)
        self.participation(self.other)
        self.student.first_name = "Full Student Name"
        self.student.save()
        self.a.organization_image = "organization/school-logo.png"
        self.a.save()
        response = self.client.get(url, HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(
            response, 'href="%s"' % reverse("contest_view", args=[self.contest.key])
        )
        self.assertContains(response, "Full Student Name")
        self.assertContains(response, "organization/school-logo.png")
        self.assertContains(response, 'title="schoola" aria-label="schoola"')
        self.assertContains(response, 'class="fa fa-school summary-school-fallback"')
        self.assertNotContains(response, "<span>schoola</span>")
        self.assertNotContains(response, "user-img")
        self.assertContains(response, ">2.50</a>")
        self.assertContains(response, "(#2)")
        self.assertContains(response, ">8</a>")
        self.assertContains(response, ">20.00</strong>")
        self.assertNotContains(response, 'id="summary-show-names"')

    def test_empty_and_translated_result_counts_render(self):
        url = reverse("contests_summary", args=[self.summary.key])
        for language in ("en", "vi"):
            with override(language):
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["summary_total"], 0)
            self.assertEqual(response.context["summary_highlight_count"], 0)
            self.assertContains(response, 'id="summary-search"')
            self.assertContains(response, 'id="summary-school"')
            self.assertContains(response, 'id="summary-highlight"')

    def test_public_filtered_board_and_links(self):
        self.participation(self.student)
        self.participation(self.other, score=90)
        self.enroll(user=self.other)
        self.summary.results = calculate_summary(self.summary)
        self.summary.save()
        response = self.client.get(
            reverse("contests_summary", args=[self.summary.key]), {"school": self.a.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["object_list"][0][0], 2)
        self.assertContains(
            response,
            reverse("contest_ranking", args=[self.contest.key])
            + "?user="
            + self.other.username,
        )
        self.assertNotContains(response, "/participations/")
        self.assertNotContains(response, self.student.username)

    def test_legacy_totals_no_guessed_columns(self):
        legacy = [
            [
                1,
                {
                    "user_id": self.student.profile.pk,
                    "points": 7.5,
                    "point_contests": [[7.5, 1]],
                },
            ]
        ]
        rows, ids, needs_refresh = read_summary(legacy)
        self.assertEqual(rows, legacy)
        self.assertEqual(ids, [])
        self.assertTrue(needs_refresh)

    def test_compact_legacy_board_has_no_explanations_or_warning(self):
        self.summary.results = [
            [
                1,
                {
                    "user_id": self.student.profile.pk,
                    "points": 7.5,
                    "point_contests": [[7.5, 1]],
                },
            ]
        ]
        self.summary.save()
        response = self.client.get(reverse("contests_summary", args=[self.summary.key]))
        self.assertContains(response, 'class="summary-filter-details"')
        self.assertNotContains(response, 'class="summary-filter-details" open')
        self.assertNotContains(response, "Follow your school.")
        self.assertNotContains(response, "About this ranking")
        self.assertNotContains(response, "administrator refresh to display")
        self.assertNotContains(response, 'class="summary-explanation"')
        self.assertNotContains(response, 'class="summary-notice"')
        self.assertRegex(response.content.decode(), r"7[,.]50")
        self.assertContains(response, 'id="users-table"')

    @patch("judge.views.contests.RANKING_PAGE_SIZE", 1)
    def test_ranking_user_link_locates_the_target_page(self):
        self.participation(self.student)
        self.participation(self.other, score=90)
        self.client.force_login(self.root)
        response = self.client.get(
            reverse("contest_ranking", args=[self.contest.key]),
            {"user": self.other.username},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["highlight_username"], self.other.username)
        self.assertEqual(response.context["page_obj"].number, 2)

    def test_malformed_scores_and_results(self):
        for scores in (None, {}, [True], [float("nan")]):
            with self.assertRaises(ValidationError):
                validate_scores(scores)
        validate_scores([-1, 0, 1])
        self.assertEqual(
            read_summary({"version": 1, "rows": [[1, {}]], "contest_ids": []}),
            ([], [], True),
        )

    def test_admin_refresh_happens_after_m2m(self):
        self.participation(self.student)
        form = type(
            "SummaryForm",
            (),
            {
                "instance": self.summary,
                "save_m2m": lambda form: form.instance.contests.clear(),
            },
        )()
        summary_admin = ContestsSummaryAdmin(ContestsSummary, AdminSite())
        summary_admin.save_related(RequestFactory().post("/"), form, [], False)
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.results["contest_ids"], [])
        self.assertEqual(self.summary.results["rows"], [])

    def test_summary_admin_uses_native_django_actions(self):
        self.client.force_login(self.root)
        response = self.client.get(reverse("admin:judge_contestssummary_changelist"))
        self.assertContains(response, static("admin/js/actions.js"))
        self.assertNotContains(response, ".actions(")

    def test_hidden_identity_cannot_be_recovered_by_search_or_school_filter(self):
        self.enroll()
        self.participation(self.student)
        UsernameModerationCase.objects.create(
            user=self.student,
            username=self.student.username,
            public_identity_hidden=True,
        )
        self.summary.results = calculate_summary(self.summary)
        self.summary.save()
        url = reverse("contests_summary", args=[self.summary.key])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.student.username)
        self.assertNotContains(response, self.a.name)
        self.assertEqual(
            len(
                self.client.get(url, {"search": self.student.username}).context[
                    "object_list"
                ]
            ),
            0,
        )
        self.assertEqual(
            len(self.client.get(url, {"school": self.a.pk}).context["object_list"]), 0
        )

    def test_current_school_changes_without_score_refresh(self):
        self.enroll()
        self.participation(self.student)
        self.summary.results = calculate_summary(self.summary)
        self.summary.save()
        before = self.summary.results
        enroll_school(
            self.student,
            self.b.pk,
            self.student.profile.pk,
            code="abc1234",
            expected_school=self.a.pk,
        )
        response = self.client.get(
            reverse("contests_summary", args=[self.summary.key]), {"school": self.b.pk}
        )
        self.assertEqual(len(response.context["object_list"]), 1)
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.results, before)

    def test_columns_follow_saved_ids_after_contest_selection_changes(self):
        self.participation(self.student)
        self.summary.results = calculate_summary(self.summary)
        self.summary.save()
        self.summary.contests.clear()
        response = self.client.get(reverse("contests_summary", args=[self.summary.key]))
        self.assertEqual(
            [c.pk for c in response.context["contests"]], [self.contest.pk]
        )

    def test_missing_profile_and_contest_render_without_crashing(self):
        self.summary.results = {
            "version": 1,
            "contest_ids": [99999999],
            "rows": [
                [1, {"user_id": 99999999, "points": 5, "point_contests": [[5, 1]]}]
            ],
        }
        self.summary.save()
        response = self.client.get(reverse("contests_summary", args=[self.summary.key]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["contests"], [None])

    def test_filter_precedes_pagination_and_preserves_controls(self):
        users = User.objects.bulk_create(
            [User(username="summarypage%d" % n) for n in range(55)]
        )
        language = Language.objects.first()
        profiles = Profile.objects.bulk_create(
            [Profile(user=user, language=language) for user in users]
        )
        bulk_enroll_school(self.teacher, self.a.pk, [user.username for user in users])
        self.summary.results = {
            "version": 1,
            "contest_ids": [],
            "rows": [
                [n + 10, {"user_id": profile.pk, "points": 1, "point_contests": []}]
                for n, profile in enumerate(profiles)
            ],
        }
        self.summary.save()
        response = self.client.get(
            reverse("contests_summary", args=[self.summary.key]),
            {
                "school": self.a.pk,
                "highlight": self.a.pk,
                "search": "summarypage",
                "page": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["object_list"]), 5)
        self.assertEqual(response.context["object_list"][0][0], 60)
        self.assertEqual(response.context["summary_highlight_count"], 55)
        self.assertIn("school=%s" % self.a.pk, response.context["page_prefix"])
        self.assertIn("search=summarypage", response.context["page_prefix"])
        self.assertContains(response, 'class="summary-highlight"', count=5)


class SchoolConcurrencyTests(SchoolFixtures, TransactionTestCase):
    @patch("judge.services.official_school._notify")
    def test_simultaneous_first_enrollment_has_one_winner(self, notify):
        barrier = Barrier(2)

        def join(org_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.teacher.pk)
                barrier.wait(timeout=10)
                try:
                    enroll_school(actor, org_id, self.student.profile.pk)
                    return "added"
                except ValidationError:
                    return "conflict"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(join, [self.a.pk, self.b.pk]))
        self.assertEqual(sorted(results), ["added", "conflict"])
        self.assertEqual(
            self.student.profile.organizations.filter(
                official_school__isnull=False
            ).count(),
            1,
        )

    @patch("judge.services.official_school._notify")
    def test_opposite_transfers_with_chat_rooms(self, notify):
        create_organization_channel(self.teacher, self.teacher.profile, self.a)
        create_organization_channel(self.teacher, self.teacher.profile, self.b)
        self.enroll(self.a, self.student)
        self.enroll(self.b, self.other)
        barrier = Barrier(2)

        def transfer(args):
            user_id, old_id, new_id = args
            close_old_connections()
            try:
                actor = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                enroll_school(
                    actor,
                    new_id,
                    actor.profile.pk,
                    code="abc1234",
                    expected_school=old_id,
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    transfer,
                    [
                        (self.student.pk, self.a.pk, self.b.pk),
                        (self.other.pk, self.b.pk, self.a.pk),
                    ],
                )
            )
        self.assertTrue(self.b.members.filter(pk=self.student.profile.pk).exists())
        self.assertTrue(self.a.members.filter(pk=self.other.profile.pk).exists())
