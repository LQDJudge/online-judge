from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _


from judge.models.profile import Profile


__all__ = ['Message']


class Message(models.Model):
    author = models.ForeignKey(Profile, verbose_name=_('user'), on_delete=CASCADE)
    time = models.DateTimeField(verbose_name=_('posted time'), auto_now_add=True)
    body = models.TextField(verbose_name=_('body of comment'), max_length=8192)
    hidden = models.BooleanField(verbose_name='is hidden', default=False)

    def save(self, *args, **kwargs):
        new_message = self.id
        self.body = self.body.strip()
        super(Message, self).save(*args, **kwargs)

    class Meta:
        app_label = 'chat_box'
        verbose_name = 'message'
        verbose_name_plural = 'messages'
        ordering = ('-time',)
