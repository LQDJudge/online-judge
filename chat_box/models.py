import os

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import IntegrityError, models, transaction
from django.db.models import CASCADE, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from judge.caching import CacheableModel, cache_wrapper
from judge.models.profile import Organization, Profile
from judge.utils.files import generate_secure_filename

__all__ = [
    "Message",
    "MessageReaction",
    "Room",
    "RoomRedirect",
    "UserRoom",
    "Ignore",
    "ChatModerationLog",
    "RoomInvitation",
    "RoomMute",
    "RoomBan",
    "RoomModerationLog",
]

# Facebook-Messenger-style reactions. Store the CODE; the emoji is presentation only.
CHAT_REACTIONS = [
    ("like", "👍"),
    ("love", "❤️"),
    ("haha", "😆"),
    ("wow", "😮"),
    ("sad", "😢"),
    ("angry", "😠"),
]
CHAT_REACTION_CODES = [code for code, _emoji in CHAT_REACTIONS]
CHAT_REACTION_EMOJI = dict(CHAT_REACTIONS)
# Reactions whose glyph is a static image instead of the text emoji above.
# Maps reaction code -> static path; the display layers render an <img> for these.
CHAT_REACTION_IMAGES = {}
# Translatable human labels (for screen-reader aria-labels / tooltips).
CHAT_REACTION_LABELS = {
    "like": _("Like"),
    "love": _("Love"),
    "haha": _("Haha"),
    "wow": _("Wow"),
    "sad": _("Sad"),
    "angry": _("Angry"),
}


def room_avatar_path(room, filename):
    secure_filename = generate_secure_filename(filename, "room_%s" % room.id)
    return os.path.join(settings.DMOJ_CHAT_ROOM_IMAGE_ROOT, secure_filename)


