from importlib import import_module

from django.apps import AppConfig


class ChatBoxConfig(AppConfig):
    name = "chat_box"

    def ready(self):
        import_module("chat_box.signals")
