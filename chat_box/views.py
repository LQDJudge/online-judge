from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.forms.models import model_to_dict
from django.db.models import Case, BooleanField
from django.utils import timezone
from django.contrib.auth.decorators import login_required

import datetime

from judge import event_poster as event
from judge.jinja2.gravatar import gravatar
from .models import Message, Profile
import json
    

class ChatView(ListView):
    context_object_name = 'message'
    template_name = 'chat/chat.html'
    title = _('Chat Box')
    paginate_by = 50
    messages = Message.objects.filter(hidden=False)
    paginator = Paginator(messages, paginate_by)

    def get_queryset(self):
        return self.messages

    def get(self, request, *args, **kwargs):
        page = request.GET.get('page')
        if page == None:
            return super().get(request, *args, **kwargs)

        cur_page = self.paginator.get_page(page)

        return render(request, 'chat/message_list.html', {
            'object_list': cur_page.object_list,
        })

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['title'] = self.title
        context['last_msg'] = event.last()
        context['status_sections'] = get_status_context(self.request)
        context['today'] = timezone.now().strftime("%d-%m-%Y")
        return context


def delete_message(request):
    ret = {'delete': 'done'}
    
    if request.method == 'GET':
        return JsonResponse(ret)

    if request.user.is_staff:
        try:
            messid = int(request.POST.get('message'))
            mess = Message.objects.get(id=messid)
        except:
            return HttpResponseBadRequest()
        
        mess.hidden = True
        mess.save()
        
        return JsonResponse(ret)
    
    return JsonResponse(ret)


@login_required
def post_message(request):
    ret = {'msg': 'posted'}

    if request.method == 'GET':
        return JsonResponse(ret)

    new_message = Message(author=request.profile,
                          body=request.POST['body'])
    new_message.save()

    event.post('chat', {
        'type': 'new_message',
        'message': new_message.id,
    })

    return JsonResponse(ret)

@login_required
def chat_message_ajax(request):
    if request.method != 'GET':
        return HttpResponseBadRequest()

    try:
        message_id = request.GET['message']
    except KeyError:
        return HttpResponseBadRequest()

    try:
        message = Message.objects.filter(hidden=False).get(id=message_id)
    except Message.DoesNotExist:
        return HttpResponseBadRequest()
    return render(request, 'chat/message.html', {
        'message': message,
    })


def get_user_online_status():
    last_five_minutes = timezone.now()-timezone.timedelta(minutes=5)
    return Profile.objects \
        .filter(display_rank='user',
        last_access__gte = last_five_minutes)\
        .annotate(is_online=Case(default=True,output_field=BooleanField()))\
        .order_by('-rating')


def get_admin_online_status():
    all_admin = Profile.objects.filter(display_rank='admin')
    last_five_minutes = timezone.now()-timezone.timedelta(minutes=5)
    ret = []

    for admin in all_admin:
        is_online = False
        if (admin.last_access >= last_five_minutes):
            is_online = True
        ret.append({'user': admin, 'is_online': is_online})
    
    return ret


def get_status_context(request):
    friend_list = request.profile.get_friends()
    all_user_status = get_user_online_status()
    friend_status = []
    user_status = []

    for user in all_user_status:
        if user.username in friend_list:
            friend_status.append(user)
        else:
            user_status.append(user)

    return [
        {
            'title': 'Admins',
            'user_list': get_admin_online_status(),
        },
        {
            'title': 'Following',
            'user_list': friend_status,
        },
        {
            'title': 'Users',
            'user_list': user_status,
        },
    ]


@login_required
def online_status_ajax(request):
    return render(request, 'chat/online_status.html', {
            'status_sections': get_status_context(request),
        })