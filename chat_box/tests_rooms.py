import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from judge.models import Language, Organization, Profile
from judge.jinja2.gravatar import public_gravatar
from judge.models.notification import (
    Notification,
    NotificationCategory,
    NotificationProfile,
)
from judge.views.select2 import ChatUserSearchSelect2View

from chat_box.admin import ReadOnlyChatAdmin
from chat_box.exceptions import RoomError, RoomNotFound, RoomPermissionDenied
from chat_box.models import (
    ChatModerationLog,
    Ignore,
    Message,
    Room,
    RoomBan,
    RoomInvitation,
    RoomModerationLog,
    RoomMute,
    RoomRedirect,
    UserRoom,
)
from chat_box.policies import RoomPolicy
from chat_box.room_views import room_list_view
from chat_box.services.invitations import (
    get_invitation_token,
    join_from_invitation,
    resolve_invitation,
    revoke_invitation,
    rotate_invitation,
)
from chat_box.services.events import room_event_channel
from chat_box.services.lobby_sync import set_lobby_moderator
from chat_box.services.memberships import (
    activate_membership,
    ban_member,
    leave_room,
    remove_member,
    set_member_role,
    unban_member,
)
from chat_box.services.moderation import mute_member, revoke_room_mute
from chat_box.services.organization_sync import sync_organization_profile
from chat_box.services.rooms import (
    archive_room,
    create_custom_channel,
    create_group,
    create_organization_channel,
    restore_room,
)
from chat_box.utils import get_unread_boxes
from chat_box.views import get_status_context, get_unread_count


def room_avatar_file(name="avatar.png"):
    return SimpleUploadedFile(
        name,
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ),
        content_type="image/png",
    )


class GeneralizedRoomTestCase(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.language = Language.objects.first()
        self.creator = self.make_profile("room_creator")
        self.member = self.make_profile("room_member")
        self.other = self.make_profile("room_other")

    def tearDown(self):
        cache.clear()

    def make_profile(self, username, *, superuser=False):
        if superuser:
            user = User.objects.create_superuser(
                username=username,
                email="%s@example.com" % username,
                password="password123",
            )
        else:
            user = User.objects.create_user(
                username=username,
                email="%s@example.com" % username,
                password="password123",
            )
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"language": self.language},
        )
        return profile

    def create_group(self, name="Test group"):
        with patch(
            "chat_box.services.rooms.can_use_community_features",
            return_value=True,
        ):
            return create_group(self.creator.user, self.creator, name)

    def create_organization(self, name="Test Organization"):
        organization = Organization.objects.create(
            name=name,
            slug=name.lower().replace(" ", "-"),
            short_name=name[:20],
            about="Test organization",
            registrant=self.creator,
        )
        organization.admins.add(self.creator)
        return organization


class ReadOnlyChatAdminTests(TestCase):
    def test_chat_models_cannot_be_mutated_through_django_admin(self):
        model_admin = ReadOnlyChatAdmin(Room, AdminSite())
        request = RequestFactory().get("/admin/chat_box/room/")

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertIsNone(model_admin.actions)

        for model in (
            Room,
            RoomRedirect,
            UserRoom,
            RoomInvitation,
            RoomMute,
            RoomBan,
            RoomModerationLog,
        ):
            self.assertIsInstance(
                django_admin.site._registry[model],
                ReadOnlyChatAdmin,
            )


class RoomModelAndPolicyTests(GeneralizedRoomTestCase):
    def test_lobby_is_persisted_and_every_profile_is_a_member(self):
        lobby = Room.objects.get(singleton_key="lobby")
        self.assertEqual(lobby.room_type, Room.Type.CHANNEL)
        self.assertEqual(lobby.channel_kind, Room.ChannelKind.LOBBY)
        self.assertEqual(lobby.name, "Lobby")
        for profile in (self.creator, self.member, self.other):
            membership = UserRoom.objects.get(room=lobby, user=profile)
            self.assertEqual(membership.state, UserRoom.State.ACTIVE)
            self.assertEqual(membership.role, UserRoom.Role.MEMBER)

    def test_database_rejects_second_lobby_and_duplicate_membership(self):
        lobby = Room.objects.get(singleton_key="lobby")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Room.objects.create(
                    room_type=Room.Type.CHANNEL,
                    channel_kind=Room.ChannelKind.LOBBY,
                    name="Lobby",
                    singleton_key="lobby",
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserRoom.objects.create(room=lobby, user=self.creator)

    def test_database_reserves_singleton_and_relationship_fields(self):
        invalid_rooms = [
            {
                "room_type": Room.Type.CHANNEL,
                "channel_kind": Room.ChannelKind.CUSTOM,
                "name": "Not Lobby",
                "singleton_key": "not-lobby",
            },
            {
                "room_type": Room.Type.CHANNEL,
                "channel_kind": Room.ChannelKind.ORGANIZATION,
                "name": "Missing organization",
            },
            {
                "room_type": Room.Type.GROUP,
                "name": "Invalid participants",
                "direct_user_low": self.creator,
                "direct_user_high": self.member,
            },
        ]
        for fields in invalid_rooms:
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Room.objects.create(**fields)

    def test_dm_is_canonical_and_private_even_from_superuser(self):
        dm = Room.get_or_create_room(self.creator, self.member)
        reverse_dm = Room.get_or_create_room(self.member, self.creator)
        self.assertEqual(dm.id, reverse_dm.id)
        self.assertEqual(dm.name, None)
        outsider = self.make_profile("dm_super", superuser=True)
        policy = RoomPolicy(outsider.user, outsider, dm, None)
        self.assertFalse(policy.can_view())
        self.assertFalse(policy.can_manage())

    def test_superuser_override_is_not_implicit_membership(self):
        group = self.create_group()
        super_profile = self.make_profile("outside_super", superuser=True)
        policy = RoomPolicy(super_profile.user, super_profile, group, None)
        self.assertFalse(policy.can_manage())
        self.assertFalse(policy.can_change_avatar())
        self.assertFalse(policy.can_view())
        self.assertFalse(policy.can_post())

        membership, _ = activate_membership(group, super_profile, self.creator)
        member_policy = RoomPolicy(
            super_profile.user,
            super_profile,
            group,
            membership,
        )
        self.assertTrue(member_policy.can_manage())
        self.assertTrue(member_policy.can_change_avatar())

    def test_room_avatar_policy_excludes_members_dms_and_lobby(self):
        group = self.create_group()
        membership, _ = activate_membership(group, self.member, self.creator)
        self.assertFalse(
            RoomPolicy(
                self.member.user,
                self.member,
                group,
                membership,
            ).can_change_avatar()
        )
        custom = Room.objects.create(
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.CUSTOM,
            name="Avatar channel",
        )
        custom_membership = UserRoom.objects.create(
            room=custom,
            user=self.creator,
            role=UserRoom.Role.ADMIN,
            manual_role=UserRoom.Role.ADMIN,
        )
        self.assertTrue(
            RoomPolicy(
                self.creator.user,
                self.creator,
                custom,
                custom_membership,
            ).can_change_avatar()
        )
        direct = Room.get_or_create_room(self.creator, self.member)
        direct_membership = UserRoom.objects.get(room=direct, user=self.creator)
        self.assertFalse(
            RoomPolicy(
                self.creator.user,
                self.creator,
                direct,
                direct_membership,
            ).can_change_avatar()
        )
        lobby = Room.objects.get(singleton_key="lobby")
        lobby_membership = UserRoom.objects.get(room=lobby, user=self.creator)
        self.assertFalse(
            RoomPolicy(
                self.creator.user,
                self.creator,
                lobby,
                lobby_membership,
            ).can_change_avatar()
        )

    def test_database_rejects_room_avatar_on_direct_room(self):
        direct = Room.get_or_create_room(self.creator, self.member)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Room.objects.filter(id=direct.id).update(
                    avatar="chat_room_images/invalid.png"
                )

    def test_cached_room_metadata_supports_non_direct_rooms_without_queries(self):
        group = self.create_group("Cached group")
        Room.prefetch_room_cache([group.id])
        cached_group = Room(id=group.id)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(cached_group.room_type, Room.Type.GROUP)
            self.assertEqual(cached_group.name, "Cached group")
            self.assertIsNone(cached_group.channel_kind)
        self.assertEqual(len(queries), 0)


class GroupMembershipTests(GeneralizedRoomTestCase):
    def test_group_starts_with_creator_as_only_admin(self):
        group = self.create_group("Named group")
        memberships = list(UserRoom.objects.filter(room=group))
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].user_id, self.creator.id)
        self.assertEqual(memberships[0].role, UserRoom.Role.ADMIN)

    @override_settings(CHAT_GROUP_CREATION_LIMIT=1)
    def test_group_creation_rate_limit_is_configurable(self):
        self.create_group("First")
        with self.assertRaises(RoomError) as raised:
            self.create_group("Second")
        self.assertEqual(raised.exception.code, "group_creation_limited")

    def test_group_creation_locks_creator_rate_limit_ledger(self):
        with CaptureQueriesContext(connection) as queries:
            self.create_group("Serialized creation")
        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"].upper() and "judge_profile" in query["sql"]
                for query in queries.captured_queries
            )
        )

    @patch("chat_box.services.memberships.GROUP_MEMBER_LIMIT", 3)
    def test_group_cap_rejects_fourth_member(self):
        group = self.create_group()
        activate_membership(group, self.member, self.creator)
        activate_membership(group, self.other, self.creator)
        extra = self.make_profile("room_extra")
        with self.assertRaises(RoomError) as raised:
            activate_membership(group, extra, self.creator)
        self.assertEqual(raised.exception.code, "group_full")
        self.assertEqual(
            UserRoom.objects.filter(
                room=group,
                state=UserRoom.State.ACTIVE,
            ).count(),
            3,
        )

    def test_last_admin_cannot_leave_or_demote_until_another_exists(self):
        group = self.create_group()
        activate_membership(group, self.member, self.creator)
        with self.assertRaises(RoomError) as leave_error:
            leave_room(group, self.creator.user, self.creator)
        self.assertEqual(leave_error.exception.code, "last_admin_required")
        with self.assertRaises(RoomError) as demote_error:
            set_member_role(
                group,
                self.creator.user,
                self.creator,
                self.creator,
                UserRoom.Role.MEMBER,
            )
        self.assertEqual(demote_error.exception.code, "last_admin_required")

        set_member_role(
            group,
            self.creator.user,
            self.creator,
            self.member,
            UserRoom.Role.ADMIN,
        )
        set_member_role(
            group,
            self.creator.user,
            self.creator,
            self.creator,
            UserRoom.Role.MEMBER,
        )
        leave_room(group, self.creator.user, self.creator)
        self.assertEqual(
            UserRoom.objects.get(room=group, user=self.creator).state,
            UserRoom.State.LEFT,
        )

    def test_sole_admin_can_archive_and_restore_room(self):
        group = self.create_group()
        archive_room(group, self.creator.user, self.creator, "No longer active")
        group.refresh_from_db()
        self.assertIsNotNone(group.archived_at)
        membership = UserRoom.objects.get(room=group, user=self.creator)
        self.assertTrue(
            RoomPolicy(self.creator.user, self.creator, group, membership).can_view()
        )
        self.assertFalse(
            RoomPolicy(self.creator.user, self.creator, group, membership).can_post()
        )
        restore_room(group, self.creator.user, self.creator)
        group.refresh_from_db()
        self.assertIsNone(group.archived_at)

    def test_deleting_last_admin_archives_but_other_admin_preserves_room(self):
        archived_group = self.create_group("Archive on deletion")
        creator_user_id = self.creator.user_id
        self.creator.user.delete()
        archived_group.refresh_from_db()
        self.assertIsNotNone(archived_group.archived_at)
        self.assertEqual(archived_group.archive_reason, "last_admin_deleted")
        self.assertFalse(User.objects.filter(id=creator_user_id).exists())

        replacement_creator = self.make_profile("replacement_creator")
        with patch(
            "chat_box.services.rooms.can_use_community_features",
            return_value=True,
        ):
            active_group = create_group(
                replacement_creator.user,
                replacement_creator,
                "Keep active",
            )
        activate_membership(
            active_group,
            self.member,
            replacement_creator,
            role=UserRoom.Role.ADMIN,
        )
        replacement_creator.user.delete()
        active_group.refresh_from_db()
        self.assertIsNone(active_group.archived_at)


