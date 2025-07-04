import json
import mimetypes
import os
from itertools import chain
import shutil
from tempfile import gettempdir
from zipfile import BadZipfile, ZipFile

from django import forms
from django.conf import settings
from django.http import HttpResponse, HttpRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files import File
from django.core.exceptions import ValidationError
from django.forms import (
    BaseModelFormSet,
    HiddenInput,
    ModelForm,
    NumberInput,
    Select,
    formset_factory,
    FileInput,
    TextInput,
    Textarea,
    CheckboxInput,
    modelformset_factory,
)
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import DetailView

from judge.highlight_code import highlight_code
from judge.models import (
    Problem,
    ProblemData,
    ProblemTestCase,
    Submission,
    problem_data_storage,
    ProblemSignatureGrader,
)
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin
from judge.widgets.fine_uploader import (
    handle_upload,
    FineUploadFileInput,
    FineUploadForm,
)
from judge.widgets.file_edit import FileEditWidget
from judge.views.problem import ProblemMixin
from judge.logging import log_exception

mimetypes.init()
mimetypes.add_type("application/x-yaml", ".yml")


def checker_args_cleaner(self):
    data = self.cleaned_data["checker_args"]
    if not data or data.isspace():
        return ""
    try:
        if not isinstance(json.loads(data), dict):
            raise ValidationError(_("Checker arguments must be a JSON object"))
    except ValueError:
        raise ValidationError(_("Checker arguments is invalid JSON"))
    return data


class ProblemDataForm(ModelForm):
    def clean_zipfile(self):
        if hasattr(self, "zip_valid") and not self.zip_valid:
            raise ValidationError(_("Your zip file is invalid!"))
        return self.cleaned_data["zipfile"]

    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemData
        fields = [
            "zipfile",
            "generator",
            "generator_script",
            "checker",
            "checker_args",
            "custom_checker",
            "custom_checker_cpp",
            "interactive_judge",
            "fileio_input",
            "fileio_output",
            "output_only",
            "use_ioi_signature",
        ]
        widgets = {
            "zipfile": FineUploadFileInput,
            "generator": FileEditWidget,
            "generator_script": HiddenInput,
            "checker_args": HiddenInput,
            "output_limit": HiddenInput,
            "output_prefix": HiddenInput,
            "fileio_input": TextInput,
            "fileio_output": TextInput,
            "output_only": CheckboxInput,
            "use_ioi_signature": CheckboxInput,
            "custom_checker_cpp": FileEditWidget,
            "custom_checker": FileEditWidget,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["generator"].widget = FileEditWidget(default_file_name="gen.cpp")
        self.fields["custom_checker_cpp"].widget = FileEditWidget(
            default_file_name="checker.cpp"
        )
        self.fields["custom_checker"].widget = FileEditWidget(
            default_file_name="checker.py"
        )
        self.fields["interactive_judge"].widget = FileEditWidget(
            default_file_name="interactive.cpp"
        )


class ProblemCaseForm(ModelForm):
    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemTestCase
        fields = (
            "order",
            "type",
            "input_file",
            "output_file",
            "points",
            "is_pretest",
            "checker",
            "checker_args",
            "generator_args",
        )  # , 'output_limit', 'output_prefix', 'generator_args')
        widgets = {
            "generator_args": TextInput(attrs={"style": "width: 100%"}),
            "type": Select(attrs={"style": "width: 100%"}),
            "points": NumberInput(attrs={"style": "width: 4em"}),
            # 'output_prefix': NumberInput(attrs={'style': 'width: 4.5em'}),
            # 'output_limit': NumberInput(attrs={'style': 'width: 6em'}),
            # 'checker_args': HiddenInput,
        }


class ProblemSignatureGraderForm(ModelForm):
    class Meta:
        model = ProblemSignatureGrader
        fields = ["language", "handler", "header"]


class ProblemSignatureGraderFormSet(BaseModelFormSet):
    def clean(self):
        super().clean()
        languages = []
        for form in self.forms:
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False):
                language = form.cleaned_data["language"]
                if language in languages:
                    form.add_error("language", _("Each language must be unique."))
                languages.append(language)


