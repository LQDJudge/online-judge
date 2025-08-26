from django.db import models
from django.db.models import CASCADE, Q, Max
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from judge.models.profile import Profile
from judge.caching import cache_wrapper, CacheableModel


__all__ = ["Message", "Room", "UserRoom", "Ignore"]


class Room(CacheableModel):
    last_msg_id = models.IntegerField(
        verbose_name=_("last message id"), null=True, db_index=True
    )

    class Meta:
        app_label = "chat_box"

    @classmethod
    def get_cached_dict(cls, room_id):
        return _get_room(room_id)

    @classmethod
    def get_cached_instances(cls, *ids):
        # Prefetch cache data
        _get_room.batch([(id,) for id in ids])
        return [cls(id=id) for id in ids]

    @classmethod
    def dirty_cache(cls, *ids):
        id_list = [(id,) for id in ids]
        _get_room.dirty_multi(id_list)

    def contain(self, profile):
        """Check if profile is a member of this room"""
        return profile.id in self.get_user_ids()

    def other_user(self, profile):
        """Get the other user in a two-person room"""
        user_ids = self.get_user_ids()
        if len(user_ids) == 2:
            other_id = user_ids[0] if user_ids[1] == profile.id else user_ids[1]
            return Profile(id=other_id)
        return None

    def other_user_id(self, profile):
        """Get the other user's ID in a two-person room"""
        user_ids = self.get_user_ids()
        if len(user_ids) == 2:
            return user_ids[0] if user_ids[1] == profile.id else user_ids[1]
        return None

    def users(self):
        """Get all users in this room (deprecated, use get_users)"""
        return self.get_users()

    def get_users(self):
        """Get all users in this room from cached dict"""
        user_ids = self.get_user_ids()
        return Profile.get_cached_instances(*user_ids) if user_ids else []

    def get_user_ids(self):
        """Get all user IDs in this room from cached dict"""
        return self.get_cached_value("user_ids", [])

    def get_last_message(self):
        """Get last message body from cached dict"""
        return self.get_cached_value("last_message")

    def get_last_msg_id(self):
        """Get last message ID from cached dict"""
        return self.get_cached_value("last_msg_id")

    def get_last_msg_time(self):
        """Get last message time from cached dict"""
        return self.get_cached_value("last_msg_time")

    def last_message_body(self):
        """Deprecated, use get_last_message()"""
        return self.get_last_message()

    @classmethod
    def prefetch_room_cache(cls, room_ids):
        """Prefetch room cache for multiple rooms"""
        cls.get_cached_instances(*room_ids)

    @classmethod
    def get_or_create_room(cls, user_one, user_two):
        """Get or create a room between two users"""
        room_id = get_common_room_id(user_one, user_two)

        if room_id:
            return cls(id=room_id)

        # No existing room found, create new room
        room = cls.objects.create(last_msg_id=None)

        # Create UserRoom entries for both users
        UserRoom.objects.create(user=user_one, room=room, last_seen=timezone.now())
        UserRoom.objects.create(user=user_two, room=room, last_seen=timezone.now())

        # Dirty caches for both users
        cls.dirty_cache(room.id)
        get_user_room_list.dirty(user_one.id)
        get_user_room_list.dirty(user_two.id)

        room.save()

        return room


class Message(models.Model):
    author = models.ForeignKey(Profile, verbose_name=_("user"), on_delete=CASCADE)
    time = models.DateTimeField(
        verbose_name=_("posted time"), auto_now_add=True, db_index=True
    )
    body = models.TextField(verbose_name=_("body of comment"), max_length=8192)
    hidden = models.BooleanField(verbose_name="is hidden", default=False)
    room = models.ForeignKey(
        Room, verbose_name="room id", on_delete=CASCADE, default=None, null=True
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
        ]
        app_label = "chat_box"


class UserRoom(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_("user"), on_delete=CASCADE)
    room = models.ForeignKey(
        Room, verbose_name="room id", on_delete=CASCADE, default=None, null=True
    )
    last_seen = models.DateTimeField(verbose_name=_("last seen"), auto_now_add=True)
    unread_count = models.IntegerField(default=0, db_index=True)

    class Meta:
        unique_together = ("user", "room")
        app_label = "chat_box"


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
        """Toggle the ignore status of a user."""
        if cls.is_ignored(current_user, ignored_user):
            cls.remove_ignore(current_user, ignored_user)
        else:
            cls.add_ignore(current_user, ignored_user)