class RoomConcurrencyTests(TransactionTestCase):
    fixtures = ["language_small"]

    def setUp(self):
        cache.clear()
        self.language = Language.objects.first()
        self.creator = self.make_profile("concurrent_creator")
        self.member = self.make_profile("concurrent_member")
        with patch(
            "chat_box.services.rooms.can_use_community_features",
            return_value=True,
        ):
            self.room = create_group(
                self.creator.user,
                self.creator,
                "Concurrent group",
            )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def make_profile(self, username):
        user = User.objects.create_user(username=username, password="password123")
        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"language": self.language},
        )
        return profile

    @staticmethod
    def run_in_thread(callback):
        close_old_connections()
        try:
            return callback()
        except RoomError as error:
            return error.code
        finally:
            close_old_connections()

    def test_concurrent_joins_cannot_exceed_group_limit(self):
        activate_membership(self.room, self.member, self.creator)
        candidates = [
            self.make_profile("concurrent_candidate_%s" % index) for index in range(2)
        ]

        def join(profile_id):
            def callback():
                room = Room.objects.get(id=self.room.id)
                target = Profile.objects.get(id=profile_id)
                actor = Profile.objects.get(id=self.creator.id)
                activate_membership(room, target, actor, announce=False)
                return "joined"

            return self.run_in_thread(callback)

        with patch("chat_box.services.memberships.GROUP_MEMBER_LIMIT", 3):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(join, [profile.id for profile in candidates])
                )

        self.assertCountEqual(results, ["joined", "group_full"])
        self.assertEqual(
            UserRoom.objects.filter(
                room=self.room,
                state=UserRoom.State.ACTIVE,
            ).count(),
            3,
        )

    def test_concurrent_admin_leaves_retain_one_admin(self):
        activate_membership(
            self.room,
            self.member,
            self.creator,
            role=UserRoom.Role.ADMIN,
            announce=False,
        )

        def leave(profile_id):
            def callback():
                room = Room.objects.get(id=self.room.id)
                actor = Profile.objects.select_related("user").get(id=profile_id)
                leave_room(room, actor.user, actor)
                return "left"

            return self.run_in_thread(callback)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(leave, [self.creator.id, self.member.id]))

        self.assertCountEqual(results, ["left", "last_admin_required"])
        self.assertEqual(
            UserRoom.objects.filter(
                room=self.room,
                state=UserRoom.State.ACTIVE,
                role=UserRoom.Role.ADMIN,
            ).count(),
            1,
        )