class ProblemCaseFormSet(
    formset_factory(
        ProblemCaseForm, formset=BaseModelFormSet, extra=1, max_num=1, can_delete=True
    )
):
    model = ProblemTestCase

    def __init__(self, *args, **kwargs):
        self.valid_files = kwargs.pop("valid_files", None)
        super(ProblemCaseFormSet, self).__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        form = super(ProblemCaseFormSet, self)._construct_form(i, **kwargs)
        form.valid_files = self.valid_files
        return form


class ProblemManagerMixin(LoginRequiredMixin, ProblemMixin, DetailView):
    def get_object(self, queryset=None):
        problem = super(ProblemManagerMixin, self).get_object(queryset)
        if problem.is_manually_managed:
            raise Http404()
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()


class ProblemSubmissionDiff(TitleMixin, ProblemMixin, DetailView):
    template_name = "problem/submission-diff.html"

    def get_title(self):
        return _("Comparing submissions for {0}").format(self.object.name)

    def get_content_title(self):
        return format_html(
            _('Comparing submissions for <a href="{1}">{0}</a>'),
            self.object.name,
            reverse("problem_detail", args=[self.object.code]),
        )

    def get_object(self, queryset=None):
        problem = super(ProblemSubmissionDiff, self).get_object(queryset)
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionDiff, self).get_context_data(**kwargs)
        try:
            ids = self.request.GET.getlist("id")
            subs = Submission.objects.filter(id__in=ids)
        except ValueError:
            raise Http404
        if not subs:
            raise Http404

        context["submissions"] = subs

        # If we have associated data we can do better than just guess
        data = ProblemTestCase.objects.filter(dataset=self.object, type="C")
        if data:
            num_cases = data.count()
        else:
            num_cases = subs.first().test_cases.count()
        context["num_cases"] = num_cases
        return context


