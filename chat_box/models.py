from django.db import models
from django.db.models import CASCADE, Q
from django.utils.translation import gettext_lazy as _
from django.utils.functional import cached_property


from judge.models.profile import Profile
from judge.caching import cache_wrapper


__all__ = ["Message", "Room", "UserRoom", "Ignore"]


class Room(models.Model):
    user_one = models.ForeignKey(
        Profile, related_name="user_one", verbose_name="user 1", on_delete=CASCADE
    )
    user_two = models.ForeignKey(
        Profile, related_name="user_two", verbose_name="user 2", on_delete=CASCADE
    )
    last_msg_time = models.DateTimeField(
        verbose_name=_("last seen"), null=True, db_index=True
    )

    class Meta:
        app_label = "chat_box"

    @cache_wrapper(prefix="Rinfo")
    def _info(self):
        last_msg = self.message_set.filter(hidden=False).first()
        return {
            "user_ids": [self.user_one.id, self.user_two.id],
            "last_message": last_msg.body if last_msg else None,
        }

    @cached_property
    def _cached_info(self):
        return self._info()

    def contain(self, profile):
        return profile.id in self._cached_info["user_ids"]

    def other_user(self, profile):
        return self.user_one if profile == self.user_two else self.user_two

    def other_user_id(self, profile):
        user_ids = self._cached_info["user_ids"]
        return sum(user_ids) - profile.id

    def users(self):
        return [self.user_one, self.user_two]

    def last_message_body(self):
        return self._cached_info["last_message"]


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
        new_message = self.id
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
    def is_ignored(self, current_user, new_friend):
        try:
            return current_user.ignored_chat_users.ignored_users.filter(
                id=new_friend.id
            ).exists()
        except:
            return False

    @classmethod
    def get_ignored_users(self, user):
        try:
            return self.objects.get(user=user).ignored_users.all()
        except:
            return Profile.objects.none()

    @classmethod
    def get_ignored_rooms(self, user):
        try:
            ignored_users = self.objects.get(user=user).ignored_users.all()
            return Room.objects.filter(Q(user_one=user) | Q(user_two=user)).filter(
                Q(user_one__in=ignored_users) | Q(user_two__in=ignored_users)
            )
        except:
            return Room.objects.none()

    @classmethod
    def add_ignore(self, current_user, friend):
        ignore, created = self.objects.get_or_create(user=current_user)
        ignore.ignored_users.add(friend)

    @classmethod
    def remove_ignore(self, current_user, friend):
        ignore, created = self.objects.get_or_create(user=current_user)
        ignore.ignored_users.remove(friend)

    @classmethod
    def toggle_ignore(self, current_user, friend):
        if self.is_ignored(current_user, friend):
            self.remove_ignore(current_user, friend)
        else:
            self.add_ignore(current_user, friend)
