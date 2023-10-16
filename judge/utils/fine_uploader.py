# https://github.com/FineUploader/server-examples/blob/master/python/django-fine-uploader

from django.conf import settings
from django import forms
from django.forms import ClearableFileInput

import os, os.path
import shutil

__all__ = ("handle_upload", "save_upload", "FineUploadForm", "FineUploadFileInput")


def combine_chunks(total_parts, total_size, source_folder, dest):
    if not os.path.exists(os.path.dirname(dest)):
        os.makedirs(os.path.dirname(dest))

    with open(dest, "wb+") as destination:
        for i in range(total_parts):
            part = os.path.join(source_folder, str(i))
            with open(part, "rb") as source:
                destination.write(source.read())


def save_upload(f, path):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, "wb+") as destination:
        if hasattr(f, "multiple_chunks") and f.multiple_chunks():
            for chunk in f.chunks():
                destination.write(chunk)
        else:
            destination.write(f.read())


# pass callback function to post_upload
def handle_upload(f, fileattrs, upload_dir, post_upload=None):
    chunks_dir = settings.CHUNK_UPLOAD_DIR
    if not os.path.exists(os.path.dirname(chunks_dir)):
        os.makedirs(os.path.dirname(chunks_dir))
    chunked = False
    dest_folder = upload_dir
    dest = os.path.join(dest_folder, fileattrs["qqfilename"])

    # Chunked
    if fileattrs.get("qqtotalparts") and int(fileattrs["qqtotalparts"]) > 1:
        chunked = True
        dest_folder = os.path.join(chunks_dir, fileattrs["qquuid"])
        dest = os.path.join(
            dest_folder, fileattrs["qqfilename"], str(fileattrs["qqpartindex"])
        )
    save_upload(f, dest)

    # If the last chunk has been sent, combine the parts.
    if chunked and (fileattrs["qqtotalparts"] - 1 == fileattrs["qqpartindex"]):
        combine_chunks(
            fileattrs["qqtotalparts"],
            fileattrs["qqtotalfilesize"],
            source_folder=os.path.dirname(dest),
            dest=os.path.join(upload_dir, fileattrs["qqfilename"]),
        )
        shutil.rmtree(os.path.dirname(os.path.dirname(dest)))

    if post_upload and (
        not chunked or fileattrs["qqtotalparts"] - 1 == fileattrs["qqpartindex"]
    ):
        post_upload()


class FineUploadForm(forms.Form):
    qqfile = forms.FileField()
    qquuid = forms.CharField()
    qqfilename = forms.CharField()
    qqpartindex = forms.IntegerField(required=False)
    qqchunksize = forms.IntegerField(required=False)
    qqpartbyteoffset = forms.IntegerField(required=False)
    qqtotalfilesize = forms.IntegerField(required=False)
    qqtotalparts = forms.IntegerField(required=False)


class FineUploadFileInput(ClearableFileInput):
    template_name = "widgets/fine_uploader.html"

    def fine_uploader_id(self, name):
        return name + "_" + "fine_uploader"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"].update(
            {
                "fine_uploader_id": self.fine_uploader_id(name),
            }
        )
        return context
