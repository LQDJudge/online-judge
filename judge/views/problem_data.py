import json
import mimetypes
import os
import uuid
from itertools import chain
import shutil
from tempfile import gettempdir
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.views.generic import View

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.forms import (
    BaseModelFormSet,
    HiddenInput,
    ModelForm,
    NumberInput,
    Select,
    formset_factory,
    TextInput,
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
    ProblemValidation,
    ProblemValidationResult,
    ProblemSolutionCode,
    Submission,
    SubmissionSource,
    problem_data_storage,
    ProblemSignatureGrader,
    Language,
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

mimetypes.init()
mimetypes.add_type("application/x-yaml", ".yml")

# 50 MB limit for non-superusers uploading test data
MAX_TESTDATA_FILE_SIZE = 50 * 1024 * 1024


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

    def clean_generator(self):
        generator = self.cleaned_data.get("generator")
        if generator and hasattr(generator, "name"):
            if not generator.name.endswith(".cpp"):
                raise ValidationError(_("Generator file must be a .cpp file."))
        return generator

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
            "testcase_validator",
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
        self.fields["generator"].widget = FileEditWidget(
            default_file_name="gen.cpp", accept=".cpp"
        )
        self.fields["custom_checker_cpp"].widget = FileEditWidget(
            default_file_name="checker.cpp"
        )
        self.fields["custom_checker"].widget = FileEditWidget(
            default_file_name="checker.py"
        )
        self.fields["interactive_judge"].widget = FileEditWidget(
            default_file_name="interactive.cpp"
        )
        self.fields["testcase_validator"].widget = FileEditWidget(
            default_file_name="validator.cpp"
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
        except FileNotFoundError:
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
        # Pass file size limit to template (0 = unlimited for superusers)
        if self.request.user.is_superuser:
            context["max_file_size"] = 0
        else:
            context["max_file_size"] = MAX_TESTDATA_FILE_SIZE
        context["max_file_size_mb"] = MAX_TESTDATA_FILE_SIZE // (1024 * 1024)
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
        self.object = self.get_object()
        problem_data = get_object_or_404(ProblemData, problem=self.object)
        form = FineUploadForm(request.POST, request.FILES)

        if form.is_valid():
            # Server-side file size validation for non-superusers
            # Check on every chunk request to reject early before saving any data
            total_size = form.cleaned_data.get("qqtotalfilesize") or 0
            if not request.user.is_superuser and total_size > MAX_TESTDATA_FILE_SIZE:
                # Clean up any existing chunks for this upload
                fileuid = form.cleaned_data["qquuid"]
                chunks_dir = getattr(settings, "CHUNK_UPLOAD_DIR", gettempdir())
                chunk_folder = os.path.join(chunks_dir, fileuid)
                if os.path.exists(chunk_folder):
                    shutil.rmtree(chunk_folder)
                return JsonResponse(
                    {
                        "success": False,
                        "error": _(
                            "File size exceeds 50 MB limit. Contact admin for larger uploads."
                        ),
                    }
                )
            fileuid = form.cleaned_data["qquuid"]
            filename = form.cleaned_data["qqfilename"]
            dest = os.path.join(gettempdir(), fileuid)

            def post_upload():
                zip_dest = os.path.join(dest, filename)
                try:
                    # Check actual file size (can't be faked like qqtotalfilesize)
                    actual_size = os.path.getsize(zip_dest)
                    if (
                        not request.user.is_superuser
                        and actual_size > MAX_TESTDATA_FILE_SIZE
                    ):
                        raise Exception(
                            _(
                                "File size exceeds 50 MB limit. Contact admin for larger uploads."
                            )
                        )

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


class ProblemValidatorView(TitleMixin, ProblemManagerMixin):
    template_name = "problem/validator.html"

    def get_title(self):
        return _("Testcase Validator for {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Testcase Validator for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        problem = self.object
        data = ProblemData.objects.filter(problem=problem).first()
        context["has_validator"] = data and data.testcase_validator

        # Validator code content for editing
        validator_code = ""
        validator_language = "cpp"
        if data:
            validator_language = data.testcase_validator_language or "cpp"
            if data.testcase_validator:
                try:
                    with data.testcase_validator.open("r") as f:
                        validator_code = f.read()
                except Exception:
                    validator_code = ""
        context["validator_code"] = validator_code
        context["validator_language"] = validator_language
        context["ACE_URL"] = settings.ACE_URL

        # Get latest validation
        latest = (
            ProblemValidation.objects.filter(problem=problem).order_by("-date").first()
        )
        context["latest_validation"] = latest
        if latest:
            context["validation_results"] = list(
                ProblemValidationResult.objects.filter(validation=latest).order_by(
                    "case"
                )
            )
        else:
            context["validation_results"] = []

        return context


class ProblemValidatorSaveView(ProblemManagerMixin, View):
    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse(
                {"status": "error", "message": _("Invalid JSON.")}, status=400
            )

        code = body.get("code", "")
        language = body.get("language", "cpp")
        if language not in ("cpp", "python"):
            return JsonResponse(
                {"status": "error", "message": _("Invalid language.")}, status=400
            )

        data, _ = ProblemData.objects.get_or_create(problem=problem)

        ext = "cpp" if language == "cpp" else "py"
        filename = "validator." + ext

        # Save the validator file
        if data.testcase_validator:
            data.testcase_validator.delete(save=False)
        data.testcase_validator.save(filename, ContentFile(code.encode("utf-8")))
        data.testcase_validator_language = language
        data.save()

        # Regenerate init.yml
        valid_files = []
        try:
            if data.zipfile:
                valid_files = ZipFile(data.zipfile.path).namelist()
        except (BadZipfile, FileNotFoundError):
            pass
        ProblemDataCompiler.generate(
            problem, data, problem.cases.order_by("order"), valid_files
        )

        return JsonResponse({"status": "ok"})


MAX_PENDING_VALIDATIONS_PER_USER = 3


class ValidateTestCasesView(ProblemManagerMixin, View):
    def post(self, request, *args, **kwargs):
        from judge.judgeapi import validate_testcases

        problem = self.get_object()
        profile = request.profile

        # Check if validation already running for this problem
        if ProblemValidation.objects.filter(
            problem=problem, status__in=("P", "V")
        ).exists():
            return JsonResponse(
                {
                    "status": "error",
                    "message": _("Validation already in progress for this problem."),
                },
                status=409,
            )

        # Check per-user rate limit
        pending_count = ProblemValidation.objects.filter(
            user=profile, status__in=("P", "V")
        ).count()
        if pending_count >= MAX_PENDING_VALIDATIONS_PER_USER:
            return JsonResponse(
                {
                    "status": "error",
                    "message": _(
                        "You have %(count)d validations running. "
                        "Max %(max)d allowed."
                    )
                    % {
                        "count": pending_count,
                        "max": MAX_PENDING_VALIDATIONS_PER_USER,
                    },
                },
                status=429,
            )

        validate_id = str(uuid.uuid4())
        validation = ProblemValidation.objects.create(
            problem=problem,
            validate_id=validate_id,
            user=profile,
            status="P",
        )

        success = validate_testcases(problem.code, validate_id)
        if not success:
            validation.status = "E"
            validation.error = "No judges available"
            validation.save()
            return JsonResponse(
                {"status": "error", "message": _("No judges available.")},
                status=503,
            )

        return JsonResponse({"status": "ok", "validate_id": validate_id})


class ValidateTestCasesStatusView(ProblemManagerMixin, View):
    def get(self, request, *args, **kwargs):
        problem = self.get_object()
        validation = (
            ProblemValidation.objects.filter(problem=problem).order_by("-date").first()
        )

        if not validation:
            return JsonResponse({"is_running": False, "status": None, "results": []})

        is_running = validation.status in ("P", "V")
        results = list(
            ProblemValidationResult.objects.filter(validation=validation)
            .order_by("case")
            .values("case", "batch", "status", "feedback")
        )

        status_data = {
            "status": validation.get_status_display(),
            "total_cases": validation.total_cases,
            "passed": validation.passed,
            "failed_count": validation.failed_count,
            "error": validation.error,
        }

        return JsonResponse(
            {
                "is_running": is_running,
                "status": status_data,
                "results": results,
            }
        )


MAX_SOLUTION_CODES = 6
MAX_PENDING_SOLUTION_RUNS = 3


class ProblemSolutionCodesView(TitleMixin, ProblemManagerMixin):
    template_name = "problem/solution_codes.html"

    def get_title(self):
        return _("Solution Codes for {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Solution Codes for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        problem = self.object
        context["solution_codes"] = (
            ProblemSolutionCode.objects.filter(problem=problem)
            .select_related("language", "last_submission")
            .order_by("order")
        )
        context["max_solution_codes"] = MAX_SOLUTION_CODES
        context["languages"] = list(
            Language.objects.filter(judges__online=True)
            .distinct()
            .values("id", "name", "key", "ace")
        )
        context["expected_result_choices"] = ProblemSolutionCode.EXPECTED_RESULT_CHOICES
        context["ACE_URL"] = settings.ACE_URL
        return context


class ProblemSolutionCodesSaveView(ProblemManagerMixin, View):
    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse(
                {"status": "error", "message": _("Invalid JSON.")}, status=400
            )

        if not isinstance(data, list):
            return JsonResponse(
                {"status": "error", "message": _("Expected a list.")}, status=400
            )

        if len(data) > MAX_SOLUTION_CODES:
            return JsonResponse(
                {
                    "status": "error",
                    "message": _("Maximum %d solution codes allowed.")
                    % MAX_SOLUTION_CODES,
                },
                status=400,
            )

        valid_expected = {c[0] for c in ProblemSolutionCode.EXPECTED_RESULT_CHOICES}
        valid_language_ids = set(Language.objects.values_list("id", flat=True))

        entries = []
        for i, entry in enumerate(data):
            name = entry.get("name", "").strip()[:128]
            source_code = entry.get("source_code", "").strip()
            language_id = entry.get("language_id")
            expected_result = entry.get("expected_result", "")

            if not source_code:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": _("Code #%d has empty source.") % (i + 1),
                    },
                    status=400,
                )
            if language_id not in valid_language_ids:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": _("Code #%d has invalid language.") % (i + 1),
                    },
                    status=400,
                )
            if expected_result not in valid_expected:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": _("Code #%d has invalid expected result.") % (i + 1),
                    },
                    status=400,
                )
            entries.append(
                {
                    "order": i,
                    "name": name,
                    "source_code": source_code,
                    "language_id": language_id,
                    "expected_result": expected_result,
                }
            )

        # Update existing, create new, delete extras
        existing = list(
            ProblemSolutionCode.objects.filter(problem=problem).order_by("order")
        )
        for i, entry in enumerate(entries):
            if i < len(existing):
                sc = existing[i]
                # Clear last_submission if source/language/expected changed
                code_changed = (
                    sc.source_code != entry["source_code"]
                    or sc.language_id != entry["language_id"]
                    or sc.expected_result != entry["expected_result"]
                )
                if code_changed:
                    sc.last_submission = None
                sc.order = entry["order"]
                sc.name = entry["name"]
                sc.source_code = entry["source_code"]
                sc.language_id = entry["language_id"]
                sc.expected_result = entry["expected_result"]
                sc.save()
            else:
                ProblemSolutionCode.objects.create(problem=problem, **entry)
        # Delete extras
        for sc in existing[len(entries) :]:
            sc.delete()

        return JsonResponse({"status": "ok"})


