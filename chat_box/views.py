from django.http import HttpResponseRedirect
from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.urls import reverse
from django.utils import timezone


from .models import Message


class ChatView(ListView):
    model = Message
    context_object_name = 'messages'
    template_name = 'chat/chat.html'
    title = _('Chat Box')

    def get_context_data(self, **kwargs):
        context = super(ChatView, self).get_context_data(**kwargs)
        context['title'] = self.title
        return context

    def get_queryset(self):
        return None


def send(request):
    new_message = Message(body=request.POST['message'],
                          author=request.profile,
                          time=timezone.now())
    new_message.save()
    return HttpResponseRedirect(reverse('chat'))