class Room(CacheableModel):
    class Type(models.TextChoices):
        DIRECT = "direct", _("Direct message")
        GROUP = "group", _("Group")
        CHANNEL = "channel", _("Channel")

    class ChannelKind(models.TextChoices):
        ORGANIZATION = "organization", _("Organization")
        CUSTOM = "custom", _("Custom")
        LOBBY = "lobby", _("Lobby")

    room_type = models.CharField(
        max_length=16,
        choices=Type.choices,
        default=Type.DIRECT,
        db_index=True,
        verbose_name=_("room type"),
    )
    channel_kind = models.CharField(
        max_length=16,
        choices=ChannelKind.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("channel kind"),
    )
    name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        verbose_name=_("room name"),
    )
    avatar = models.ImageField(
        upload_to=room_avatar_path,
        null=True,
        blank=True,
        verbose_name=_("room avatar"),
    )
    organization = models.OneToOneField(
        "judge.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_room",
        verbose_name=_("organization"),
    )
    organization_id_snapshot = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("original organization ID"),
    )
    organization_name_snapshot = models.CharField(
        max_length=128,
        blank=True,
        verbose_name=_("original organization name"),
    )
    direct_user_low = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_rooms_as_low_user",
        verbose_name=_("first direct-message user"),
    )
    direct_user_high = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_rooms_as_high_user",
        verbose_name=_("second direct-message user"),
    )
    singleton_key = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        unique=True,
        verbose_name=_("singleton key"),
    )
    created_at = models.DateTimeField(
        default=timezone.now,
        verbose_name=_("created at"),
    )
    last_activity_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("last activity at"),
    )
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("archived at"),
    )
    archived_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="archived_chat_rooms",
        verbose_name=_("archived by"),
    )
    archive_reason = models.CharField(
        max_length=64,
        blank=True,
        verbose_name=_("archive reason"),
    )
    last_msg_id = models.IntegerField(
        verbose_name=_("last message id"), null=True, db_index=True
    )

    class Meta:
        app_label = "chat_box"
        indexes = [
            models.Index(fields=["room_type", "channel_kind"]),
            models.Index(fields=["archived_at", "last_activity_at"]),
            models.Index(fields=["organization_id_snapshot"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("direct_user_low", "direct_user_high"),
                name="chat_unique_direct_user_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(direct_user_low__isnull=True)
                    | Q(direct_user_high__isnull=True)
                    | Q(direct_user_low__lte=models.F("direct_user_high"))
                ),
                name="chat_direct_pair_canonical_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(room_type="direct", channel_kind__isnull=True)
                    | Q(room_type="group", channel_kind__isnull=True)
                    | Q(room_type="channel", channel_kind__isnull=False)
                ),
                name="chat_valid_room_type_kind",
            ),
            models.CheckConstraint(
                condition=(
                    Q(room_type="direct", name__isnull=True)
                    | (Q(room_type="group", name__isnull=False) & ~Q(name=""))
                    | (Q(room_type="channel", name__isnull=False) & ~Q(name=""))
                ),
                name="chat_valid_room_name",
            ),
            models.CheckConstraint(
                condition=(
                    Q(avatar__isnull=True)
                    | Q(avatar="")
                    | Q(room_type="group")
                    | Q(
                        room_type="channel",
                        channel_kind__in=("organization", "custom"),
                    )
                ),
                name="chat_valid_room_avatar",
            ),
            models.CheckConstraint(
                condition=(
                    Q(channel_kind="lobby", singleton_key="lobby")
                    | (~Q(channel_kind="lobby") & Q(singleton_key__isnull=True))
                ),
                name="chat_valid_lobby_singleton",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        channel_kind="organization",
                        organization__isnull=False,
                    )
                    | Q(
                        channel_kind="organization",
                        organization__isnull=True,
                        archived_at__isnull=False,
                        organization_id_snapshot__isnull=False,
                    )
                    | (~Q(channel_kind="organization") & Q(organization__isnull=True))
                ),
                name="chat_valid_room_organization",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        room_type="direct",
                        direct_user_low__isnull=False,
                        direct_user_high__isnull=False,
                    )
                    | Q(room_type="direct", archived_at__isnull=False)
                    | (
                        ~Q(room_type="direct")
                        & Q(direct_user_low__isnull=True)
                        & Q(direct_user_high__isnull=True)
                    )
                ),
                name="chat_valid_direct_participants",
            ),
        ]

    @classmethod
    def get_cached_dict(cls, room_id):
        return _get_room(room_id)

    @classmethod
    def get_cached_instances(cls, *ids):
        # Prefetch cache data
        cached_results = _get_room.batch([(id,) for id in ids])
        return cls.instances_from_cached_results(
            ids, cached_results, filter_missing=False
        )

    @classmethod
    def dirty_cache(cls, *ids):
        id_list = [(id,) for id in ids]
        _get_room.dirty_multi(id_list)

    def other_user(self, profile):
        """Get the display user for this room from the current user's perspective."""
        other_id = self.other_user_id(profile)
        if other_id:
            return Profile(id=other_id)
        return None

    def other_user_id(self, profile):
        """Get the display user ID for this room from the current user's perspective."""
        if self.room_type == self.Type.DIRECT:
            if self.direct_user_low_id == self.direct_user_high_id == profile.id:
                return profile.id
            if self.direct_user_low_id == profile.id:
                return self.direct_user_high_id
            if self.direct_user_high_id == profile.id:
                return self.direct_user_low_id
            # A deleted DM participant is represented by a nullable canonical-pair
            # FK. Do not fall back to a membership query for every archived row.
            return None

        return None

    def get_last_message(self):
        """Get last message body from cached dict"""
        return self.get_cached_value("last_message")

    def get_room_type(self):
        return self.get_cached_value("room_type", self.Type.DIRECT)

    def get_channel_kind(self):
        return self.get_cached_value("channel_kind")

    def get_name(self):
        return self.get_cached_value("name")

    def get_avatar_url(self):
        return self.get_cached_value("avatar_url")

    def get_direct_user_low_id(self):
        return self.get_cached_value("direct_user_low_id")

    def get_direct_user_high_id(self):
        return self.get_cached_value("direct_user_high_id")

    def get_archived_at(self):
        return self.get_cached_value("archived_at")

    def get_last_activity_at(self):
        return self.get_cached_value("last_activity_at")

    def get_last_msg_id(self):
        """Get last message ID from cached dict"""
        return self.get_cached_value("last_msg_id")

    def get_last_msg_time(self):
        """Get last message time from cached dict"""
        return self.get_cached_value("last_msg_time")

    @classmethod
    def prefetch_room_cache(cls, room_ids):
        """Prefetch room cache for multiple rooms"""
        cls.get_cached_instances(*room_ids)

    @classmethod
    def get_or_create_room(cls, user_one, user_two):
        """Get or atomically create the canonical DM for an unordered user pair."""
        low_id, high_id = sorted((user_one.id, user_two.id))
        lookup = {
            "room_type": cls.Type.DIRECT,
            "direct_user_low_id": low_id,
            "direct_user_high_id": high_id,
        }
        try:
            room = cls.objects.get(**lookup)
        except cls.DoesNotExist:
            try:
                with transaction.atomic():
                    room = cls.objects.create(**lookup)
                    member_ids = sorted({user_one.id, user_two.id})
                    UserRoom.objects.bulk_create(
                        [
                            UserRoom(
                                user_id=user_id,
                                room=room,
                                role=None,
                                manual_role=None,
                            )
                            for user_id in member_ids
                        ]
                    )
            except IntegrityError:
                room = cls.objects.get(**lookup)

        cls.dirty_cache(room.id)
        return room


