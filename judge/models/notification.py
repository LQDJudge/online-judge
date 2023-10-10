from django.db import models
from django.utils.translation import gettext_lazy as _
from django.db.models import CASCADE, F
from django.core.exceptions import ObjectDoesNotExist

from judge.models import Profile, Comment
from judge.caching import cache_wrapper


class Notification(models.Model):
    owner = models.ForeignKey(
        Profile,
        verbose_name=_("owner"),
        related_name="notifications",
        on_delete=CASCADE,
    )
    time = models.DateTimeField(verbose_name=_("posted time"), auto_now_add=True)
    category = models.CharField(verbose_name=_("category"), max_length=1000)
    html_link = models.TextField(
        default="",
        verbose_name=_("html link to comments, used for non-comments"),
        max_length=1000,
    )
    author = models.ForeignKey(
        Profile,
        null=True,
        verbose_name=_("who trigger, used for non-comment"),
        on_delete=CASCADE,
    )
    comment = models.ForeignKey(
        Comment, null=True, verbose_name=_("comment"), on_delete=CASCADE
    )  # deprecated
    read = models.BooleanField(verbose_name=_("read"), default=False)  # deprecated


class NotificationProfile(models.Model):
    unread_count = models.IntegerField(default=0)
    user = models.OneToOneField(Profile, on_delete=CASCADE)


def make_notification(to_users, category, html_link, author):
    for user in to_users:
        if user == author:
            continue
        notif = Notification(
            owner=user, category=category, html_link=html_link, author=author
        )
        notif.save()
        NotificationProfile.objects.get_or_create(user=user)
        NotificationProfile.objects.filter(user=user).update(
            unread_count=F("unread_count") + 1
        )
        unseen_notifications_count.dirty(user)


@cache_wrapper(prefix="unc")
def unseen_notifications_count(profile):
    try:
        return NotificationProfile.objects.get(user=profile).unread_count
    except ObjectDoesNotExist:
        return 0
