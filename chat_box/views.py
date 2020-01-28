from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views import View


class ChatView(View):
    template_name = 'chat.html'