class InvitationTests(GeneralizedRoomTestCase):
    def setUp(self):
        super().setUp()
        self.group = self.create_group()

    def test_only_admin_can_access_reusable_invitation(self):
        token = get_invitation_token(
            self.group,
            self.creator.user,
            self.creator,
        )
        activate_membership(self.group, self.member, self.creator)
        with self.assertRaises(RoomPermissionDenied):
            get_invitation_token(self.group, self.member.user, self.member)
        room_one, _ = join_from_invitation(token, self.other)
        extra = self.make_profile("invite_extra")
        room_two, _ = join_from_invitation(token, extra)
        self.assertEqual(room_one.id, self.group.id)
        self.assertEqual(room_two.id, self.group.id)

    def test_invitation_tokens_are_short_and_resolve(self):
        token = get_invitation_token(
            self.group,
            self.creator.user,
            self.creator,
        )
        self.assertRegex(token, r"^[0-9a-z]+\.[A-Za-z0-9_-]{16}$")
        self.assertEqual(resolve_invitation(token).room_id, self.group.id)

    def test_rotation_and_revocation_invalidate_old_tokens(self):
        old_token = get_invitation_token(
            self.group,
            self.creator.user,
            self.creator,
        )
        new_token = rotate_invitation(
            self.group,
            self.creator.user,
            self.creator,
        )
        with self.assertRaises(RoomNotFound):
            resolve_invitation(old_token)
        self.assertEqual(resolve_invitation(new_token).room_id, self.group.id)
        revoke_invitation(self.group, self.creator.user, self.creator)
        with self.assertRaises(RoomNotFound):
            resolve_invitation(new_token)
        self.assertIsNone(
            get_invitation_token(
                self.group,
                self.creator.user,
                self.creator,
            )
        )

    def test_removed_user_can_rejoin_but_banned_user_cannot(self):
        token = get_invitation_token(
            self.group,
            self.creator.user,
            self.creator,
        )
        join_from_invitation(token, self.member)
        ban_member(
            self.group,
            self.creator.user,
            self.creator,
            self.member,
            "Repeated disruption",
        )
        with self.assertRaises(RoomPermissionDenied):
            join_from_invitation(token, self.member)

    def test_invitation_route_returns_active_and_revoked_states(self):
        self.client.force_login(self.creator.user)
        url = reverse("chat_room_invitation", args=[self.group.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

        response = self.client.post(url, {"action": "revoke"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["revoked"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"revoked": True})


class RoomModerationTests(GeneralizedRoomTestCase):
    def setUp(self):
        super().setUp()
        self.group = self.create_group()
        activate_membership(self.group, self.member, self.creator)
        activate_membership(self.group, self.other, self.creator)
        set_member_role(
            self.group,
            self.creator.user,
            self.creator,
            self.member,
            UserRoom.Role.MODERATOR,
        )

    def test_moderator_can_mute_member_but_not_admin_or_moderator(self):
        mute = mute_member(
            self.group,
            self.member.user,
            self.member,
            self.other,
            "Slow down",
        )
        self.assertEqual(mute.duration_days, 1)
        notification = Notification.objects.filter(owner=self.other).latest("id")
        self.assertEqual(notification.category, NotificationCategory.CHAT_MUTE)
        self.assertEqual(notification.extra_data["type"], "room_mute_notice")
        self.assertEqual(notification.extra_data["room_name"], self.group.name)
        with self.assertRaises(RoomPermissionDenied):
            mute_member(
                self.group,
                self.member.user,
                self.member,
                self.creator,
                "Not allowed",
            )
        second_mod = self.make_profile("second_mod")
        activate_membership(self.group, second_mod, self.creator)
        set_member_role(
            self.group,
            self.creator.user,
            self.creator,
            second_mod,
            UserRoom.Role.MODERATOR,
        )
        with self.assertRaises(RoomPermissionDenied):
            mute_member(
                self.group,
                self.member.user,
                self.member,
                second_mod,
                "Not allowed",
            )

    def test_role_and_block_changes_use_membership_notifications(self):
        Notification.objects.filter(owner=self.other).delete()
        set_member_role(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
            UserRoom.Role.MODERATOR,
        )
        role_notification = Notification.objects.get(owner=self.other)
        self.assertEqual(
            role_notification.category,
            NotificationCategory.ROOM_MEMBERSHIP,
        )
        self.assertEqual(role_notification.extra_data["type"], "room_role_changed")
        self.assertEqual(role_notification.extra_data["role"], UserRoom.Role.MODERATOR)

        Notification.objects.filter(owner=self.other).delete()
        ban_member(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
            "Repeated disruption",
        )
        blocked_notification = Notification.objects.get(owner=self.other)
        self.assertEqual(
            blocked_notification.category,
            NotificationCategory.ROOM_MEMBERSHIP,
        )
        self.assertEqual(blocked_notification.extra_data["type"], "room_blocked")
        self.assertNotIn('href="', blocked_notification.html_link)

        Notification.objects.filter(owner=self.other).delete()
        unban_member(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
        )
        unblocked_notification = Notification.objects.get(owner=self.other)
        self.assertEqual(
            unblocked_notification.category,
            NotificationCategory.ROOM_MEMBERSHIP,
        )
        self.assertEqual(
            unblocked_notification.extra_data["type"],
            "room_unblocked",
        )
        self.assertNotIn('href="', unblocked_notification.html_link)
        self.client.force_login(self.other.user)
        response = self.client.get(
            "/notifications/",
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertContains(response, "Room membership")
        self.assertContains(
            response,
            "You were unblocked from %s." % self.group.name,
        )

    def test_mute_escalation_is_room_scoped_and_admin_can_revoke(self):
        first = mute_member(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
            "First",
        )
        revoke_room_mute(
            self.group,
            self.creator.user,
            self.creator,
            first.id,
        )
        second = mute_member(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
            "Second",
        )
        self.assertEqual(second.duration_days, 2)

        another_group = self.create_group("Another group")
        activate_membership(another_group, self.other, self.creator)
        independent = mute_member(
            another_group,
            self.creator.user,
            self.creator,
            self.other,
            "Independent",
        )
        self.assertEqual(independent.duration_days, 1)

    def test_moderation_api_returns_actionable_state_without_audit_history(self):
        mute_member(
            self.group,
            self.creator.user,
            self.creator,
            self.other,
            "Slow down",
        )
        self.client.force_login(self.creator.user)
        url = reverse("chat_room_moderation", args=[self.group.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["mutes"]), 1)
        self.assertEqual(payload["mutes"][0]["target_name"], self.other.get_username())
        self.assertEqual(
            payload["mutes"][0]["target_url"], self.other.get_absolute_url()
        )
        self.assertEqual(payload["mutes"][0]["target_css_class"], self.other.css_class)
        self.assertEqual(payload["mutes"][0]["reason"], "Slow down")
        self.assertTrue(payload["mutes"][0]["expires_at"])
        self.assertNotIn("logs", payload)


class OrganizationChannelTests(GeneralizedRoomTestCase):
    def setUp(self):
        super().setUp()
        self.organization = self.create_organization()
        self.member.organizations.add(self.organization)
        self.organization.moderators.add(self.other)
        self.room = create_organization_channel(
            self.creator.user,
            self.creator,
            self.organization,
        )

    def test_roles_follow_organization_without_changing_org_permissions(self):
        self.assertEqual(
            UserRoom.objects.get(room=self.room, user=self.creator).role,
            UserRoom.Role.ADMIN,
        )
        self.assertEqual(
            UserRoom.objects.get(room=self.room, user=self.other).role,
            UserRoom.Role.MODERATOR,
        )
        set_member_role(
            self.room,
            self.creator.user,
            self.creator,
            self.member,
            UserRoom.Role.ADMIN,
        )
        self.assertEqual(
            UserRoom.objects.get(room=self.room, user=self.member).role,
            UserRoom.Role.ADMIN,
        )
        self.assertFalse(self.organization.admins.filter(id=self.member.id).exists())

    def test_organization_role_is_an_effective_role_floor(self):
        self.client.force_login(self.creator.user)

        response = self.client.post(
            reverse("chat_room_member_action", args=[self.room.id]),
            {
                "action": "role",
                "user_id": self.other.id,
                "role": UserRoom.Role.MEMBER,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "role": UserRoom.Role.MODERATOR,
                "manual_role": UserRoom.Role.MEMBER,
                "synced_role": UserRoom.Role.MODERATOR,
            },
        )
        details = self.client.get(
            reverse("chat_room_details", args=[self.room.id])
        ).json()
        member = next(
            member for member in details["members"] if member["id"] == self.other.id
        )
        self.assertEqual(member["role"], UserRoom.Role.MODERATOR)
        self.assertEqual(member["synced_role"], UserRoom.Role.MODERATOR)

    def test_unchanged_organization_sync_does_not_rewrite_membership(self):
        with CaptureQueriesContext(connection) as queries:
            membership = sync_organization_profile(
                self.organization,
                self.member.id,
            )

        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertFalse(
            any(
                query["sql"]
                .replace("`", "")
                .replace('"', "")
                .lstrip()
                .upper()
                .startswith("UPDATE CHAT_BOX_USERROOM")
                for query in queries
            )
        )

    def test_organization_avatar_is_the_channel_fallback(self):
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            with patch("chat_box.signals.broadcast_room_event") as broadcast:
                with self.captureOnCommitCallbacks(execute=True):
                    self.organization.organization_image = room_avatar_file(
                        "organization-avatar.png"
                    )
                    self.organization.save(update_fields=["organization_image"])

            organization_avatar_url = self.organization.organization_image.url
            self.assertEqual(
                Room(id=self.room.id).get_avatar_url(), organization_avatar_url
            )
            broadcast.assert_called_once_with(
                self.room.id,
                {
                    "type": "room_avatar_changed",
                    "room": self.room.id,
                    "room_type": Room.Type.CHANNEL,
                    "avatar_url": organization_avatar_url,
                },
            )

            self.client.force_login(self.creator.user)
            search = self.client.get(
                reverse("chat_user_search_select2_ajax"),
                {"term": self.organization.name},
            ).json()
            search_room = next(
                result
                for result in search["results"]
                if result.get("id") == "room:%s" % self.room.id
            )
            self.assertEqual(search_room["avatar_url"], organization_avatar_url)

            details = self.client.get(
                reverse("chat_room_details", args=[self.room.id])
            ).json()
            self.assertEqual(details["avatar_url"], organization_avatar_url)
            self.assertFalse(details["has_custom_avatar"])

            response = self.client.post(
                reverse("chat_room_avatar", args=[self.room.id]),
                {"avatar": room_avatar_file("custom-channel-avatar.png")},
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(response.json()["avatar_url"], organization_avatar_url)
            self.assertTrue(response.json()["has_custom_avatar"])

            response = self.client.post(
                reverse("chat_room_avatar", args=[self.room.id]),
                {"remove": "1"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["avatar_url"], organization_avatar_url)
            self.assertFalse(response.json()["has_custom_avatar"])

    def test_site_admin_must_be_organization_member_to_access_channel(self):
        super_profile = self.make_profile("org_support_super", superuser=True)
        self.client.force_login(super_profile.user)

        sync_organization_profile(self.organization, super_profile.id)

        self.assertFalse(
            UserRoom.objects.filter(
                room=self.room,
                user=super_profile,
                state=UserRoom.State.ACTIVE,
            ).exists()
        )
        self.assertEqual(
            self.client.get(reverse("chat", args=[self.room.id])).status_code,
            403,
        )

        super_profile.organizations.add(self.organization)
        membership = UserRoom.objects.get(room=self.room, user=super_profile)
        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertEqual(
            self.client.get(reverse("chat", args=[self.room.id])).status_code,
            200,
        )

    def test_manual_leave_is_sticky_until_org_leave_and_rejoin(self):
        leave_room(self.room, self.member.user, self.member)
        sync_organization_profile(self.organization, self.member.id)
        self.assertEqual(
            UserRoom.objects.get(room=self.room, user=self.member).state,
            UserRoom.State.LEFT,
        )
        self.member.organizations.remove(self.organization)
        self.assertEqual(
            UserRoom.objects.get(room=self.room, user=self.member).state,
            UserRoom.State.INELIGIBLE,
        )
        self.member.organizations.add(self.organization)
        membership = UserRoom.objects.get(room=self.room, user=self.member)
        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertEqual(membership.role, UserRoom.Role.MEMBER)

    def test_manual_leave_can_be_rejoined_from_organization_home(self):
        leave_room(self.room, self.member.user, self.member)
        self.client.force_login(self.member.user)
        organization_url = reverse(
            "organization_home",
            args=[self.organization.id, self.organization.slug],
        )

        response = self.client.get(organization_url)

        join_url = reverse(
            "chat_organization_channel_join",
            args=[self.organization.id],
        )
        self.assertContains(response, 'action="%s"' % join_url)
        response = self.client.post(join_url)

        self.assertRedirects(
            response,
            reverse("chat", args=[self.room.id]),
            fetch_redirect_response=False,
        )
        membership = UserRoom.objects.get(room=self.room, user=self.member)
        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertEqual(membership.role, UserRoom.Role.MEMBER)

    def test_removed_member_can_explicitly_rejoin_organization_channel(self):
        remove_member(
            self.room,
            self.creator.user,
            self.creator,
            self.member,
            "Membership cleanup",
        )
        sync_organization_profile(self.organization, self.member.id)
        membership = UserRoom.objects.get(room=self.room, user=self.member)
        self.assertEqual(membership.state, UserRoom.State.REMOVED)

        notification = Notification.objects.filter(owner=self.member).latest("id")
        self.assertEqual(notification.category, NotificationCategory.ROOM_MEMBERSHIP)
        self.assertIn(self.room.name, notification.html_link)
        self.assertNotIn('href="', notification.html_link)

        self.client.force_login(self.member.user)
        organization_url = reverse(
            "organization_home",
            args=[self.organization.id, self.organization.slug],
        )
        join_url = reverse(
            "chat_organization_channel_join",
            args=[self.organization.id],
        )
        response = self.client.get(organization_url)
        self.assertContains(response, 'action="%s"' % join_url)

        response = self.client.post(join_url)

        self.assertRedirects(
            response,
            reverse("chat", args=[self.room.id]),
            fetch_redirect_response=False,
        )
        membership.refresh_from_db()
        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertEqual(membership.role, UserRoom.Role.MEMBER)

    def test_site_admin_direct_add_clears_eligible_manual_leave(self):
        leave_room(self.room, self.member.user, self.member)
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.creator.user)

        response = self.client.post(
            reverse("chat_room_member_action", args=[self.room.id]),
            {
                "action": "add",
                "user_id": self.member.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "added": 1})
        membership = UserRoom.objects.get(room=self.room, user=self.member)
        self.assertEqual(membership.state, UserRoom.State.ACTIVE)
        self.assertTrue(
            Message.objects.filter(
                room=self.room,
                kind=Message.Kind.SYSTEM,
                system_event=Message.SystemEvent.JOIN,
                event_data__user_id=self.member.id,
            ).exists()
        )

    def test_ban_overrides_organization_sync(self):
        ban_member(
            self.room,
            self.creator.user,
            self.creator,
            self.member,
            "Channel ban",
        )
        sync_organization_profile(self.organization, self.member.id)
        self.assertNotEqual(
            UserRoom.objects.get(room=self.room, user=self.member).state,
            UserRoom.State.ACTIVE,
        )
        self.assertTrue(
            RoomBan.objects.filter(
                room=self.room,
                target=self.member,
                revoked_at__isnull=True,
            ).exists()
        )

    def test_site_admin_direct_add_does_not_override_organization_channel_ban(self):
        ban_member(
            self.room,
            self.creator.user,
            self.creator,
            self.member,
            "Channel block",
        )
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.creator.user)

        response = self.client.post(
            reverse("chat_room_member_action", args=[self.room.id]),
            {
                "action": "add",
                "user_id": self.member.id,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "room_banned")
        self.assertNotEqual(
            UserRoom.objects.get(room=self.room, user=self.member).state,
            UserRoom.State.ACTIVE,
        )

    def test_org_channel_can_temporarily_have_no_admin(self):
        self.organization.admins.remove(self.creator)
        self.creator.organizations.add(self.organization)
        UserRoom.objects.filter(room=self.room).update(manual_role=UserRoom.Role.MEMBER)
        sync_organization_profile(self.organization, self.creator.id)
        self.assertFalse(
            UserRoom.objects.filter(
                room=self.room,
                state=UserRoom.State.ACTIVE,
                role=UserRoom.Role.ADMIN,
            ).exists()
        )

    def test_organization_rename_emits_system_event_and_deletion_archives(self):
        old_name = self.room.name
        self.organization.name = "Renamed Organization"
        self.organization.save(update_fields=["name"])
        self.room.refresh_from_db()
        self.assertEqual(self.room.name, "Renamed Organization")
        rename_event = Message.objects.get(
            room=self.room,
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.RENAME,
        )
        self.assertEqual(rename_event.event_data["old_name"], old_name)
        self.assertEqual(rename_event.event_data["new_name"], self.organization.name)

        organization_id = self.organization.id
        self.organization.delete()
        self.room.refresh_from_db()
        self.assertIsNone(self.room.organization_id)
        self.assertIsNotNone(self.room.archived_at)
        self.assertEqual(self.room.archive_reason, "organization_deleted")
        self.assertEqual(self.room.organization_id_snapshot, organization_id)


class LobbyAndCustomChannelTests(GeneralizedRoomTestCase):
    def test_lobby_roles_follow_superuser_and_manual_moderator_state(self):
        lobby = Room.objects.get(singleton_key="lobby")
        super_profile = self.make_profile("lobby_super", superuser=True)
        set_lobby_moderator(
            super_profile.user,
            super_profile,
            self.member,
            True,
        )
        self.assertEqual(
            UserRoom.objects.get(room=lobby, user=self.member).role,
            UserRoom.Role.MODERATOR,
        )
        notification = Notification.objects.get(owner=self.member)
        self.assertEqual(notification.category, NotificationCategory.ROOM_MEMBERSHIP)
        self.assertEqual(
            notification.extra_data["type"],
            "lobby_moderator_appointed",
        )
        self.assertEqual(notification.author, super_profile)
        # A normal user cannot appoint Lobby moderators.
        with self.assertRaises(RoomPermissionDenied):
            set_lobby_moderator(
                self.creator.user,
                self.creator,
                self.other,
                True,
            )

        membership = UserRoom.objects.get(room=lobby, user=super_profile)
        self.assertEqual(membership.role, UserRoom.Role.ADMIN)
        super_profile.user.is_superuser = False
        super_profile.user.is_staff = False
        super_profile.user.save(update_fields=["is_superuser", "is_staff"])
        membership.refresh_from_db()
        self.assertEqual(membership.role, UserRoom.Role.MEMBER)

    def test_every_lobby_member_can_see_moderators_but_only_superusers_manage(self):
        lobby = Room.objects.get(singleton_key="lobby")
        super_profile = self.make_profile("visible_lobby_super", superuser=True)
        set_lobby_moderator(
            super_profile.user,
            super_profile,
            self.member,
            True,
        )

        self.client.force_login(self.other.user)
        details = self.client.get(reverse("chat_room_details", args=[lobby.id])).json()

        self.assertEqual(
            [member["id"] for member in details["members"]],
            [self.member.id],
        )
        self.assertFalse(details["permissions"]["manage_lobby_moderators"])

    def test_lobby_moderator_search_returns_only_ordinary_active_members(self):
        lobby = Room.objects.get(singleton_key="lobby")
        super_profile = self.make_profile("search_lobby_super", superuser=True)
        set_lobby_moderator(
            super_profile.user,
            super_profile,
            self.member,
            True,
        )
        self.client.force_login(super_profile.user)

        response = self.client.get(
            reverse("chat_member_search"),
            {"term": "room_", "room": lobby.id},
        )

        self.assertEqual(response.status_code, 200)
        result_ids = {int(result["id"]) for result in response.json()["results"]}
        self.assertIn(self.creator.id, result_ids)
        self.assertIn(self.other.id, result_ids)
        self.assertNotIn(self.member.id, result_ids)
        self.assertNotIn(super_profile.id, result_ids)

    def test_revoking_lobby_moderator_sends_membership_notification(self):
        lobby = Room.objects.get(singleton_key="lobby")
        super_profile = self.make_profile("revoke_lobby_super", superuser=True)
        set_lobby_moderator(
            super_profile.user,
            super_profile,
            self.member,
            True,
        )
        Notification.objects.filter(owner=self.member).delete()

        set_lobby_moderator(
            super_profile.user,
            super_profile,
            self.member,
            False,
        )

        self.assertEqual(
            UserRoom.objects.get(room=lobby, user=self.member).role,
            UserRoom.Role.MEMBER,
        )
        notification = Notification.objects.get(owner=self.member)
        self.assertEqual(notification.category, NotificationCategory.ROOM_MEMBERSHIP)
        self.assertEqual(
            notification.extra_data["type"],
            "lobby_moderator_revoked",
        )

    def test_lobby_cannot_be_left(self):
        lobby = Room.objects.get(singleton_key="lobby")
        with self.assertRaises(RoomPermissionDenied):
            leave_room(lobby, self.creator.user, self.creator)

    def test_custom_channel_requires_superuser_and_creator_is_visible_admin(self):
        with self.assertRaises(RoomPermissionDenied):
            create_custom_channel(self.creator.user, self.creator, "Private channel")
        super_profile = self.make_profile("channel_super", superuser=True)
        channel = create_custom_channel(
            super_profile.user,
            super_profile,
            "Private channel",
        )
        membership = UserRoom.objects.get(room=channel, user=super_profile)
        self.assertEqual(membership.role, UserRoom.Role.ADMIN)

    def test_custom_channel_initial_members_share_one_join_event(self):
        super_profile = self.make_profile("bulk_channel_super", superuser=True)
        targets = [self.make_profile("bulk_channel_%s" % index) for index in range(20)]
        with patch(
            "chat_box.services.memberships.broadcast_personal_events"
        ) as broadcast:
            with self.captureOnCommitCallbacks(execute=True):
                with CaptureQueriesContext(connection) as queries:
                    channel = create_custom_channel(
                        super_profile.user,
                        super_profile,
                        "Bulk channel",
                        targets,
                    )
        self.assertLessEqual(len(queries), 25)
        self.assertEqual(
            UserRoom.objects.filter(
                room=channel,
                state=UserRoom.State.ACTIVE,
            ).count(),
            21,
        )
        self.assertEqual(
            RoomModerationLog.objects.filter(
                room=channel,
                action=RoomModerationLog.Action.ADD,
            ).count(),
            20,
        )
        join_event = Message.objects.get(
            room=channel,
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.JOIN,
        )
        self.assertEqual(
            join_event.event_data["usernames"],
            [target.get_username() for target in targets],
        )
        self.assertEqual(
            Notification.objects.filter(
                owner_id__in=[target.id for target in targets],
                extra_data__room_id=channel.id,
                extra_data__room_name=channel.name,
                category=NotificationCategory.ROOM_MEMBERSHIP,
            ).count(),
            20,
        )
        self.assertEqual(
            NotificationProfile.objects.filter(
                user_id__in=[target.id for target in targets],
                unread_count=1,
            ).count(),
            20,
        )
        broadcast.assert_called_once_with(
            [target.id for target in targets],
            {"type": "room_added", "room": channel.id},
        )


class UnreadCursorTests(GeneralizedRoomTestCase):
    def test_lobby_unread_does_not_count_toward_navbar_badge(self):
        lobby = Room.objects.get(singleton_key="lobby")
        message = Message.objects.create(
            room=lobby,
            author=self.creator,
            body="Lobby announcement",
        )
        Room.objects.filter(id=lobby.id).update(
            last_msg_id=message.id,
            last_activity_at=message.time,
        )

        self.assertEqual(get_unread_count(lobby, self.member), 1)
        self.assertEqual(get_unread_boxes(self.member), 0)

    def test_archived_room_unread_does_not_count_toward_navbar_badge(self):
        room = Room.get_or_create_room(self.creator, self.member)
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="Archived unread",
        )
        Room.objects.filter(id=room.id).update(
            archived_at=timezone.now(),
            last_msg_id=message.id,
            last_activity_at=message.time,
        )

        self.assertEqual(get_unread_boxes(self.member), 0)

    def test_unread_uses_cursor_and_hidden_messages_disappear(self):
        room = Room.get_or_create_room(self.creator, self.member)
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="Unread",
        )
        Room.objects.filter(id=room.id).update(
            last_msg_id=message.id,
            last_activity_at=message.time,
        )
        UserRoom.objects.filter(room=room, user=self.creator).update(
            last_read_message_id=message.id
        )
        self.assertEqual(get_unread_count(room, self.member), 1)
        self.assertEqual(get_unread_boxes(self.member), 1)
        message.hidden = True
        message.save(update_fields=["hidden"])
        self.assertEqual(get_unread_count(room, self.member), 0)
        self.assertEqual(get_unread_boxes(self.member), 0)

    def test_a_users_own_messages_are_not_unread(self):
        room = Room.get_or_create_room(self.creator, self.member)
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="My message",
        )
        Room.objects.filter(id=room.id).update(last_msg_id=message.id)
        self.assertEqual(get_unread_count(room, self.creator), 0)
        self.assertEqual(get_unread_boxes(self.creator), 0)

    def test_unread_count_is_capped_for_large_backlogs(self):
        room = Room.get_or_create_room(self.creator, self.member)
        Message.objects.bulk_create(
            [
                Message(room=room, author=self.creator, body="Unread %s" % index)
                for index in range(105)
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            count = get_unread_count(room, self.member)

        self.assertEqual(count, 100)
        message_queries = [
            query["sql"]
            for query in queries
            if "CHAT_BOX_MESSAGE" in query["sql"].upper()
        ]
        self.assertEqual(len(message_queries), 1)
        if connection.features.supports_slicing_ordering_in_compound:
            self.assertIn("LIMIT 100", message_queries[0].upper())

    def test_channel_burst_limit_returns_retry_metadata(self):
        super_profile = self.make_profile("rate_limit_super", superuser=True)
        room = create_custom_channel(
            super_profile.user,
            super_profile,
            "Rate-limited channel",
        )
        Message.objects.bulk_create(
            [
                Message(room=room, author=super_profile, body="Burst %s" % index)
                for index in range(10)
            ]
        )
        self.client.force_login(super_profile.user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                reverse("post_chat_message"),
                {"room": room.id, "body": "One too many"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "message_rate_limited")
        self.assertGreaterEqual(response.json()["retry_after"], 1)
        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"].upper()
                and "chat_box_userroom" in query["sql"]
                for query in queries.captured_queries
            )
        )


class ReconciliationCommandTests(GeneralizedRoomTestCase):
    def test_preflight_snapshot_is_accepted_by_post_migration_verifier(self):
        with TemporaryDirectory() as temporary_directory:
            snapshot_path = "%s/chat-snapshot.json" % temporary_directory
            call_command(
                "chat_room_preflight",
                "--snapshot",
                snapshot_path,
                stdout=StringIO(),
                stderr=StringIO(),
            )
            output = StringIO()
            call_command(
                "chat_room_verify",
                "--snapshot",
                snapshot_path,
                stdout=output,
                stderr=StringIO(),
            )

        self.assertIn("Verification passed.", output.getvalue())

    def test_post_migration_verification_passes_for_valid_data(self):
        output = StringIO()

        call_command("chat_room_verify", stdout=output, stderr=StringIO())

        self.assertIn("Legacy roomless messages: 0", output.getvalue())
        self.assertIn("Verification passed.", output.getvalue())

    def test_preflight_ignores_generalized_non_direct_rooms(self):
        group = self.create_group("Preflight group")
        activate_membership(group, self.member, self.creator)
        activate_membership(group, self.other, self.creator)
        output = StringIO()

        call_command("chat_room_preflight", stdout=output, stderr=StringIO())

        self.assertIn("Direct rooms:", output.getvalue())
        self.assertIn("Preflight passed.", output.getvalue())

    def test_sync_command_dry_run_repairs_and_is_idempotent(self):
        lobby = Room.objects.get(singleton_key="lobby")
        UserRoom.objects.filter(room=lobby, user=self.member).delete()
        organization = self.create_organization("Sync Organization")
        self.member.organizations.add(organization)
        room = create_organization_channel(
            self.creator.user,
            self.creator,
            organization,
        )
        UserRoom.objects.filter(room=room, user=self.member).update(
            role=UserRoom.Role.ADMIN,
            synced_role=UserRoom.Role.ADMIN,
        )

        output = StringIO()
        call_command(
            "sync_chat_rooms",
            "--lobby",
            "--organizations",
            "--dry-run",
            stdout=output,
        )
        self.assertIn("missing=1", output.getvalue())
        self.assertFalse(UserRoom.objects.filter(room=lobby, user=self.member).exists())
        self.assertEqual(
            UserRoom.objects.get(room=room, user=self.member).role,
            UserRoom.Role.ADMIN,
        )

        output = StringIO()
        call_command(
            "sync_chat_rooms",
            "--lobby",
            "--organizations",
            stdout=output,
        )
        self.assertTrue(UserRoom.objects.filter(room=lobby, user=self.member).exists())
        membership = UserRoom.objects.get(room=room, user=self.member)
        self.assertEqual(membership.role, UserRoom.Role.MEMBER)
        self.assertEqual(membership.synced_role, UserRoom.Role.MEMBER)

        output = StringIO()
        call_command(
            "sync_chat_rooms",
            "--lobby",
            "--organizations",
            "--dry-run",
            stdout=output,
        )
        self.assertIn("missing=0", output.getvalue())
        self.assertIn("stale_roles=0", output.getvalue())

    def test_lobby_repair_query_count_is_bounded_by_batch_not_users(self):
        targets = [self.make_profile("sync_lobby_%s" % index) for index in range(20)]
        lobby = Room.objects.get(singleton_key="lobby")
        UserRoom.objects.filter(
            room=lobby,
            user_id__in=[target.id for target in targets],
        ).delete()
        with CaptureQueriesContext(connection) as queries:
            call_command("sync_chat_rooms", "--lobby", stdout=StringIO())
        self.assertLessEqual(len(queries), 12)


class RoomRouteAndListTests(GeneralizedRoomTestCase):
    def test_legacy_room_url_redirects_to_canonical_room(self):
        room = self.create_group("Canonical room")
        RoomRedirect.objects.create(old_room_id=987654321, canonical_room=room)
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("chat", args=[987654321]))
        self.assertRedirects(
            response,
            reverse("chat", args=[room.id]),
            fetch_redirect_response=False,
        )

        response = self.client.get(
            reverse("chat", args=[987654321]),
            {"switch_room": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["room"]["id"], room.id)

    def test_room_json_routes_redirect_anonymous_users_to_login(self):
        response = self.client.get(reverse("chat_room_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.assertIn("next=", response.url)

    def test_group_details_render_as_toggleable_sidebar(self):
        room = self.create_group("Sidebar details")
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("chat", args=[room.id]))

        self.assertContains(response, 'id="chat-details-panel"')
        self.assertContains(response, 'aria-controls="chat-details-panel"')
        self.assertContains(response, 'aria-expanded="false"')
        self.assertContains(response, 'class="chat-room-details-button"')
        self.assertContains(response, 'class="fa fa-ellipsis-v"')
        self.assertNotContains(response, 'id="chat-details-modal"')
        self.assertContains(response, 'data-room-section="all"')
        self.assertContains(response, 'data-room-filter="all"')
        self.assertContains(response, 'data-room-filter="conversations"')
        self.assertContains(response, 'data-room-filter="channels"')

    def test_large_room_member_search_queries_beyond_initial_slice(self):
        room = Room.objects.create(
            room_type=Room.Type.CHANNEL,
            channel_kind=Room.ChannelKind.CUSTOM,
            name="Large member directory",
        )
        UserRoom.objects.create(
            room=room,
            user=self.creator,
            role=UserRoom.Role.ADMIN,
            manual_role=UserRoom.Role.ADMIN,
        )
        users = User.objects.bulk_create(
            [User(username="directory_member_%s" % index) for index in range(100)]
        )
        profiles = Profile.objects.bulk_create(
            [Profile(user=user, language=self.language) for user in users]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=profile,
                    role=UserRoom.Role.MODERATOR,
                    manual_role=UserRoom.Role.MODERATOR,
                )
                for profile in profiles
            ]
        )
        UserRoom.objects.create(
            room=room,
            user=self.member,
            role=UserRoom.Role.MEMBER,
            manual_role=UserRoom.Role.MEMBER,
        )
        self.client.force_login(self.creator.user)
        url = reverse("chat_room_details", args=[room.id])

        initial = self.client.get(url).json()

        self.assertTrue(initial["members_truncated"])
        self.assertEqual(initial["members"][0]["role"], UserRoom.Role.ADMIN)
        self.assertTrue(
            all(
                member["role"] == UserRoom.Role.MODERATOR
                for member in initial["members"][1:]
            )
        )
        self.assertNotIn(
            self.member.username,
            {member["name"] for member in initial["members"]},
        )

        filtered = self.client.get(
            url,
            {"member_search": self.member.username},
        ).json()

        self.assertFalse(filtered["members_truncated"])
        self.assertEqual(
            [(member["name"], member["role"]) for member in filtered["members"]],
            [(self.member.username, UserRoom.Role.MEMBER)],
        )

    def test_lobby_header_keeps_site_avatar(self):
        lobby = Room.objects.get(singleton_key="lobby")
        self.client.force_login(self.creator.user)

        response = self.client.get(
            reverse("chat", args=[lobby.id]),
            {"switch_room": "1"},
        )

        self.assertEqual(response.status_code, 200)
        header_html = response.json()["header_html"]
        self.assertIn('class="info-pic lobby-icon"', header_html)
        self.assertRegex(header_html, r"/static/icons/icon(?:\.[a-f0-9]+)?\.svg")
        self.assertNotIn("fa-hashtag", header_html)

    def test_site_wide_scope_is_limited_to_superusers_in_channels(self):
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        lobby = Room.objects.get(singleton_key="lobby")
        lobby_message = Message.objects.create(
            room=lobby,
            author=self.member,
            body="Lobby moderation target",
        )
        self.client.force_login(self.creator.user)

        lobby_response = self.client.get(reverse("chat", args=[lobby.id]))

        self.assertContains(lobby_response, 'data-can-site-wide="1"')
        self.assertContains(lobby_response, 'class="chat_mute"', count=1)

        lobby_history_response = self.client.get(
            reverse("chat", args=[lobby.id]),
            {"only_messages": "1", "last_id": lobby_message.id + 1},
        )

        self.assertContains(lobby_history_response, 'data-can-site-wide="1"')

        group = self.create_group("Private moderation context")
        activate_membership(group, self.member, self.creator)
        Message.objects.create(
            room=group,
            author=self.member,
            body="Group moderation target",
        )

        group_response = self.client.get(reverse("chat", args=[group.id]))

        self.assertContains(group_response, 'data-can-site-wide="0"')
        self.assertNotContains(group_response, 'data-can-site-wide="1"')

    def test_internal_legacy_moderation_is_limited_to_active_rooms(self):
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        lobby = Room.objects.get(singleton_key="lobby")
        visible_message = Message.objects.create(
            room=lobby,
            author=self.member,
            body="Visible moderation case",
        )
        visible_log = ChatModerationLog.log_action(
            message=visible_message,
            action="hide",
        )
        private_room = Room.objects.create(
            room_type=Room.Type.GROUP,
            name="Private moderation room",
        )
        UserRoom.objects.create(room=private_room, user=self.member)
        private_message = Message.objects.create(
            room=private_room,
            author=self.member,
            body="Private moderation case",
        )
        private_log = ChatModerationLog.log_action(
            message=private_message,
            action="hide",
        )
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("internal_chat_moderation"))

        self.assertContains(response, visible_message.body)
        self.assertNotContains(response, private_message.body)

        response = self.client.post(
            reverse("internal_chat_moderation"),
            {"log": private_log.id, "action": "keep"},
        )

        self.assertEqual(response.status_code, 404)
        private_log.refresh_from_db()
        self.assertEqual(private_log.action, "hide")
        visible_log.refresh_from_db()
        self.assertEqual(visible_log.action, "hide")

    def test_internal_legacy_hide_supports_accessible_custom_channels(self):
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        channel = create_custom_channel(
            self.creator.user,
            self.creator,
            "Moderated custom channel",
        )
        activate_membership(channel, self.member, self.creator)
        message = Message.objects.create(
            room=channel,
            author=self.member,
            body="Custom channel moderation case",
        )
        log = ChatModerationLog.log_action(message=message, action="review")
        self.client.force_login(self.creator.user)

        response = self.client.post(
            reverse("internal_chat_moderation"),
            {"log": log.id, "action": "hide"},
        )

        self.assertEqual(response.status_code, 302)
        message.refresh_from_db()
        log.refresh_from_db()
        self.assertTrue(message.hidden)
        self.assertEqual(log.action, "hide")

    def test_group_creation_route_opens_new_creator_only_room(self):
        older_rooms = Room.objects.bulk_create(
            [
                Room(
                    room_type=Room.Type.GROUP,
                    name="Older group %s" % index,
                    last_activity_at=timezone.now() - timedelta(days=1),
                )
                for index in range(10)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in older_rooms
            ]
        )
        self.client.force_login(self.creator.user)
        with patch(
            "chat_box.services.rooms.can_use_community_features",
            return_value=True,
        ):
            response = self.client.post(
                reverse("chat_group_create"),
                {"name": "Created through API"},
            )
        self.assertEqual(response.status_code, 200)
        room = Room.objects.get(id=response.json()["room"])
        self.assertEqual(room.room_type, Room.Type.GROUP)
        self.assertIsNotNone(room.last_activity_at)
        self.assertEqual(
            list(UserRoom.objects.filter(room=room).values_list("user_id", "role")),
            [(self.creator.id, UserRoom.Role.ADMIN)],
        )
        conversations = get_status_context(self.creator)[0]["room_list"]
        self.assertEqual(conversations[0]["room"], room.id)

    def test_superuser_can_create_group_with_initial_members(self):
        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        self.client.force_login(self.creator.user)

        response = self.client.post(
            reverse("chat_group_create"),
            {
                "name": "Group with initial members",
                "member_ids": [self.member.id, self.other.id],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(
                UserRoom.objects.filter(room_id=response.json()["room"]).values_list(
                    "user_id", flat=True
                )
            ),
            {self.creator.id, self.member.id, self.other.id},
        )
        join_event = Message.objects.get(
            room_id=response.json()["room"],
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.JOIN,
        )
        self.assertEqual(
            join_event.event_data["usernames"],
            [self.member.get_username(), self.other.get_username()],
        )

    def test_channel_options_are_searchable_and_follow_creation_permissions(self):
        administered = self.create_organization("Administered Search Organization")
        external = Organization.objects.create(
            name="External Search Organization",
            slug="external-search-organization",
            short_name="External Search",
            about="Test organization",
            registrant=self.member,
        )
        external.admins.add(self.member)

        self.client.force_login(self.creator.user)
        response = self.client.get(
            reverse("chat_channel_options"),
            {"term": "Search Organization"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["organizations"],
            [{"id": administered.id, "name": administered.name}],
        )

        self.creator.user.is_superuser = True
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_superuser", "is_staff"])
        response = self.client.get(
            reverse("chat_channel_options"),
            {"term": "External Search"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["organizations"], [])
        self.assertTrue(response.json()["can_create_custom"])

        response = self.client.post(
            reverse("chat_channel_create"),
            {
                "channel_kind": Room.ChannelKind.ORGANIZATION,
                "organization_id": external.id,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Room.objects.filter(
                organization=external,
                channel_kind=Room.ChannelKind.ORGANIZATION,
            ).exists()
        )

        create_organization_channel(self.member.user, self.member, external)
        response = self.client.get(
            reverse("chat_channel_options"),
            {"term": "External Search"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["organizations"], [])

    def test_organization_channel_creation_rejects_invalid_organization(self):
        self.client.force_login(self.creator.user)
        url = reverse("chat_channel_create")

        for organization_id in ("invalid", "999999999"):
            response = self.client.post(
                url,
                {
                    "channel_kind": Room.ChannelKind.ORGANIZATION,
                    "organization_id": organization_id,
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "invalid_organization")

    def test_regular_user_cannot_create_group_with_initial_members(self):
        self.client.force_login(self.creator.user)
        with patch(
            "chat_box.services.rooms.can_use_community_features",
            return_value=True,
        ):
            response = self.client.post(
                reverse("chat_group_create"),
                {
                    "name": "Unauthorized initial members",
                    "member_ids": [self.member.id],
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Room.objects.filter(name="Unauthorized initial members").exists()
        )

    def test_chat_search_returns_people_and_only_accessible_named_rooms(self):
        visible = self.create_group("Searchable Team")
        activate_membership(visible, self.member, self.creator)
        hidden_from_member = self.create_group("Searchable Secret")
        self.client.force_login(self.member.user)

        response = self.client.get(
            reverse("chat_user_search_select2_ajax"),
            {"term": "Searchable"},
        )

        self.assertEqual(response.status_code, 200)
        room_results = [
            result
            for result in response.json()["results"]
            if result.get("kind") == "room"
        ]
        self.assertEqual(
            room_results,
            [
                {
                    "text": visible.name,
                    "id": "room:%s" % visible.id,
                    "kind": "room",
                    "room_type": Room.Type.GROUP,
                    "channel_kind": None,
                    "avatar_url": None,
                    "url": reverse("chat", args=[visible.id]),
                }
            ],
        )
        self.assertNotContains(response, hidden_from_member.name)

        response = self.client.get(
            reverse("chat_user_search_select2_ajax"),
            {"term": self.other.username},
        )
        user_result = response.json()["results"][0]
        self.assertEqual(user_result["kind"], "user")
        self.assertEqual(user_result["text"], self.other.username)

    def test_chat_search_query_count_is_constant_on_a_cold_cache(self):
        for index in range(20):
            self.make_profile("search_perf_%s" % index)
        cache.clear()
        request = RequestFactory().get(
            reverse("chat_user_search_select2_ajax"),
            {"term": "search_perf_"},
        )
        request.user = self.creator.user
        request.profile = self.creator

        with CaptureQueriesContext(connection) as queries:
            response = ChatUserSearchSelect2View.as_view()(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(len(payload["results"]), 10)
        self.assertLessEqual(len(queries), 4)

    def test_chat_page_renders_room_unread_badge(self):
        room = self.create_group("Unread room")
        activate_membership(room, self.member, self.creator)
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="Unread room message",
        )
        Room.objects.filter(id=room.id).update(
            last_msg_id=message.id,
            last_activity_at=message.time,
        )

        self.client.force_login(self.member.user)
        response = self.client.get(reverse("chat", args=[""]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="unread-count-room-%s"' % room.id)
        self.assertContains(response, 'aria-label="')

        response = self.client.get(reverse("chat", args=[room.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "chat-system-message")
        self.assertContains(response, "room_member")

    def test_sidebar_room_menu_contains_available_room_actions(self):
        room = self.create_group("Action menu room")
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("chat", args=[room.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-room="%s"' % room.id)
        self.assertContains(response, 'class="fa fa-ellipsis-v"')
        self.assertContains(response, 'data-chat-room-action="hide"')
        self.assertContains(response, 'class="fa fa-eye-slash"')
        self.assertContains(
            response, 'class="red" role="menuitem" data-chat-room-action="leave"'
        )
        self.assertContains(response, 'class="fa fa-sign-out-alt"')
        self.assertContains(
            response, 'class="red" role="menuitem" data-chat-room-action="archive"'
        )
        self.assertContains(response, 'class="fa fa-archive"')
        self.assertContains(response, 'id="chat-archive-room-modal"')
        self.assertContains(response, 'id="chat-archive-room-reason"')
        self.assertContains(response, 'id="chat-archive-room-confirm"')

        details = self.client.get(reverse("chat_room_details", args=[room.id])).json()
        self.assertNotIn("hide", details["permissions"])
        self.assertNotIn("archive", details["permissions"])

    def test_room_switch_returns_messages_header_and_runtime_permissions(self):
        room = self.create_group("Smooth switch room")
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="Smooth switch marker",
        )
        Room.objects.filter(id=room.id).update(
            last_msg_id=message.id,
            last_activity_at=message.time,
        )
        self.creator.user.is_staff = True
        self.creator.user.save(update_fields=["is_staff"])
        self.client.force_login(self.creator.user)

        response = self.client.get(
            reverse("chat", args=[room.id]),
            {"switch_room": "1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["room"]["id"], room.id)
        self.assertEqual(payload["room"]["type"], Room.Type.GROUP)
        self.assertEqual(payload["room"]["max_length"], 5000)
        self.assertEqual(payload["room"]["last_message_id"], message.id)
        self.assertTrue(payload["user"]["can_interact_room"])
        self.assertTrue(payload["user"]["can_moderate_chat"])
        self.assertIn("Smooth switch room", payload["header_html"])
        self.assertIn('id="chat-room-details"', payload["header_html"])
        self.assertIn('class="chat-room-details-button"', payload["header_html"])
        self.assertIn("Smooth switch marker", payload["messages_html"])
        self.assertIn("$body", payload["message_template"])

    def test_direct_room_switch_returns_other_user(self):
        room = Room.get_or_create_room(self.creator, self.member)
        self.client.force_login(self.creator.user)

        response = self.client.get(
            reverse("chat", args=[room.id]),
            {"switch_room": "1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["room"]["type"], Room.Type.DIRECT)
        self.assertEqual(payload["room"]["other_user_id"], self.member.id)
        self.assertIn(self.member.username, payload["header_html"])
        self.assertIn('id="chat-room-details"', payload["header_html"])
        self.assertIn('class="chat-room-details-button"', payload["header_html"])
        self.assertIn('class="fa fa-ellipsis-v"', payload["header_html"])
        self.assertNotIn("fa-ellipsis-h", payload["header_html"])

    def test_group_admin_can_upload_and_remove_room_avatar(self):
        room = self.create_group("Avatar room")
        self.client.force_login(self.creator.user)

        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            with patch("chat_box.services.rooms.broadcast_room_event") as broadcast:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        reverse("chat_room_avatar", args=[room.id]),
                        {"avatar": room_avatar_file()},
                    )

            self.assertEqual(response.status_code, 200)
            avatar_url = response.json()["avatar_url"]
            self.assertTrue(avatar_url)
            room.refresh_from_db()
            self.assertTrue(room.avatar)
            self.assertEqual(room.get_avatar_url(), avatar_url)
            broadcast.assert_called_once_with(
                room.id,
                {
                    "type": "room_avatar_changed",
                    "room": room.id,
                    "room_type": Room.Type.GROUP,
                    "avatar_url": avatar_url,
                },
            )

            details = self.client.get(
                reverse("chat_room_details", args=[room.id])
            ).json()
            self.assertEqual(details["avatar_url"], avatar_url)
            self.assertTrue(details["permissions"]["change_avatar"])
            room_list = self.client.get(reverse("chat_room_list")).json()["rooms"]
            self.assertEqual(
                next(item for item in room_list if item["id"] == room.id)["avatar_url"],
                avatar_url,
            )
            switch = self.client.get(
                reverse("chat", args=[room.id]), {"switch_room": "1"}
            ).json()
            self.assertIn(escape(avatar_url), switch["header_html"])

            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("chat_room_avatar", args=[room.id]),
                    {"remove": "1"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["avatar_url"])
            room.refresh_from_db()
            self.assertFalse(room.avatar)

    def test_room_avatar_requires_an_authorized_supported_room(self):
        group = self.create_group("Restricted avatar")
        activate_membership(group, self.member, self.creator)
        self.client.force_login(self.member.user)
        response = self.client.post(
            reverse("chat_room_avatar", args=[group.id]),
            {"avatar": room_avatar_file()},
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.creator.user)
        direct = Room.get_or_create_room(self.creator, self.member)
        response = self.client.post(
            reverse("chat_room_avatar", args=[direct.id]),
            {"avatar": room_avatar_file()},
        )
        self.assertEqual(response.status_code, 403)

        lobby = Room.objects.get(singleton_key="lobby")
        response = self.client.post(
            reverse("chat_room_avatar", args=[lobby.id]),
            {"avatar": room_avatar_file()},
        )
        self.assertEqual(response.status_code, 403)

    def test_room_avatar_rejects_invalid_image_content(self):
        room = self.create_group("Invalid avatar")
        self.client.force_login(self.creator.user)
        response = self.client.post(
            reverse("chat_room_avatar", args=[room.id]),
            {
                "avatar": SimpleUploadedFile(
                    "not-an-image.png",
                    b"not an image",
                    content_type="image/png",
                )
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_avatar")

        response = self.client.post(
            reverse("chat_room_avatar", args=[room.id]),
            {"avatar": room_avatar_file("unsafe.html")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_avatar")

    def test_direct_room_details_include_ignore_action(self):
        room = Room.get_or_create_room(self.creator, self.member)
        self.client.force_login(self.creator.user)
        url = reverse("chat_room_details", args=[room.id])

        payload = self.client.get(url).json()

        self.assertFalse(payload["ignored"])
        self.assertEqual(
            payload["ignore_url"],
            reverse("toggle_ignore", args=[self.member.id]),
        )

        Ignore.add_ignore(self.creator, self.member)
        self.assertTrue(self.client.get(url).json()["ignored"])

    def test_ignore_requires_post_and_rejects_external_redirects(self):
        self.client.force_login(self.creator.user)
        url = reverse("toggle_ignore", args=[self.member.id])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 405)
        self.assertFalse(Ignore.is_ignored(self.creator, self.member))

        response = self.client.post(
            url,
            {"next": "https://example.com/steal"},
        )

        self.assertRedirects(
            response,
            reverse("chat", args=[""]),
            fetch_redirect_response=False,
        )
        self.assertTrue(Ignore.is_ignored(self.creator, self.member))

    def test_ignore_ajax_returns_safe_redirect_and_new_state(self):
        self.client.force_login(self.creator.user)
        url = reverse("toggle_ignore", args=[self.member.id])

        response = self.client.post(
            url,
            {"next": reverse("chat", args=[""])},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ignored": True, "redirect": reverse("chat", args=[""])},
        )

    def test_ignore_rejects_post_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.creator.user)

        response = client.post(reverse("toggle_ignore", args=[self.member.id]))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Ignore.is_ignored(self.creator, self.member))

    def test_invitation_form_post_joins_and_redirects_to_room(self):
        room = self.create_group("Invitation redirect")
        token = get_invitation_token(room, self.creator.user, self.creator)
        self.client.force_login(self.member.user)

        response = self.client.post(reverse("chat_invitation", args=[token]))

        self.assertRedirects(
            response,
            reverse("chat", args=[room.id]),
            fetch_redirect_response=False,
        )
        self.assertTrue(
            UserRoom.objects.filter(
                room=room,
                user=self.member,
                state=UserRoom.State.ACTIVE,
            ).exists()
        )

    def test_banned_invitation_form_post_renders_unavailable_page(self):
        room = self.create_group("Banned invitation")
        token = get_invitation_token(room, self.creator.user, self.creator)
        activate_membership(room, self.member, self.creator)
        ban_member(
            room,
            self.creator.user,
            self.creator,
            self.member,
            "Repeated disruption",
        )
        self.client.force_login(self.member.user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

        response = self.client.post(reverse("chat_invitation", args=[token]))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertContains(
            response,
            "This invitation is unavailable",
            status_code=403,
        )
        self.assertContains(
            response, "You are blocked from this room.", status_code=403
        )
        self.assertNotContains(response, '"code": "room_banned"', status_code=403)

    def test_invitation_page_has_room_card_and_singular_member_count(self):
        room = self.create_group("Invitation preview")
        token = get_invitation_token(room, self.creator.user, self.creator)
        self.client.force_login(self.member.user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

        response = self.client.get(reverse("chat_invitation", args=[token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invitation preview")
        self.assertContains(response, "1 member")
        self.assertContains(response, "Join room")
        self.assertNotContains(response, "{{ title }}")

    def test_nonmember_cannot_view_private_room_details(self):
        room = self.create_group()
        self.client.force_login(self.other.user)
        response = self.client.get(reverse("chat_room_details", args=[room.id]))
        self.assertEqual(response.status_code, 403)

        response = self.client.get(
            reverse("chat", args=[room.id]),
            {"switch_room": "1"},
        )
        self.assertEqual(response.status_code, 403)

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("chat", args=[room.id]))
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            "You do not have access to this private room.",
            status_code=403,
        )

    @patch("chat_box.services.memberships.broadcast_room_event")
    def test_join_event_includes_updated_member_count(self, broadcast_room_event):
        room = self.create_group("Live member count")

        with self.captureOnCommitCallbacks(execute=True):
            activate_membership(room, self.member, self.creator)

        message_events = [
            call.args[1]
            for call in broadcast_room_event.call_args_list
            if call.args[1].get("type") == "message"
        ]
        self.assertEqual(message_events[-1]["member_count"], 2)

    @patch("chat_box.services.memberships.broadcast_room_event")
    def test_remove_event_includes_updated_member_count(self, broadcast_room_event):
        room = self.create_group("Live removal count")
        activate_membership(room, self.member, self.creator)

        with self.captureOnCommitCallbacks(execute=True):
            remove_member(
                room,
                self.creator.user,
                self.creator,
                self.member,
                "No longer participating",
            )

        removal_events = [
            call.args[1]
            for call in broadcast_room_event.call_args_list
            if call.args[1].get("type") == "member_removed"
        ]
        self.assertEqual(removal_events[-1]["member_count"], 1)

    def test_superuser_without_membership_cannot_access_private_rooms(self):
        group = self.create_group("Private support room")
        marker = Message.objects.create(
            room=group,
            author=self.creator,
            body="Private history marker",
        )
        dm = Room.get_or_create_room(self.creator, self.member)
        Message.objects.create(
            room=dm,
            author=self.creator,
            body="Private direct-message marker",
        )
        super_profile = self.make_profile("outside_room_super", superuser=True)
        self.client.force_login(super_profile.user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

        group_page = self.client.get(reverse("chat", args=[group.id]))
        self.assertEqual(group_page.status_code, 403)
        self.assertNotContains(group_page, marker.body, status_code=403)
        self.assertEqual(
            self.client.get(
                reverse("chat", args=[group.id]),
                {"switch_room": "1"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("chat_room_details", args=[group.id])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("chat_room_member_action", args=[group.id]),
                {"action": "add", "user_id": self.member.id},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("chat_member_search"),
                {"term": "room", "room": group.id},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/chat/room/%s/join/" % group.id).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("chat", args=[dm.id])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("chat_room_details", args=[dm.id])).status_code,
            403,
        )
        self.assertFalse(
            UserRoom.objects.filter(
                user=super_profile,
                room_id__in=(group.id, dm.id),
            ).exists()
        )

    def test_superuser_member_can_manage_but_cannot_see_hidden_history(self):
        room = self.create_group("Visible member administration")
        super_profile = self.make_profile("member_room_super", superuser=True)
        membership, _ = activate_membership(room, super_profile, self.creator)
        visible = Message.objects.create(
            room=room,
            author=self.creator,
            body="Visible member marker",
        )
        hidden = Message.objects.create(
            room=room,
            author=self.member,
            body="Hidden member marker",
            hidden=True,
        )
        self.client.force_login(super_profile.user)

        response = self.client.get(reverse("chat", args=[room.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, visible.body)
        self.assertNotContains(response, hidden.body)
        self.assertTrue(
            RoomPolicy(
                super_profile.user,
                super_profile,
                room,
                membership,
            ).can_manage()
        )
        details = self.client.get(reverse("chat_room_details", args=[room.id]))
        self.assertEqual(details.status_code, 200)
        self.assertTrue(details.json()["permissions"]["manage"])

    def test_superuser_member_can_direct_add_users(self):
        room = self.create_group()
        super_profile = self.make_profile("route_super", superuser=True)
        activate_membership(room, super_profile, self.creator)
        self.client.force_login(super_profile.user)
        response = self.client.post(
            reverse("chat_room_member_action", args=[room.id]),
            {"action": "add", "user_id": self.member.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            UserRoom.objects.filter(
                room=room,
                user=self.member,
                state=UserRoom.State.ACTIVE,
            ).exists()
        )
        join_event = Message.objects.get(
            room=room,
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.JOIN,
            event_data__user_id=self.member.id,
        )
        self.assertEqual(join_event.event_data["username"], self.member.get_username())

    def test_superuser_member_can_bulk_add_members(self):
        room = self.create_group("Bulk-managed room")
        super_profile = self.make_profile("bulk_route_super", superuser=True)
        activate_membership(room, super_profile, self.creator)
        self.client.force_login(super_profile.user)

        response = self.client.post(
            reverse("chat_room_member_action", args=[room.id]),
            {
                "action": "add",
                "member_ids": [self.member.id, self.other.id],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 2)
        self.assertEqual(
            set(
                UserRoom.objects.filter(
                    room=room,
                    state=UserRoom.State.ACTIVE,
                ).values_list("user_id", flat=True)
            ),
            {self.creator.id, super_profile.id, self.member.id, self.other.id},
        )
        join_event = Message.objects.get(
            room=room,
            kind=Message.Kind.SYSTEM,
            system_event=Message.SystemEvent.JOIN,
            event_data__usernames=[
                self.member.get_username(),
                self.other.get_username(),
            ],
        )
        self.assertEqual(
            join_event.event_data["usernames"],
            [self.member.get_username(), self.other.get_username()],
        )

    def test_member_search_excludes_users_already_active_in_room(self):
        room = self.create_group("Search managed room")
        activate_membership(room, self.member, self.creator)
        super_profile = self.make_profile("member_search_super", superuser=True)
        activate_membership(room, super_profile, self.creator)
        self.client.force_login(super_profile.user)

        response = self.client.get(
            reverse("chat_member_search"),
            {"term": "room_", "room": room.id},
        )

        self.assertEqual(response.status_code, 200)
        result_ids = {result["id"] for result in response.json()["results"]}
        self.assertNotIn(self.creator.id, result_ids)
        self.assertNotIn(self.member.id, result_ids)
        self.assertIn(self.other.id, result_ids)

    def test_member_search_includes_user_who_previously_left_room(self):
        room = self.create_group("Search rejoin room")
        activate_membership(room, self.member, self.creator)
        leave_room(room, self.member.user, self.member)
        super_profile = self.make_profile("member_rejoin_super", superuser=True)
        activate_membership(room, super_profile, self.creator)
        self.client.force_login(super_profile.user)

        response = self.client.get(
            reverse("chat_member_search"),
            {"term": self.member.get_username(), "room": room.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            self.member.id,
            {result["id"] for result in response.json()["results"]},
        )

    def test_event_grant_covers_visible_rooms_but_omits_unauthorized_rooms(self):
        current = self.create_group("Current room")
        visible = self.create_group("Visible room")
        outsider = Room.objects.create(
            room_type=Room.Type.GROUP,
            name="Outsider room",
        )
        UserRoom.objects.create(
            room=outsider,
            user=self.other,
            role=UserRoom.Role.ADMIN,
            manual_role=UserRoom.Role.ADMIN,
        )
        self.client.force_login(self.creator.user)
        response = self.client.get(
            reverse("chat_event_grant", args=[current.id]),
            {"room_ids": "%s,%s,%s" % (current.id, visible.id, outsider.id)},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(room_event_channel(current.id), payload["channels"])
        self.assertIn(room_event_channel(visible.id), payload["channels"])
        self.assertNotIn(room_event_channel(outsider.id), payload["channels"])
        encoded = payload["grant"].split(".", 1)[0]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
        self.assertIn(current.id, claims["room_ids"])
        self.assertIn(visible.id, claims["room_ids"])
        self.assertNotIn(outsider.id, claims["room_ids"])
        self.assertGreaterEqual(len(claims["nonce"]), 8)

    def test_event_grant_reserves_current_room_at_subscription_cap(self):
        sidebar_rooms = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Sidebar room %s" % index)
                for index in range(63)
            ],
            batch_size=63,
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.MEMBER,
                    manual_role=UserRoom.Role.MEMBER,
                )
                for room in sidebar_rooms
            ],
            batch_size=63,
        )
        current = self.create_group("Newest current room")
        lobby = Room.objects.get(singleton_key="lobby")
        self.client.force_login(self.creator.user)
        response = self.client.get(
            reverse("chat_event_grant", args=[current.id]),
            {"room_ids": ",".join(str(room.id) for room in sidebar_rooms)},
        )
        self.assertEqual(response.status_code, 200)
        encoded = response.json()["grant"].split(".", 1)[0]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
        self.assertEqual(len(claims["room_ids"]), 63)
        self.assertIn(current.id, claims["room_ids"])
        self.assertIn(lobby.id, claims["room_ids"])

    def test_event_grant_omits_hidden_or_archived_current_room(self):
        hidden = self.create_group("Hidden current room")
        archived = self.create_group("Archived current room")
        UserRoom.objects.filter(room=hidden, user=self.creator).update(is_hidden=True)
        Room.objects.filter(id=archived.id).update(archived_at=timezone.now())
        self.client.force_login(self.creator.user)

        for room in (hidden, archived):
            response = self.client.get(
                reverse("chat_event_grant", args=[room.id]),
                {"room_ids": str(room.id)},
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(room_event_channel(room.id), response.json()["channels"])

    @patch("chat_box.room_views.revoke_room_subscriptions")
    def test_hiding_room_revokes_existing_event_subscriptions(self, revoke):
        room = self.create_group("Revoke hidden room")
        self.client.force_login(self.creator.user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("chat_room_visibility", args=[room.id]),
                {"hidden": "1"},
            )

        self.assertEqual(response.status_code, 200)
        revoke.assert_called_once_with(self.creator.id, room.id)

    def test_default_chat_uses_visible_room_when_lobby_is_hidden(self):
        room = self.create_group("Visible default room")
        lobby = Room.objects.get(singleton_key="lobby")
        UserRoom.objects.filter(room=lobby, user=self.creator).update(is_hidden=True)
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("chat", args=[""]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["room"], room.id)
        self.assertIn(
            room_event_channel(room.id), response.context["chat_event_channels"]
        )
        self.assertNotIn(
            room_event_channel(lobby.id), response.context["chat_event_channels"]
        )

    def test_unhide_route_advances_cursor_to_current_tail(self):
        room = Room.get_or_create_room(self.creator, self.member)
        message = Message.objects.create(
            room=room,
            author=self.creator,
            body="While hidden",
        )
        Room.objects.filter(id=room.id).update(last_msg_id=message.id)
        membership = UserRoom.objects.get(room=room, user=self.member)
        membership.is_hidden = True
        membership.last_read_message_id = None
        membership.save(update_fields=["is_hidden", "last_read_message_id"])
        self.client.force_login(self.member.user)
        response = self.client.post(
            reverse("chat_room_visibility", args=[room.id]),
            {"hidden": "0"},
        )
        self.assertEqual(response.status_code, 200)
        membership.refresh_from_db()
        self.assertFalse(membership.is_hidden)
        self.assertEqual(membership.last_read_message_id, message.id)
        self.assertEqual(get_unread_count(room, self.member), 0)

    def test_room_list_is_keyset_paginated_without_overlap(self):
        rooms = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Group %s" % index)
                for index in range(30)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in rooms
            ]
        )
        self.client.force_login(self.creator.user)
        first = self.client.get(reverse("chat_room_list")).json()
        self.assertEqual(len(first["rooms"]), 20)
        self.assertTrue(first["has_more"])
        seen = set()
        page = first
        while True:
            page_ids = {room["id"] for room in page["rooms"]}
            self.assertLessEqual(len(page_ids), 20)
            self.assertFalse(seen & page_ids)
            seen.update(page_ids)
            if not page["has_more"]:
                break
            page = self.client.get(
                reverse("chat_room_list"),
                {"cursor": page["next_cursor"]},
            ).json()
        self.assertEqual(seen, {room.id for room in rooms})

    def test_recent_rooms_are_unified_and_filters_are_independently_paginated(self):
        channels = Room.objects.bulk_create(
            [
                Room(
                    room_type=Room.Type.CHANNEL,
                    channel_kind=Room.ChannelKind.CUSTOM,
                    name="Channel %s" % index,
                )
                for index in range(21)
            ]
        )
        conversations = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Group %s" % index)
                for index in range(21)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in channels + conversations
            ]
        )

        sections = get_status_context(self.creator)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["key"], "all")
        self.assertEqual(len(sections[0]["room_list"]), 20)
        self.assertTrue(sections[0]["has_more"])
        self.assertTrue(sections[0]["next_cursor"])

        channel_section = get_status_context(self.creator, section="channels")[0]
        conversation_section = get_status_context(
            self.creator, section="conversations"
        )[0]
        self.assertEqual(channel_section["key"], "channels")
        self.assertEqual(conversation_section["key"], "conversations")
        self.assertEqual(len(channel_section["room_list"]), 20)
        self.assertEqual(len(conversation_section["room_list"]), 20)

        self.client.force_login(self.creator.user)
        filtered_status = self.client.get(
            reverse("online_status_ajax"),
            {"section": "channels"},
        )
        self.assertEqual(filtered_status.status_code, 200)
        self.assertContains(filtered_status, 'data-room-section="channels"')
        self.assertContains(
            filtered_status,
            'data-room-filter="channels" aria-pressed="true"',
        )
        self.assertNotContains(filtered_status, "Group 0")

        channel_page = self.client.get(
            reverse("chat_room_list"),
            {
                "section": "channels",
                "cursor": channel_section["next_cursor"],
            },
        ).json()
        conversation_page = self.client.get(
            reverse("chat_room_list"),
            {
                "section": "conversations",
                "cursor": conversation_section["next_cursor"],
            },
        ).json()
        self.assertEqual(len(channel_page["rooms"]), 1)
        self.assertEqual(channel_page["rooms"][0]["room_type"], Room.Type.CHANNEL)
        self.assertEqual(len(conversation_page["rooms"]), 1)
        self.assertIn(
            conversation_page["rooms"][0]["room_type"],
            (Room.Type.DIRECT, Room.Type.GROUP),
        )

    def test_room_list_rejects_invalid_section(self):
        self.client.force_login(self.creator.user)
        response = self.client.get(
            reverse("chat_room_list"),
            {"section": "unknown"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_section")

    def test_hidden_room_list_search_filters_named_rooms_and_direct_handles(self):
        matching_group = self.create_group("Needle planning room")
        other_group = self.create_group("Unrelated room")
        direct = Room.get_or_create_room(self.creator, self.member)
        UserRoom.objects.filter(
            room__in=(matching_group, other_group, direct),
            user=self.creator,
        ).update(is_hidden=True, hidden_at=timezone.now())
        self.client.force_login(self.creator.user)

        named_response = self.client.get(
            reverse("chat_room_list"),
            {"hidden": "1", "search": "Needle"},
        )
        direct_response = self.client.get(
            reverse("chat_room_list"),
            {"hidden": "1", "search": self.member.username},
        )

        self.assertEqual(
            [room["id"] for room in named_response.json()["rooms"]],
            [matching_group.id],
        )
        self.assertTrue(named_response.json()["rooms"][0]["actions"]["unhide"])
        self.assertEqual(
            [room["id"] for room in direct_response.json()["rooms"]],
            [direct.id],
        )

    def test_archived_room_list_exposes_restore_action(self):
        room = self.create_group("Archived action room")
        archive_room(room, self.creator.user, self.creator, "Done")
        self.client.force_login(self.creator.user)

        response = self.client.get(reverse("chat_room_list"), {"archived": "1"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()["rooms"]
        archived_room = next(item for item in payload if item["id"] == room.id)
        self.assertTrue(archived_room["actions"]["restore"])
        self.assertFalse(archived_room["actions"]["archive"])

    def test_archive_route_validates_and_persists_reason(self):
        room = self.create_group("Archive reason room")
        self.client.force_login(self.creator.user)
        url = reverse("chat_room_archive", args=[room.id])

        response = self.client.post(url, {"reason": "Project complete"})

        self.assertEqual(response.status_code, 200)
        room.refresh_from_db()
        self.assertEqual(room.archive_reason, "Project complete")

        restore_room(room, self.creator.user, self.creator)
        response = self.client.post(url, {"reason": "x" * 65})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "archive_reason_too_long")
        room.refresh_from_db()
        self.assertIsNone(room.archived_at)

    def test_paginated_direct_room_includes_real_avatar_metadata(self):
        direct = Room.get_or_create_room(self.creator, self.member)
        Room.objects.filter(id=direct.id).update(
            last_activity_at=timezone.now() - timedelta(days=1)
        )
        groups = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Newer group %s" % index)
                for index in range(20)
            ]
        )
        Room.objects.filter(id__in=[room.id for room in groups]).update(
            last_activity_at=timezone.now()
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in groups
            ]
        )
        self.client.force_login(self.creator.user)
        first = self.client.get(reverse("chat_room_list")).json()

        second = self.client.get(
            reverse("chat_room_list"),
            {"cursor": first["next_cursor"]},
        ).json()

        direct_result = next(
            room for room in second["rooms"] if room["id"] == direct.id
        )
        self.assertEqual(direct_result["other_user_id"], self.member.id)
        self.assertEqual(
            direct_result["avatar_url"],
            public_gravatar(self.member, self.creator.user, 135),
        )
        self.assertIn("is_online", direct_result)

    def test_ignored_direct_room_is_absent_from_every_room_list_page(self):
        ignored_room = Room.get_or_create_room(self.creator, self.member)
        Ignore.add_ignore(self.creator, self.member)
        rooms = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Visible %s" % index)
                for index in range(30)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in rooms
            ]
        )
        self.client.force_login(self.creator.user)
        response = self.client.get(reverse("chat_room_list"))
        seen = {room["id"] for room in response.json()["rooms"]}
        cursor = response.json()["next_cursor"]
        while cursor:
            response = self.client.get(reverse("chat_room_list"), {"cursor": cursor})
            seen.update(room["id"] for room in response.json()["rooms"])
            cursor = response.json()["next_cursor"]
        self.assertNotIn(ignored_room.id, seen)
        self.assertTrue({room.id for room in rooms}.issubset(seen))

    def test_status_list_query_count_does_not_grow_with_room_count(self):
        rooms = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Query group %s" % index)
                for index in range(25)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in rooms
            ]
        )
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            sections = get_status_context(self.creator)
        self.assertEqual(len(sections[0]["room_list"]), 20)
        self.assertLessEqual(len(queries), 8)

    def test_paginated_room_list_query_count_is_constant_on_a_cold_cache(self):
        rooms = Room.objects.bulk_create(
            [
                Room(room_type=Room.Type.GROUP, name="Paged query group %s" % index)
                for index in range(25)
            ]
        )
        UserRoom.objects.bulk_create(
            [
                UserRoom(
                    room=room,
                    user=self.creator,
                    role=UserRoom.Role.ADMIN,
                    manual_role=UserRoom.Role.ADMIN,
                )
                for room in rooms
            ]
        )
        messages = Message.objects.bulk_create(
            [
                Message(room=room, author=self.creator, body="Room preview")
                for room in rooms
            ]
        )
        for room, message in zip(rooms, messages):
            room.last_msg_id = message.id
            room.last_activity_at = message.time
        Room.objects.bulk_update(rooms, ["last_msg_id", "last_activity_at"])
        cache.clear()
        request = RequestFactory().get(reverse("chat_room_list"))
        request.user = self.creator.user
        request.profile = self.creator

        with CaptureQueriesContext(connection) as queries:
            response = room_list_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(json.loads(response.content)["rooms"]), 20)
        self.assertLessEqual(len(queries), 8)

    def test_hidden_room_has_no_unread_badge(self):
        room = Room.get_or_create_room(self.creator, self.member)
        Message.objects.create(room=room, author=self.creator, body="Unread")
        membership = UserRoom.objects.get(room=room, user=self.member)
        membership.is_hidden = True
        membership.hidden_at = timezone.now()
        membership.save(update_fields=["is_hidden", "hidden_at"])
        self.assertEqual(get_unread_count(room, self.member), 0)
        self.assertEqual(get_unread_boxes(self.member), 0)

    def test_internal_moderation_only_exposes_joined_room_activity(self):
        room = self.create_group("Audited group")
        activate_membership(room, self.member, self.creator)
        mute_member(
            room,
            self.creator.user,
            self.creator,
            self.member,
            "Audit reason",
        )
        super_profile = self.make_profile("audit_super", superuser=True)
        self.client.force_login(super_profile.user)

        response = self.client.get(
            reverse("internal_chat_moderation"),
            {"scope": "rooms"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Audited group")
        self.assertNotContains(response, "Audit reason")

        response = self.client.get(
            reverse("internal_chat_moderation"),
            {"scope": "room_mutes", "action": "active"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Audited group")
        self.assertNotContains(response, "Audit reason")

        activate_membership(room, super_profile, self.creator)
        response = self.client.get(
            reverse("internal_chat_moderation"),
            {"scope": "rooms"},
        )
        self.assertContains(response, "Audited group")
        self.assertContains(response, "Audit reason")

        response = self.client.get(
            reverse("internal_chat_moderation"),
            {"scope": "room_mutes", "action": "active"},
        )
        self.assertContains(response, "Audited group")
        self.assertContains(response, "Audit reason")
