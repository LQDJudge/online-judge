from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.core.paginator import Paginator
from django.urls import reverse

from judge.jinja2.gravatar import gravatar
from .models import Message
import json


def format_time(time):
    return time.strftime('%H:%M %p  %d-%m-%Y')


def format_messages(messages):
    msg_list = [{
        'time': format_time(msg.time),
        'author': str(msg.author),
        'body': msg.body,
        'image': gravatar(msg.author, 32),
    } for msg in messages]
    return json.dumps(msg_list)


class ChatView(ListView):
    model = Message
    context_object_name = 'message'
    template_name = 'chat/chat.html'
    title = _('Chat Box')
    paginate_by = 50
    paginator = Paginator(Message.objects.all(), paginate_by)

    def get(self, request, *args, **kwargs):
        page = request.GET.get('page')
        if (page == None):
            return super().get(request, *args, **kwargs)

        cur_page = self.paginator.get_page(page)
        return HttpResponse(format_messages(cur_page.object_list))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = self.title

        for msg in context['message']:
            msg.time = format_time(msg.time)

        return context