class RoomRedirect(models.Model):
    """Permanent mapping from a removed legacy room ID to its canonical room."""

    old_room_id = models.PositiveBigIntegerField(
        primary_key=True,
        verbose_name=_("old room ID"),
    )
    canonical_room = models.ForeignKey(
        Room,
        on_delete=CASCADE,
        related_name="legacy_redirects",
        verbose_name=_("canonical room"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("created at"),
    )

    class Meta:
        app_label = "chat_box"
        verbose_name = _("room redirect")
        verbose_name_plural = _("room redirects")


class Message(models.Model):
    class Kind(models.TextChoices):
        USER = "user", _("User message")
        SYSTEM = "system", _("System message")

    class SystemEvent(models.TextChoices):
        JOIN = "join", _("User joined")
        LEAVE = "leave", _("User left")
        RENAME = "rename", _("Room renamed")

    author = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    time = models.DateTimeField(
        verbose_name=_("posted time"), auto_now_add=True, db_index=True
    )
    body = models.TextField(verbose_name=_("body of comment"), max_length=8192)
    hidden = models.BooleanField(verbose_name="is hidden", default=False)
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.USER,
        verbose_name=_("message kind"),
    )
    system_event = models.CharField(
        max_length=16,
        choices=SystemEvent.choices,
        null=True,
        blank=True,
        verbose_name=_("system event"),
    )
    event_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("system event data"),
    )
    room = models.ForeignKey(
        Room,
        verbose_name=_("room"),
        on_delete=CASCADE,
    )
    reply_to = models.ForeignKey(
        "self",
        verbose_name=_("reply to"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replies",
    )

    def save(self, *args, **kwargs):
        self.body = self.body.strip()
        super(Message, self).save(*args, **kwargs)

    class Meta:
        verbose_name = "message"
        verbose_name_plural = "messages"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["hidden", "room", "-id"]),
            models.Index(fields=["room", "kind", "hidden", "-id"]),
            models.Index(fields=["room", "author", "time"]),
        ]
        app_label = "chat_box"


class MessageReaction(models.Model):
    """One emoji reaction by one user on one message (Messenger-style).

    unique_together(message, user) enforces "at most one reaction per user per
    message"; re-reacting updates the row, so a user never has two reactions.
    """

    message = models.ForeignKey(
        Message, verbose_name=_("message"), on_delete=CASCADE, related_name="reactions"
    )
    user = models.ForeignKey(Profile, verbose_name=_("user"), on_delete=CASCADE)
    reaction = models.CharField(max_length=10, choices=CHAT_REACTIONS)
    created = models.DateTimeField(auto_now=True)

    class Meta:
        # (message, user) unique also indexes lookups by message (leading column).
        unique_together = ("message", "user")
        app_label = "chat_box"