class ProblemDataView(TitleMixin, ProblemManagerMixin):
    template_name = "problem/data.html"

    def get_title(self):
        return _("Editing data for {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing data for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def get_data_form(self, post=False):
        return ProblemDataForm(
            data=self.request.POST if post else None,
            prefix="problem-data",
            files=self.request.FILES if post else None,
            instance=ProblemData.objects.get_or_create(problem=self.object)[0],
        )

    def get_case_formset(self, files, post=False):
        return ProblemCaseFormSet(
            data=self.request.POST if post else None,
            prefix="cases",
            valid_files=files,
            queryset=ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by(
                "order"
            ),
        )

    def get_signature_grader_formset(self, post=False):
        queryset = ProblemSignatureGrader.objects.filter(problem=self.object)
        existing_count = queryset.count()
        extra_forms = max(3 - existing_count, 0)

        return modelformset_factory(
            ProblemSignatureGrader,
            form=ProblemSignatureGraderForm,
            formset=ProblemSignatureGraderFormSet,
            extra=extra_forms,
            max_num=3,
            can_delete=True,
        )(
            queryset=queryset,
            data=self.request.POST if post else None,
            files=self.request.FILES if post else None,
            prefix="signature-graders",
        )

    def get_valid_files(self, data, post=False):
        try:
            if post and "problem-data-zipfile-clear" in self.request.POST:
                return []
            elif post and "problem-data-zipfile" in self.request.FILES:
                return ZipFile(self.request.FILES["problem-data-zipfile"]).namelist()
            elif data.zipfile:
                return ZipFile(data.zipfile.path).namelist()
        except BadZipfile:
            return []
        except FileNotFoundError as e:
            log_exception(e)
            return []
        return []

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        if "data_form" not in context:
            context["data_form"] = self.get_data_form()
            valid_files = context["valid_files"] = self.get_valid_files(
                context["data_form"].instance
            )
            context["data_form"].zip_valid = valid_files is not False
            context["cases_formset"] = self.get_case_formset(valid_files)
            context["signature_grader_formset"] = self.get_signature_grader_formset()

        context["valid_files_json"] = mark_safe(json.dumps(context["valid_files"]))
        context["valid_files"] = set(context["valid_files"])
        context["all_case_forms"] = chain(
            context["cases_formset"], [context["cases_formset"].empty_form]
        )
        return context

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        data_form = self.get_data_form(post=True)
        valid_files = self.get_valid_files(data_form.instance, post=True)
        data_form.zip_valid = valid_files is not False
        cases_formset = self.get_case_formset(valid_files, post=True)
        signature_grader_formset = self.get_signature_grader_formset(post=True)

        if (
            data_form.is_valid()
            and cases_formset.is_valid()
            and signature_grader_formset.is_valid()
        ):
            data = data_form.save()
            for case in cases_formset.save(commit=False):
                case.dataset_id = problem.id
                case.save()
            for case in cases_formset.deleted_objects:
                case.delete()

            for grader in signature_grader_formset.save(commit=False):
                grader.problem_id = problem.id
                grader.save()
            for grader in signature_grader_formset.deleted_objects:
                grader.delete()

            ProblemDataCompiler.generate(
                problem, data, problem.cases.order_by("order"), valid_files
            )
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(
            self.get_context_data(
                data_form=data_form,
                cases_formset=cases_formset,
                signature_grader_formset=signature_grader_formset,
                valid_files=valid_files,
            )
        )

    put = post


@login_required
def problem_data_file(request, problem, path):
    object = get_object_or_404(Problem, code=problem)
    if not object.is_editable_by(request.user):
        raise Http404()

    response = HttpResponse()
    if hasattr(settings, "DMOJ_PROBLEM_DATA_INTERNAL") and request.META.get(
        "SERVER_SOFTWARE", ""
    ).startswith("nginx/"):
        response["X-Accel-Redirect"] = "%s/%s/%s" % (
            settings.DMOJ_PROBLEM_DATA_INTERNAL,
            problem,
            path,
        )
    else:
        try:
            with problem_data_storage.open(os.path.join(problem, path), "rb") as f:
                response.content = f.read()
        except IOError:
            raise Http404()

    response["Content-Type"] = "application/octet-stream"
    return response


@login_required
def problem_init_view(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    if not request.user.is_superuser and not problem.is_editable_by(request.user):
        raise Http404()

    try:
        with problem_data_storage.open(
            os.path.join(problem.code, "init.yml"), "rb"
        ) as f:
            data = utf8text(f.read()).rstrip("\n")
    except IOError:
        raise Http404()

    return render(
        request,
        "problem/yaml.html",
        {
            "raw_source": data,
            "highlighted_source": highlight_code(data, "yaml", linenos=False),
            "title": _("Generated init.yml for %s") % problem.name,
            "content_title": mark_safe(
                escape(_("Generated init.yml for %s"))
                % (
                    format_html(
                        '<a href="{1}">{0}</a>',
                        problem.name,
                        reverse("problem_detail", args=[problem.code]),
                    )
                )
            ),
        },
    )


class ProblemZipUploadView(ProblemManagerMixin, View):
    def dispatch(self, *args, **kwargs):
        return super(ProblemZipUploadView, self).dispatch(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        problem_data = get_object_or_404(ProblemData, problem=self.object)
        form = FineUploadForm(request.POST, request.FILES)

        if form.is_valid():
            fileuid = form.cleaned_data["qquuid"]
            filename = form.cleaned_data["qqfilename"]
            dest = os.path.join(gettempdir(), fileuid)

            def post_upload():
                zip_dest = os.path.join(dest, filename)
                try:
                    ZipFile(zip_dest).namelist()  # check if this file is valid
                    with open(zip_dest, "rb") as f:
                        problem_data.zipfile.delete()
                        problem_data.zipfile.save(filename, File(f))
                        f.close()
                except Exception as e:
                    raise Exception(e)
                finally:
                    shutil.rmtree(dest)

            try:
                handle_upload(
                    request.FILES["qqfile"],
                    form.cleaned_data,
                    dest,
                    post_upload=post_upload,
                )
            except Exception as e:
                return JsonResponse({"success": False, "error": str(e)})
            return JsonResponse({"success": True})
        else:
            return HttpResponse(status_code=400)