class ProblemSolutionCodesRunView(ProblemManagerMixin, View):
    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        codes = ProblemSolutionCode.objects.filter(problem=problem).order_by("order")

        if not codes.exists():
            return JsonResponse(
                {"status": "error", "message": _("No solution codes to run.")},
                status=400,
            )

        # Block concurrent: check if any last_submission is still in progress
        in_progress_ids = (
            codes.filter(last_submission__status__in=("QU", "P", "G"))
            .exclude(last_submission=None)
            .values_list("last_submission_id", flat=True)
        )
        if in_progress_ids:
            return JsonResponse(
                {
                    "status": "error",
                    "message": _("A run is already in progress."),
                },
                status=409,
            )

        # Per-user limit across all problems
        pending_count = Submission.objects.filter(
            user=request.profile,
            status__in=("QU", "P", "G"),
            id__in=ProblemSolutionCode.objects.exclude(
                last_submission=None
            ).values_list("last_submission_id", flat=True),
        ).count()
        if pending_count >= MAX_PENDING_SOLUTION_RUNS:
            return JsonResponse(
                {
                    "status": "error",
                    "message": _("Too many pending solution code runs."),
                },
                status=429,
            )

        submission_ids = []
        for sc in codes:
            sub = Submission.objects.create(
                user=request.profile,
                problem=problem,
                language=sc.language,
            )
            SubmissionSource.objects.create(submission=sub, source=sc.source_code)
            sc.last_submission = sub
            sc.save(update_fields=["last_submission"])
            sub.judge(rejudge=False, batch_rejudge=True)
            submission_ids.append(sub.id)

        return JsonResponse({"status": "ok", "submission_ids": submission_ids})