class UserRoom(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", _("Active")
        LEFT = "left", _("Left")
        REMOVED = "removed", _("Removed")
        INELIGIBLE = "ineligible", _("Ineligible")

    class Role(models.TextChoices):
        ADMIN = "admin", _("Administrator")
        MODERATOR = "moderator", _("Moderator")
        MEMBER = "member", _("Member")

    user = models.ForeignKey(Profile, verbose_name=_("user"), on_delete=CASCADE)
    room = models.ForeignKey(
        Room,
        verbose_name=_("room"),
        on_delete=CASCADE,
    )
    last_seen = models.DateTimeField(verbose_name=_("last seen"), auto_now_add=True)
    unread_count = models.IntegerField(default=0, db_index=True)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.ACTIVE,
        db_index=True,
        verbose_name=_("membership state"),
    )
    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.MEMBER,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("effective room role"),
    )
    manual_role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.MEMBER,
        null=True,
        blank=True,
        verbose_name=_("manual room role"),
    )
    synced_role = models.CharField(
        max_length=16,
        choices=Role.choices,
        null=True,
        blank=True,
        verbose_name=_("synchronized room role"),
    )
    # Deprecated compatibility field. Site administrators now join rooms only
    # through the same membership flows as other users.
    site_admin_joined = models.BooleanField(
        default=False,
        verbose_name=_("joined through site administration"),
    )
    joined_at = models.DateTimeField(
        default=timezone.now,
        verbose_name=_("joined at"),
    )
    activated_at = models.DateTimeField(
        default=timezone.now,
        verbose_name=_("activated at"),
    )
    deactivated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("deactivated at"),
    )
    last_read_message_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name=_("last read message ID"),
    )
    is_hidden = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("room is hidden"),
    )
    hidden_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("hidden at"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "room"),
                name="chat_unique_user_room_membership",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "state", "is_hidden", "room"]),
            models.Index(fields=["room", "state", "role", "user"]),
        ]
        app_label = "chat_box"


class RoomInvitation(models.Model):
    room = models.OneToOneField(
        Room,
        on_delete=CASCADE,
        related_name="invitation",
        verbose_name=_("room"),
    )
    nonce = models.CharField(max_length=64, verbose_name=_("invitation nonce"))
    created_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_room_invitations",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    rotated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("rotated at"),
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("revoked at"),
    )

    class Meta:
        app_label = "chat_box"
        verbose_name = _("room invitation")
        verbose_name_plural = _("room invitations")


class RoomMute(models.Model):
    room = models.ForeignKey(
        Room,
        on_delete=CASCADE,
        related_name="room_mutes",
        verbose_name=_("room"),
    )
    target = models.ForeignKey(
        Profile,
        on_delete=CASCADE,
        related_name="room_mutes_received",
        verbose_name=_("target user"),
    )
    muted_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_mutes_created",
        verbose_name=_("muted by"),
    )
    reason = models.TextField(verbose_name=_("reason"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    expires_at = models.DateTimeField(db_index=True, verbose_name=_("expires at"))
    duration_days = models.PositiveSmallIntegerField(verbose_name=_("duration in days"))
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("revoked at"),
    )
    revoked_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_mutes_revoked",
        verbose_name=_("revoked by"),
    )

    class Meta:
        app_label = "chat_box"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["room", "target", "revoked_at", "expires_at"]),
            models.Index(fields=["room", "revoked_at", "expires_at"]),
        ]
        verbose_name = _("room mute")
        verbose_name_plural = _("room mutes")


class RoomBan(models.Model):
    room = models.ForeignKey(
        Room,
        on_delete=CASCADE,
        related_name="room_bans",
        verbose_name=_("room"),
    )
    target = models.ForeignKey(
        Profile,
        on_delete=CASCADE,
        related_name="room_bans_received",
        verbose_name=_("target user"),
    )
    banned_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_bans_created",
        verbose_name=_("banned by"),
    )
    reason = models.TextField(verbose_name=_("reason"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("revoked at"),
    )
    revoked_by = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_bans_revoked",
        verbose_name=_("revoked by"),
    )

    class Meta:
        app_label = "chat_box"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["room", "target", "revoked_at"]),
            models.Index(fields=["room", "revoked_at", "-created_at"]),
        ]
        verbose_name = _("room ban")
        verbose_name_plural = _("room bans")


class RoomModerationLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", _("Room created")
        JOIN = "join", _("User joined")
        LEAVE = "leave", _("User left")
        ADD = "add", _("User added")
        REMOVE = "remove", _("User removed")
        BAN = "ban", _("User banned")
        UNBAN = "unban", _("User unbanned")
        PROMOTE = "promote", _("User promoted")
        DEMOTE = "demote", _("User demoted")
        RENAME = "rename", _("Room renamed")
        ARCHIVE = "archive", _("Room archived")
        RESTORE = "restore", _("Room restored")
        INVITE_ROTATE = "invite_rotate", _("Invitation rotated")
        INVITE_REVOKE = "invite_revoke", _("Invitation revoked")
        MUTE = "mute", _("User muted")
        UNMUTE = "unmute", _("User unmuted")
        HIDE_MESSAGE = "hide_message", _("Message hidden")
        AVATAR_CHANGE = "avatar_change", _("Room avatar changed")

    room = models.ForeignKey(
        Room,
        on_delete=CASCADE,
        related_name="moderation_logs",
        verbose_name=_("room"),
    )
    action = models.CharField(
        max_length=24,
        choices=Action.choices,
        db_index=True,
        verbose_name=_("action"),
    )
    actor = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_moderation_actions",
        verbose_name=_("actor"),
    )
    target = models.ForeignKey(
        Profile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_moderation_actions_received",
        verbose_name=_("target user"),
    )
    message = models.ForeignKey(
        Message,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="room_moderation_logs",
        verbose_name=_("message"),
    )
    reason = models.TextField(blank=True, verbose_name=_("reason"))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("metadata"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = "chat_box"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["room", "action", "-created_at"]),
            models.Index(fields=["actor", "action", "-created_at"]),
            models.Index(fields=["room", "-created_at"]),
        ]
        verbose_name = _("room moderation log")
        verbose_name_plural = _("room moderation logs")


class Ignore(models.Model):
    user = models.OneToOneField(
        Profile,
        related_name="ignored_chat_users",
        verbose_name=_("user"),
        on_delete=CASCADE,
        db_index=True,
    )
    ignored_users = models.ManyToManyField(Profile)

    class Meta:
        app_label = "chat_box"

    @classmethod
    def is_ignored(cls, current_user, ignored_user):
        """Check if a user has ignored another user."""
        if current_user is None or ignored_user is None:
            return False
        return ignored_user.id in get_ignored_user_ids(current_user)

    @classmethod
    def get_ignored_room_ids(cls, user):
        """Get all rooms where the other user is ignored."""
        return _get_ignored_room_ids(user)

    @classmethod
    def add_ignore(cls, current_user, ignored_user):
        """Add a user to the ignore list."""
        if current_user is None or ignored_user is None:
            raise ValueError("Current user and ignored user must not be None.")

        if current_user == ignored_user:
            raise ValueError("A user cannot ignore themselves.")

        if cls.is_ignored(current_user, ignored_user):
            raise ValueError("You have already ignored this user.")

        ignore, _ = cls.objects.get_or_create(user=current_user)
        ignore.ignored_users.add(ignored_user)
        get_ignored_user_ids.dirty(current_user)
        _get_ignored_room_ids.dirty(current_user)

    @classmethod
    def remove_ignore(cls, current_user, ignored_user):
        """Remove a user from the ignore list."""
        if current_user is None or ignored_user is None:
            raise ValueError("Current user and ignored user must not be None.")

        if current_user == ignored_user:
            raise ValueError("A user cannot unignore themselves.")

        if not cls.is_ignored(current_user, ignored_user):
            raise ValueError("This user is not ignored, so they cannot be unignored.")

        ignore, _ = cls.objects.get_or_create(user=current_user)
        ignore.ignored_users.remove(ignored_user)
        get_ignored_user_ids.dirty(current_user)
        _get_ignored_room_ids.dirty(current_user)

    @classmethod
    def toggle_ignore(cls, current_user, ignored_user):
        """Toggle the ignore status and return the resulting state."""
        if cls.is_ignored(current_user, ignored_user):
            cls.remove_ignore(current_user, ignored_user)
            return False
        cls.add_ignore(current_user, ignored_user)
        return True


class ChatModerationLog(models.Model):
    ACTIONS = (
        ("keep", _("Keep")),
        ("hide", _("Hide Message")),
        ("review", _("Needs Review")),
        ("mute", _("Mute User")),
        ("mute_temp", _("Temporarily Mute User")),
        ("mute_perm", _("Permanently Mute User")),
    )

    message = models.ForeignKey(
        Message, on_delete=CASCADE, related_name="moderation_logs"
    )
    action = models.CharField(max_length=20, choices=ACTIONS)
    reason = models.TextField(blank=True)
    mute_until = models.DateTimeField(null=True, blank=True)
    mute_duration_days = models.PositiveIntegerField(null=True, blank=True)
    is_automated = models.BooleanField(default=False)
    moderator = models.ForeignKey(
        Profile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_moderation_actions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["message", "created_at"]),
        ]
        app_label = "chat_box"
        verbose_name = _("chat moderation log")
        verbose_name_plural = _("chat moderation logs")

    def __str__(self):
        return f"{self.get_action_display()} - Message #{self.message_id} - {self.created_at}"

    @classmethod
    def log_action(
        cls,
        message,
        action,
        reason="",
        is_automated=False,
        moderator=None,
        mute_until=None,
        mute_duration_days=None,
    ):
        return cls.objects.create(
            message=message,
            action=action,
            reason=reason,
            is_automated=is_automated,
            moderator=moderator,
            mute_until=mute_until,
            mute_duration_days=mute_duration_days,
        )


