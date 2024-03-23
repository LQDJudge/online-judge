from django.shortcuts import render
from django.core.files.storage import FileSystemStorage
from django import forms
from django.utils.translation import gettext as _
from django.conf import settings
from django.http import Http404

import os
import secrets
from urllib.parse import urljoin

MEDIA_PATH = "uploads"


class FileUploadForm(forms.Form):
    file = forms.FileField()


def file_upload(request):
    if not request.user.is_superuser:
        raise Http404()
    file_url = None
    if request.method == "POST":
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES["file"]
            random_str = secrets.token_urlsafe(5)
            file_name, file_extension = os.path.splitext(file.name)
            new_filename = f"{file_name}_{random_str}{file_extension}"

            fs = FileSystemStorage(
                location=os.path.join(settings.MEDIA_ROOT, MEDIA_PATH)
            )
            filename = fs.save(new_filename, file)
            file_url = urljoin(settings.MEDIA_URL, os.path.join(MEDIA_PATH, filename))
    else:
        form = FileUploadForm()

    return render(
        request,
        "custom_file_upload.html",
        {"form": form, "file_url": file_url, "title": _("File Upload")},
    )