class ProblemSolutionCodesStatusView(ProblemManagerMixin, View):
    def get(self, request, *args, **kwargs):
        problem = self.get_object()
        codes = (
            ProblemSolutionCode.objects.filter(problem=problem)
            .select_related("last_submission", "language")
            .order_by("order")
        )

        all_graded = True
        results = []
        for sc in codes:
            sub = sc.last_submission
            if sub is None:
                results.append(
                    {
                        "order": sc.order,
                        "name": sc.name,
                        "expected": sc.expected_result,
                        "language": sc.language.name,
                        "status": None,
                        "result": None,
                        "is_graded": False,
                        "match": None,
                        "submission_id": None,
                        "case_points": 0,
                        "case_total": 0,
                        "time": None,
                        "memory": None,
                    }
                )
                all_graded = False
                continue

            is_graded = sub.status == "D"
            if not is_graded:
                all_graded = False

            # Determine match
            match = None
            if is_graded and sub.result:
                match = sub.result == sc.expected_result

            results.append(
                {
                    "order": sc.order,
                    "name": sc.name,
                    "expected": sc.expected_result,
                    "language": sc.language.name,
                    "status": sub.status,
                    "result": sub.result,
                    "is_graded": is_graded,
                    "match": match,
                    "submission_id": sub.id,
                    "case_points": sub.case_points,
                    "case_total": sub.case_total,
                    "time": sub.time,
                    "memory": sub.memory,
                }
            )

        return JsonResponse({"all_graded": all_graded, "results": results})