def _get_room_batch(args_list):
    """Batch function to get room data for multiple rooms"""
    room_ids = [args[0] for args in args_list]

    # Room.last_msg_id deliberately tracks only the latest visible user-authored
    # message, so system events never replace sidebar previews.
    last_messages = {}
    last_msg_times = {}
    room_rows = {
        row["id"]: row
        for row in Room.objects.filter(id__in=room_ids).values(
            "id",
            "room_type",
            "channel_kind",
            "name",
            "avatar",
            "organization_id",
            "direct_user_low_id",
            "direct_user_high_id",
            "archived_at",
            "last_activity_at",
            "last_msg_id",
        )
    }
    room_last_msg_ids = {
        room_id: row["last_msg_id"] for room_id, row in room_rows.items()
    }
    organization_ids = {
        row["organization_id"]
        for row in room_rows.values()
        if row["channel_kind"] == Room.ChannelKind.ORGANIZATION
        and row["organization_id"]
        and not row["avatar"]
    }
    organizations = {
        organization.id: organization
        for organization in Organization.get_cached_instances(*organization_ids)
    }
    message_ids = [
        message_id for message_id in room_last_msg_ids.values() if message_id
    ]
    if message_ids:
        last_msgs = Message.objects.filter(
            id__in=message_ids,
            hidden=False,
            kind=Message.Kind.USER,
        ).values("room_id", "body", "time")

        for msg in last_msgs:
            last_messages[msg["room_id"]] = msg["body"]
            last_msg_times[msg["room_id"]] = msg["time"]

    # Build results
    results = []
    for room_id in room_ids:
        result = dict(room_rows.get(room_id, {}))
        avatar_name = result.pop("avatar", None)
        organization_id = result.pop("organization_id", None)
        organization = organizations.get(organization_id)
        result.update(
            {
                "avatar_url": (
                    default_storage.url(avatar_name)
                    if avatar_name
                    else (
                        organization.get_organization_image_url()
                        if organization
                        else None
                    )
                ),
                "last_message": last_messages.get(room_id),
                "last_msg_id": room_last_msg_ids.get(room_id),
                "last_msg_time": last_msg_times.get(room_id),
            }
        )
        # Remove None values to save cache space
        result = {k: v for k, v in result.items() if v is not None}
        results.append(result)

    return results


@cache_wrapper(prefix="Rgcd2", expected_type=dict, batch_fn=_get_room_batch)
def _get_room(room_id):
    """Get cached room dict including users and last message"""
    results = _get_room_batch([(room_id,)])
    return results[0]


@cache_wrapper(prefix="giuis", expected_type=set)
def get_ignored_user_ids(user):
    """
    Returns a set of all user IDs that the given user has ignored.
    """
    try:
        return set(
            Ignore.objects.get(user=user).ignored_users.values_list("id", flat=True)
        )
    except Ignore.DoesNotExist:
        return set()


@cache_wrapper(prefix="giri", expected_type=set)
def _get_ignored_room_ids(user):
    """
    Returns a set of all room IDs where the given user has ignored the other user.
    This is used to filter out rooms from unread counts and room lists.
    """
    # Get all users that this user has ignored
    ignored_user_ids = get_ignored_user_ids(user)

    if not ignored_user_ids:
        return set()

    return set(
        Room.objects.filter(room_type=Room.Type.DIRECT)
        .filter(
            Q(direct_user_low_id=user.id, direct_user_high_id__in=ignored_user_ids)
            | Q(direct_user_high_id=user.id, direct_user_low_id__in=ignored_user_ids)
        )
        .values_list("id", flat=True)
    )


@cache_wrapper(prefix="gfmi")
def get_first_msg_id(room_id):
    try:
        msg = Message.objects.filter(room=room_id, hidden=False).earliest("id")
    except Message.DoesNotExist:
        return None
    return msg.id
