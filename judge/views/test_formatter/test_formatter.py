from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.files import File
from django.core.files.base import ContentFile
from django.http import (
    FileResponse,
    HttpResponseRedirect,
    HttpResponseBadRequest,
    HttpResponse,
)
from judge.models import TestFormatterModel
from judge.forms import TestFormatterForm
from judge.views.test_formatter import tf_logic, tf_utils
from django.utils.translation import gettext_lazy as _
from zipfile import ZipFile, ZIP_DEFLATED

import os
import uuid
from dmoj import settings


def id_to_path(id):
    return os.path.join(settings.MEDIA_ROOT, "test_formatter/" + id + "/")


def get_names_in_archive(file_path):
    suffixes = ("inp", "out", "INP", "OUT")
    with ZipFile(os.path.join(settings.MEDIA_ROOT, file_path)) as f:
        result = [
            x for x in f.namelist() if not x.endswith("/") and x.endswith(suffixes)
        ]
        return list(sorted(result, key=tf_utils.natural_sorting_key))


def get_renamed_archive(file_str, file_name, file_path, bef, aft):
    target_file_id = str(uuid.uuid4())
    source_path = os.path.join(settings.MEDIA_ROOT, file_str)
    target_path = os.path.join(settings.MEDIA_ROOT, file_str + "_" + target_file_id)
    new_path = os.path.join(settings.MEDIA_ROOT, "test_formatter/" + file_name)

    source = ZipFile(source_path, "r")
    target = ZipFile(target_path, "w", ZIP_DEFLATED)

    for bef_name, aft_name in zip(bef, aft):
        target.writestr(aft_name, source.read(bef_name))

    os.remove(source_path)
    os.rename(target_path, new_path)

    target.close()
    source.close()

    return {"file_path": "test_formatter/" + file_name}


class TestFormatter(View):
    form_class = TestFormatterForm()

    def get(self, request):
        return render(
            request,
            "test_formatter/test_formatter.html",
            {"title": _("Test Formatter"), "form": self.form_class},
        )

    def post(self, request):
        form = TestFormatterForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect("edit_page")
        return render(
            request, "test_formatter/test_formatter.html", {"form": self.form_class}
        )


class EditTestFormatter(View):
    file_path = ""

    def get(self, request):
        file = TestFormatterModel.objects.last()
        filestr = str(file.file)
        filename = filestr.split("/")[-1]
        filepath = filestr.split("/")[0]

        bef_file = get_names_in_archive(filestr)
        preview_data = {
            "bef_inp_format": bef_file[0],
            "bef_out_format": bef_file[1],
            "aft_inp_format": "input.000",
            "aft_out_format": "output.000",
            "file_str": filestr,
        }

        preview = tf_logic.preview(preview_data)

        response = ""
        for i in range(len(bef_file)):
            bef = preview["bef_preview"][i]["value"]
            aft = preview["aft_preview"][i]["value"]
            response = response + f"<p>{bef} => {aft}</p>\n"

        return render(
            request,
            "test_formatter/edit_test_formatter.html",
            {
                "title": _("Test Formatter"),
                "check": 0,
                "files_list": bef_file,
                "file_name": filename,
                "res": response,
            },
        )

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "convert":
            try:
                file = TestFormatterModel.objects.last()
                filestr = str(file.file)
                filename = filestr.split("/")[-1]
                filepath = filestr.split("/")[0]
                bef_inp_format = request.POST["bef_inp_format"]
                bef_out_format = request.POST["bef_out_format"]
                aft_inp_format = request.POST["aft_inp_format"]
                aft_out_format = request.POST["aft_out_format"]
                aft_file_name = request.POST["file_name"]
            except KeyError:
                return HttpResponseBadRequest("No data.")

            if filename != aft_file_name:
                source_path = os.path.join(settings.MEDIA_ROOT, filestr)
                new_path = os.path.join(
                    settings.MEDIA_ROOT, "test_formatter/" + aft_file_name
                )
                os.rename(source_path, new_path)
                filename = aft_file_name

            preview_data = {
                "bef_inp_format": bef_inp_format,
                "bef_out_format": bef_out_format,
                "aft_inp_format": aft_inp_format,
                "aft_out_format": aft_out_format,
                "file_name": filename,
                "file_path": filepath,
                "file_str": filepath + "/" + filename,
            }

            converted_zip = tf_logic.convert(preview_data)

            global file_path
            file_path = converted_zip["file_path"]

            zip_instance = TestFormatterModel()
            zip_instance.file = file_path
            zip_instance.save()

            preview = tf_logic.preview(preview_data)
            response = HttpResponse()

            for i in range(len(preview["bef_preview"])):
                bef = preview["bef_preview"][i]["value"]
                aft = preview["aft_preview"][i]["value"]
                response.write(f"<p>{bef} => {aft}</p>")

            return response

        elif action == "download":
            return HttpResponse(file_path)

        return HttpResponseBadRequest("Invalid action")


class DownloadTestFormatter(View):
    def get(self, request):
        file_path = request.GET.get("file_path")
        file_name = file_path.split("/")[-1]
        preview_file = tf_logic.preview_file(file_path)

        response = ""
        for i in range(len(preview_file)):
            response = response + (f"<p>{preview_file[i]}</p>\n")

        files_list = [preview_file[0], preview_file[1]]

        return render(
            request,
            "test_formatter/download_test_formatter.html",
            {
                "title": _("Test Formatter"),
                "response": response,
                "files_list": files_list,
                "file_path": os.path.join(settings.MEDIA_ROOT, file_path),
                "file_path_getnames": file_path,
                "file_name": file_name,
            },
        )

    def post(self, request):
        file_path = request.POST.get("file_path")

        with open(file_path, "rb") as zip_file:
            response = HttpResponse(zip_file.read(), content_type="application/zip")
            response[
                "Content-Disposition"
            ] = f"attachment; filename={os.path.basename(file_path)}"
            return response
