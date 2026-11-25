from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from judge.forms import AddOrganizationMemberForm
from judge.models import Block, Notification, Organization, Profile
from judge.services.member_batches import organization_member_plan
from judge.services.official_school import bulk_enroll_school
from judge.tests.test_official_school import SchoolFixtures


class MemberBatchTests(SchoolFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(
            name="Batch organization",
            slug="batch-org",
            short_name="Batch",
            registrant=self.root.profile,
        )
        self.client.force_login(self.root)
        self.client.cookies["django_language"] = "en"

    def url(self, org=None):
        org = org or self.org
        return reverse("add_organization_member", args=[org.pk, org.slug])

    def preview(self, data, org=None):
        response = self.client.post(self.url(org), {**data, "action": "preview"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("member_plan", response.context)
        return response

    def confirm(self, data, preview, org=None):
        return self.client.post(
            self.url(org),
            {
                **data,
                "action": "confirm",
                "confirmation": preview.context["batch_confirmation"],
            },
        )

    def test_select_paste_case_and_duplicate_resolution_without_preview_writes(self):
        data = {
            "users": [self.student.profile.pk],
            "new_users": "SCHOOLSTUDENT, schoolother\nschoolother",
        }
        before = Notification.objects.count()
        preview = self.preview(data)
        self.assertEqual(
            preview.context["member_plan"]["added"],
            [self.student.username, self.other.username],
        )
        self.assertFalse(self.org.members.exists())
        self.assertEqual(Notification.objects.count(), before)
        with patch.object(Notification.objects, "_find_similar_notification") as dedup:
            self.assertEqual(self.confirm(data, preview).status_code, 302)
        dedup.assert_not_called()
        self.assertEqual(
            set(self.org.members.values_list("pk", flat=True)),
            {self.student.profile.pk, self.other.profile.pk},
        )
        before = Notification.objects.count()
        preview = self.preview(data)
        self.assertFalse(preview.context["member_plan"]["added"])
        self.assertEqual(self.confirm(data, preview).status_code, 302)
        self.assertEqual(Notification.objects.count(), before)

    def test_preview_explains_unknown_block_existing_and_capacity(self):
        self.org.members.add(self.teacher.profile)
        Block.objects.create(
            blocker_type=ContentType.objects.get_for_model(type(self.student.profile)),
            blocker_id=self.student.profile.pk,
            blocked_type=ContentType.objects.get_for_model(Organization),
            blocked_id=self.org.pk,
        )
        self.org.slots = 1
        self.org.save(update_fields=["slots"])
        data = {"new_users": "schoolteacher, schoolstudent, schoolother, missing"}
        plan = self.preview(data).context["member_plan"]
        self.assertEqual(plan["existing"], [self.teacher.username])
        self.assertEqual(plan["blocked"], [self.student.username])
        self.assertEqual(plan["full"], [self.other.username])
        self.assertEqual(plan["unknown"], ["missing"])
        self.assertFalse(plan["added"])

    def test_confirm_rejects_missing_tampered_expired_and_changed_batches(self):
        data = {"new_users": self.student.username}
        preview = self.preview(data)
        for token in ("", "tampered"):
            response = self.client.post(
                self.url(), {**data, "action": "confirm", "confirmation": token}
            )
            self.assertTrue(response.context["form"].errors)
        response = self.confirm({"new_users": self.other.username}, preview)
        self.assertTrue(response.context["form"].errors)
        with patch("django.core.signing.time.time", return_value=0):
            expired = self.preview(data)
        self.assertTrue(self.confirm(data, expired).context["form"].errors)
        self.org.members.add(self.student.profile)
        self.assertTrue(self.confirm(data, preview).context["form"].errors)
        self.assertFalse(self.org.members.filter(pk=self.other.profile.pk).exists())

    def test_confirmation_bound_to_actor_and_organization_and_current_permissions(self):
        data = {"new_users": self.student.username}
        self.client.force_login(self.teacher)
        preview = self.preview(data, self.a)
        self.assertTrue(self.confirm(data, preview, self.b).context["form"].errors)
        self.client.force_login(self.root)
        self.assertTrue(self.confirm(data, preview, self.a).context["form"].errors)
        self.client.force_login(self.teacher)
        self.a.admins.add(self.root.profile)
        self.a.admins.remove(self.teacher.profile)
        cache.clear()
        self.assertIn(self.confirm(data, preview, self.a).status_code, (403, 404))
        self.assertFalse(self.a.members.exists())

    def test_school_preview_preserves_conflicts_and_confirm_only_enrolls_ready_users(
        self,
    ):
        self.enroll(self.b)
        self.client.force_login(self.teacher)
        data = {"new_users": "SCHOOLSTUDENT, SCHOOLOTHER, missing"}
        preview = self.preview(data, self.a)
        self.assertEqual(
            preview.context["member_plan"]["conflict"], [self.student.username]
        )
        self.assertEqual(preview.context["member_plan"]["added"], [self.other.username])
        self.assertFalse(self.a.members.exists())
        response = self.confirm(data, preview, self.a)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.a.members.filter(pk=self.other.profile.pk).exists())
        self.assertTrue(self.b.members.filter(pk=self.student.profile.pk).exists())
        self.assertFalse(self.a.members.filter(pk=self.student.profile.pk).exists())
        self.assertEqual(
            response.context["form"]["new_users"].value(),
            "missing\n" + self.student.username,
        )

    def test_school_service_dry_run_and_changed_capacity(self):
        plan = bulk_enroll_school(
            self.teacher, self.a.pk, ["SCHOOLOTHER"], preview=True
        )
        self.assertEqual(plan["added"], [self.other.username])
        self.assertFalse(self.a.members.exists())
        with self.assertRaises(ValidationError):
            bulk_enroll_school(
                self.teacher,
                self.a.pk,
                [self.other.username],
                expected={"plan": plan, "profiles": []},
            )
        self.assertFalse(self.a.members.exists())
        data = {"new_users": self.other.username}
        preview = self.preview(data, self.a)
        self.a.slots = 0
        self.a.save(update_fields=["slots"])
        self.assertTrue(self.confirm(data, preview, self.a).context["form"].errors)
        self.assertFalse(self.a.members.exists())

    def test_access_and_batch_limits(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.client.force_login(self.student)
        self.assertIn(self.client.get(self.url()).status_code, (403, 404))
        self.client.force_login(self.teacher)
        self.org.admins.add(self.teacher.profile)
        cache.clear()
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(self.url(self.a)).status_code, 200)
        for data in ({}, {"new_users": " ".join("missing%d" % n for n in range(201))}):
            form = AddOrganizationMemberForm(data, organization=self.org)
            self.assertFalse(form.is_valid())

    def test_lookup_query_count_does_not_grow_per_username(self):
        users = User.objects.bulk_create(
            [User(username="batch_member_%d" % n) for n in range(199)]
        )
        Profile.objects.bulk_create(
            [Profile(user=user, language=self.language) for user in users]
        )
        # Warm content types so the assertion measures the planner itself.
        organization_member_plan(self.org, [self.student.username])
        with CaptureQueriesContext(connection) as small:
            organization_member_plan(self.org, [self.student.username])
        with CaptureQueriesContext(connection) as large:
            organization_member_plan(
                self.org,
                [self.student.username] + [user.username for user in users],
            )
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 4)

    def test_confirmation_rejects_username_reassigned_to_another_account(self):
        data = {"new_users": self.student.username}
        preview = self.preview(data)
        User.objects.filter(pk=self.student.pk).update(username="retired_batch_student")
        self.user(data["new_users"])
        response = self.confirm(data, preview)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(self.org.members.exists())
