from django import forms
from django.views import View
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.urls import reverse_lazy

from judge.widgets import HeavyPreviewPageDownWidget


class MarkdownEditorForm(forms.Form):
    body = forms.CharField(
        widget=HeavyPreviewPageDownWidget(
            id="id_body",
            preview=reverse_lazy("comment_preview"),
            preview_timeout=1000,
            hide_preview_button=True,
        ),
        required=False,
        label="",
    )


class MarkdownEditor(View):
    def get(self, request):
        form = MarkdownEditorForm()
        return render(
            request,
            "markdown_editor/markdown_editor.html",
            {
                "title": _("Markdown Editor"),
                "form": form,
            },
        )
