import os
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.conf import settings


def get_localized_markdown_content(filename, request):
    """
    Get markdown content based on user's language preference.
    Falls back to English if the localized version doesn't exist.
    """
    # Get current language from request
    current_language = getattr(request, "LANGUAGE_CODE", "en")

    # Extract language code (e.g., 'en' from 'en-us')
    lang_code = current_language.split("-")[0] if current_language else "en"

    # Try to get localized version first
    localized_file = f"{filename}.{lang_code}.md"
    localized_path = os.path.join(
        settings.BASE_DIR, "templates", "docs", localized_file
    )

    # Fall back to English if localized version doesn't exist
    if not os.path.exists(localized_path):
        lang_code = "en"
        localized_file = f"{filename}.{lang_code}.md"
        localized_path = os.path.join(
            settings.BASE_DIR, "templates", "docs", localized_file
        )

    try:
        with open(localized_path, "r", encoding="utf-8") as f:
            content = f.read()
        return content, lang_code
    except FileNotFoundError:
        return "Content not found.", "en"


def contest_format_instructions(request):
    """
    Display contest format instructions with language support.
    """
    content, lang_code = get_localized_markdown_content(
        "contest-format-instructions", request
    )

    return render(
        request,
        "docs/contest-format-instructions.html",
        {
            "title": _("Contest Format Instructions"),
            "content": content,
            "language": lang_code,
        },
    )


def test_data_instructions(request):
    """
    Display test data instructions with language support.
    """
    content, lang_code = get_localized_markdown_content(
        "test-data-instructions", request
    )

    return render(
        request,
        "docs/test-data-instructions.html",
        {
            "title": _("Test Data Instructions"),
            "content": content,
            "language": lang_code,
        },
    )
