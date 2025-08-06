import os
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.conf import settings


def about(request):
    return render(
        request,
        "about/about.html",
        {
            "title": _("About"),
        },
    )


def custom_checker_sample(request):
    # Read content from markdown file
    md_file_path = os.path.join(
        settings.BASE_DIR, "templates", "about", "custom-checker-sample.md"
    )
    try:
        with open(md_file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "Content not found."
    return render(
        request,
        "about/custom-checker-sample.html",
        {
            "title": _("Custom Checker Sample"),
            "content": content,
        },
    )


def contest_format_instructions(request):
    # Read content from markdown file
    md_file_path = os.path.join(
        settings.BASE_DIR, "templates", "about", "contest-format-instructions.md"
    )
    try:
        with open(md_file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "Content not found."
    return render(
        request,
        "about/contest-format-instructions.html",
        {
            "title": _("Contest Format Instructions"),
            "content": content,
        },
    )
