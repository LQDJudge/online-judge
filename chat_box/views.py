from django.shortcuts import render
from django.utils.translation import gettext as _


def chat(request):
    return render(request, 'chat/chat.html', {
        'title': _('Chat Box'),
    })
