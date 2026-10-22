from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from judge.models import AvatarFrame, Profile, ProfileFrame
from judge.tests.test_avatar_frames import AvatarFrameTestCase
from judge.utils.rewards import grant_manual_frame


class ManualFrameGrantTests(AvatarFrameTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("frame_admin", "", "password")
        Profile.objects.get_or_create(
            user=self.admin, defaults={"language": self.language}
        )
        other = User.objects.create_user(username="frame_other")
        self.other, _ = Profile.objects.get_or_create(
            user=other, defaults={"language": self.language}
        )
        self.url = reverse("admin:judge_profile_changelist")
        self.client.force_login(self.admin)
        self.payload = {
            "action": "award_avatar_frame",
            "_selected_action": [str(self.profile.pk)],
            "select_across": "0",
            "frame": self.manual.pk,
            "reason": "Cùng cổ vũ MU",
        }

    def test_admin_action_previews_recipients_without_granting(self):
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["recipient_count"], 1)
        self.assertContains(response, "frame_student")
        self.assertFalse(ProfileFrame.objects.filter(frame=self.manual).exists())

    def test_selected_profiles_receive_frame_with_audit_and_no_auto_equip(self):
        response = self.client.post(self.url, {**self.payload, "apply": "1"})
        self.assertEqual(response.status_code, 302)
        award = ProfileFrame.objects.get(profile=self.profile, frame=self.manual)
        self.assertEqual(award.granted_by, self.admin)
        self.assertEqual(award.reason, "Cùng cổ vũ MU")
        self.assertFalse(award.is_equipped)
        self.assertFalse(ProfileFrame.objects.filter(profile=self.other).exists())

    def test_select_across_awards_every_matching_profile(self):
        response = self.client.post(
            self.url, {**self.payload, "select_across": "1", "apply": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ProfileFrame.objects.filter(frame=self.manual).count(),
            Profile.objects.count(),
        )

    def test_select_across_preserves_admin_search_filter(self):
        response = self.client.post(
            self.url + "?q=frame_student",
            {**self.payload, "select_across": "1", "apply": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(
                ProfileFrame.objects.filter(frame=self.manual).values_list(
                    "profile_id", flat=True
                )
            ),
            [self.profile.pk],
        )

    def test_repeated_grants_preserve_original_history_and_equipment(self):
        existing = ProfileFrame.objects.create(
            profile=self.profile, frame=self.manual, reason="Original", is_equipped=True
        )
        original_time = existing.awarded_at
        for _ in range(2):
            grant_manual_frame(
                self.manual,
                Profile.objects.filter(pk__in=[self.profile.pk, self.other.pk]),
                granted_by=self.admin,
                reason="New campaign",
                batch_size=1,
            )
        existing.refresh_from_db()
        self.assertEqual(existing.reason, "Original")
        self.assertEqual(existing.awarded_at, original_time)
        self.assertIsNone(existing.granted_by)
        self.assertTrue(existing.is_equipped)
        self.assertEqual(ProfileFrame.objects.filter(frame=self.manual).count(), 2)
        self.assertFalse(
            ProfileFrame.objects.get(profile=self.other, frame=self.manual).is_equipped
        )

    def test_course_and_inactive_frames_cannot_be_granted(self):
        self.manual.is_active = False
        self.manual.save()
        for frame in (self.frame, self.manual):
            response = self.client.post(
                self.url, {**self.payload, "frame": frame.pk, "apply": "1"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame", response.context_data["form"].errors)
            with self.assertRaises(ValidationError):
                grant_manual_frame(
                    frame, Profile.objects.all(), granted_by=self.admin, reason="Test"
                )
        self.assertFalse(ProfileFrame.objects.exists())

    def test_staff_without_award_permission_cannot_grant(self):
        self.user.is_staff = True
        self.user.save()
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_profile", content_type__app_label="judge"
            )
        )
        self.client.force_login(self.user)
        self.client.post(self.url, {**self.payload, "apply": "1"})
        self.assertFalse(ProfileFrame.objects.exists())
        with self.assertRaises(PermissionDenied):
            grant_manual_frame(
                self.manual, Profile.objects.all(), granted_by=self.user, reason="Test"
            )

    def test_blank_reason_does_not_grant(self):
        response = self.client.post(
            self.url, {**self.payload, "reason": " ", "apply": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reason", response.context_data["form"].errors)
        self.assertFalse(ProfileFrame.objects.exists())

    def test_united_catalog_entry_has_packaged_art_and_manual_unlock(self):
        frame = AvatarFrame.objects.get(slug="manchester-united")
        self.assertEqual(frame.unlock_type, "manual")
        self.assertFalse(frame.label)
        self.assertTrue(
            frame.artwork_url.endswith("avatar-frames/manchester-united.png")
        )
        self.assertEqual(frame.avatar_diameter, 52)
