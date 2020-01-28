# based on https://github.com/narrowfail/django-channels-chat

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

    def notify_ws_clients(self):
        # inform client that there is a new message
        notification = {
            'type': 'recieve_group_message',
            'message': '{}'.format(self.id)
        }
        channel_layer = get_channel_layer()
        # print("user.id {}".format(self.user.id))
        # print("user.id {}".format(self.recipient.id))

        async_to_sync(channel_layer.group_send)("{}".format(self.user.id), notification)
        async_to_sync(channel_layer.group_send)("{}".format(self.recipient.id), notification)

    def save(self, *args, **kwargs):
        new_message = self.id
        self.body = self.body.strip()
        super(Message, self).save(*args, **kwargs)
        if new_message is None:
            self.notify_ws_clients()

    class Meta:
        app_label = 'chat_box'
        verbose_name = 'message'
        verbose_name_plural = 'messages'
        ordering = ('-time',)
