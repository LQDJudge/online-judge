# from django.contrib.auth.decorators import login_required
from django.views import View
from django.utils.timezone import now
from django.shortcuts import render
from django.db.models import BooleanField, Value
from django.middleware.csrf import get_token
from .preview import BlogMarkdownPreviewView


# __all__ = ["MarkdownEditor"]


class MarkdownEditor(View):
    def get(self, request):
        return render(
            request,
            "markdown_editor/markdown_editor.html",
            {
                "title": ("Markdown Editor"),
            },
        )