def _get_room_batch(args_list):
    """Batch function to get room data for multiple rooms"""
    room_ids = [args[0] for args in args_list]

    # Get last messages for all rooms - optimized approach
    last_messages = {}
    last_msg_times = {}

    # First, get the max message ID for each room (much more efficient)
    max_msg_ids = (
        Message.objects.filter(room_id__in=room_ids, hidden=False)
        .values("room_id")
        .annotate(max_id=Max("id"))
        .values_list("room_id", "max_id")
    )

    # Convert to dict for easier lookup
    room_max_ids = dict(max_msg_ids)

    # Now fetch only those specific messages (just the ones we need)
    if room_max_ids:
        last_msgs = Message.objects.filter(id__in=room_max_ids.values()).values(
            "room_id", "body", "time"
        )

        for msg in last_msgs:
            last_messages[msg["room_id"]] = msg["body"]
            last_msg_times[msg["room_id"]] = msg["time"]

    # Get room last_msg_id
    room_last_msg_ids = dict(
        Room.objects.filter(id__in=room_ids).values_list("id", "last_msg_id")
    )

    # Get users for all rooms
    room_user_ids = {}
    user_rooms = UserRoom.objects.filter(room_id__in=room_ids).values(
        "room_id", "user_id"
    )

    for ur in user_rooms:
        room_id = ur["room_id"]
        if room_id not in room_user_ids:
            room_user_ids[room_id] = []
        room_user_ids[room_id].append(ur["user_id"])

    # Build results
    results = []
    for room_id in room_ids:
        result = {
            "last_message": last_messages.get(room_id),
            "last_msg_id": room_last_msg_ids.get(room_id),
            "last_msg_time": last_msg_times.get(room_id),
            "user_ids": room_user_ids.get(room_id, []),
        }
        # Remove None values to save cache space
        result = {k: v for k, v in result.items() if v is not None}
        results.append(result)

    return results


@cache_wrapper(prefix="Rgcd", expected_type=dict, batch_fn=_get_room_batch)
def _get_room(room_id):
    """Get cached room dict including users and last message"""
    results = _get_room_batch([(room_id,)])
    return results[0]


@cache_wrapper(prefix="Purl", expected_type=list)
def get_user_room_list(profile_id):
    """Get sorted list of room IDs for a user, ordered by last_msg_id (descending)"""
    room_ids = list(
        UserRoom.objects.filter(user_id=profile_id)
        .select_related("room")
        .exclude(room__isnull=True)
        .order_by("-room__last_msg_id")
        .values_list("room_id", flat=True)
    )
    return room_ids


@cache_wrapper(prefix="giui", expected_type=set)
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

    user_rooms = set(get_user_room_list(user))

    if not user_rooms:
        return set()

    if len(ignored_user_ids) < 50:
        all_rooms = set()
        for ignored_user in ignored_user_ids:
            all_rooms |= set(get_user_room_list(ignored_user))
        return all_rooms.intersection(user_rooms)

    ignored_room_ids = set()

    # Get all UserRoom entries for the user's rooms to find the other participants
    other_participants = (
        UserRoom.objects.filter(room_id__in=user_rooms)
        .exclude(user=user)
        .values("room_id", "user_id")
    )

    # Check each room to see if it contains an ignored user
    for entry in other_participants:
        if entry["user_id"] in ignored_user_ids:
            ignored_room_ids.add(entry["room_id"])

    return ignored_room_ids


@cache_wrapper(prefix="giui")
def get_first_msg_id(room_id):
    msg = Message.objects.filter(room=room_id, hidden=False).earliest("id")
    if not msg:
        return None
    return msg.id


def get_common_room_id(user_one, user_two):
    user_rooms_1 = set(get_user_room_list(user_one.id))
    user_rooms_2 = get_user_room_list(user_two.id)

    for room_id in user_rooms_2:
        if room_id in user_rooms_1:
            return room_id
    return None
