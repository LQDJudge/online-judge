from django.urls import re_path

from . import consumers

ASGI_APPLICATION = "chat_box.routing.application"
websocket_urlpatterns = [
    re_path(r'ws/chat/', consumers.ChatConsumer),
]
