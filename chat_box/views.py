from django.utils.translation import gettext as _
from django.views.generic import ListView

from .models import Message


class ChatView(ListView):
    model = Message
    context_object_name = 'message'
    template_name = 'chat/chat.html'
    title = _('Chat Box')
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = self.title
        return context
