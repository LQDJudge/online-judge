from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import render
from django.forms.models import model_to_dict
from django.utils import timezone


from judge.jinja2.gravatar import gravatar
from .models import Message, Profile
import json
    
def format_messages(messages):
    msg_list = [{
        'time': msg.time,
        'author': msg.author,
        'body': msg.body,
        'image': gravatar(msg.author, 32),
        'id': msg.id,
        'css_class': msg.author.css_class,
    } for msg in messages]
    return json.dumps(msg_list, default=str)

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

class ChatView(ListView):
    model = Message
    context_object_name = 'message'
    template_name = 'chat/chat.html'
    title = _('Chat Box')
    paginate_by = 50
    paginator = Paginator(Message.objects.filter(hidden=False), paginate_by)

    def get_queryset(self):
        return Message.objects.filter(hidden=False)

    def get(self, request, *args, **kwargs):
        page = request.GET.get('page')
        if (page == None):
            # return render(request, 'chat/chat.html', {'message': format_messages(Message.objects.all())})
            return super().get(request, *args, **kwargs)

        cur_page = self.paginator.get_page(page)
        return HttpResponse(format_messages(cur_page.object_list))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # hard code, should be fixed later
        address = f'{self.request.get_host()}/ws/chat/'
        if self.request.is_secure():
            context['ws_address'] = f'wss://{address}'
        else:
            context['ws_address'] = f'ws://{address}'

        context['title'] = self.title
        last_five_minutes = timezone.now()-timezone.timedelta(minutes=5)
        context['online_users'] = Profile.objects \
                                        .filter(display_rank='user',
                                            last_access__gte = last_five_minutes)\
                                        .order_by('-rating')
        context['admin_status'] = get_admin_online_status()
        return context

def delete_message(request):
    ret = {'delete': 'done'}
    
    if request.method == 'GET':
        return JsonResponse(ret)

    if request.user.is_staff:
        messid = int(request.POST.get('messid'))
        all_mess = Message.objects.all()
        
        for mess in all_mess:
            if mess.id == messid:
                mess.hidden = True
                mess.save()
                new_elt = {'time': mess.time, 'content': mess.body}
                ret = new_elt
                break
        
        return JsonResponse(ret)
    
    return JsonResponse(ret)
