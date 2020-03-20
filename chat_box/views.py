from django.utils.translation import gettext as _
from django.views.generic import ListView

from .models import Message


def format_time(time):
    return time.strftime('%H:%M %p  %d-%m-%Y')


class ChatView(ListView):
    model = Message
    context_object_name = 'message'
    template_name = 'chat/chat.html'
    title = _('Chat Box')
    paginate_by = 50

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = self.title

        for msg in context['message']: 
            msg.time = format_time(msg.time)

        return context
