from django.template.loader import render_to_string
from django.contrib.sites.shortcuts import get_current_site
from django.conf import settings


def render_email_message(request, contexts):
    current_site = get_current_site(request)
    email_contexts = {
        "username": request.user.username,
        "domain": current_site.domain,
        "site_name": settings.SITE_NAME,
        "message": None,
        "title": None,
        "button_text": "Click here",
        "url_path": None,
        "protocol": "https" if request.is_secure() else "http",
    }
    email_contexts.update(contexts)
    message = render_to_string("general_email.html", email_contexts)
    return message
