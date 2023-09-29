from django.views import View
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _


class MarkdownEditor(View):
    def get(self, request):
        return render(
            request,
            "markdown_editor/markdown_editor.html",
            {
                "title": _("Markdown Editor"),
            },
        )
