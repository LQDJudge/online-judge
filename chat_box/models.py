from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _


from judge.models.profile import Profile


__all__ = ['Message']

class Room(models.Model):
    user_one = models.ForeignKey(Profile, related_name="user_one", verbose_name='user 1', on_delete=CASCADE)
    user_two = models.ForeignKey(Profile, related_name="user_two", verbose_name='user 2', on_delete=CASCADE)

    def contain(self, profile):
        return self.user_one == profile or self.user_two == profile
    def other_user(self, profile):
        return self.user_one if profile == self.user_two else self.user_two
    def users(self):
        return [self.user_one, self.user_two]

class Message(models.Model):
    author = models.ForeignKey(Profile, verbose_name=_('user'), on_delete=CASCADE)
    time = models.DateTimeField(verbose_name=_('posted time'), auto_now_add=True)
    body = models.TextField(verbose_name=_('body of comment'), max_length=8192)
    hidden = models.BooleanField(verbose_name='is hidden', default=False)
    room = models.ForeignKey(Room, verbose_name='room id', on_delete=CASCADE, default=None, null=True)

    def save(self, *args, **kwargs):
        new_message = self.id
        self.body = self.body.strip()
        super(Message, self).save(*args, **kwargs)

    class Meta:
        app_label = 'chat_box'
        verbose_name = 'message'
        verbose_name_plural = 'messages'
        ordering = ('-time',)

class UserRoom(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), on_delete=CASCADE)
    room = models.ForeignKey(Room, verbose_name='room id', on_delete=CASCADE, default=None, null=True)
    last_seen = models.DateTimeField(verbose_name=_('last seen'), auto_now_add=True)


class Ignore(models.Model):
    user = models.ForeignKey(Profile, related_name="ignored_chat_users", verbose_name=_('user'), on_delete=CASCADE)
    ignored_users = models.ManyToManyField(Profile)

    @classmethod
    def is_ignored(self, current_user, new_friend):
        try:
            return current_user.ignored_chat_users.get().ignored_users \
                        .filter(id=new_friend.id).exists()
        except:
            return False

    @classmethod
    def get_ignored_users(self, user):
        try:
            return self.objects.filter(user=user)[0].ignored_users.all()
        except:
            return Profile.objects.none()

    @classmethod
    def add_ignore(self, current_user, friend):
        ignore, created = self.objects.get_or_create(
            user = current_user
        )
        ignore.ignored_users.add(friend)

    @classmethod
    def remove_ignore(self, current_user, friend):
        ignore, created = self.objects.get_or_create(
            user = current_user
        )
        ignore.ignored_users.remove(friend)

    @classmethod
    def toggle_ignore(self, current_user, friend):
        if (self.is_ignored(current_user, friend)):
            self.remove_ignore(current_user, friend)
        else:
            self.add_ignore(current_user, friend)