from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.generic import ListView

from .models import Message


class ChatView(ListView):
    model = Message
    title = _('Chat Box')
    template_name = 'chat/chat.html'
