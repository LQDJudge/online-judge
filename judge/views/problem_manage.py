from operator import itemgetter

import zipfile, tempfile

from celery.result import AsyncResult
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, ngettext
from django.views.generic import DetailView
from django.views.generic.detail import BaseDetailView

from judge.models import Language, Submission
from judge.tasks import apply_submission_filter, rejudge_problem_filter, rescore_problem
from judge.utils.celery import redirect_to_task_status
from judge.utils.views import TitleMixin
from judge.views.problem import ProblemMixin


class ManageProblemSubmissionMixin(ProblemMixin):
    def get_object(self, queryset=None):
        problem = super().get_object(queryset)
        user = self.request.user
        if not problem.is_subs_manageable_by(user):
            raise Http404()
        return problem


class ManageProblemSubmissionActionMixin(ManageProblemSubmissionMixin):
    def perform_action(self):
        raise NotImplementedError()

    def get(self, request, *args, **kwargs):
        raise Http404()

    def post(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except Http404:
            return self.no_such_problem()
        else:
            return self.perform_action()


class ManageProblemSubmissionView(TitleMixin, ManageProblemSubmissionMixin, DetailView):
    template_name = "problem/manage_submission.html"

    def get_title(self):
        return _("Managing submissions for %s") % (self.object.name,)

    def get_content_title(self):
        return mark_safe(
            escape(_("Managing submissions for %s"))
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
        context["submission_count"] = self.object.submission_set.count()
        context["languages"] = [
            (lang_id, short_name or key)
            for lang_id, key, short_name in Language.objects.values_list(
                "id", "key", "short_name"
            )
        ]
        context["results"] = sorted(map(itemgetter(0), Submission.RESULT))
        context["current_contest"] = None
        if (
            self.request.in_contest_mode
            and self.object in self.request.participation.contest.problems.all()
        ):
            context["current_contest"] = self.request.participation.contest

        return context


class BaseActionSubmissionsView(
    PermissionRequiredMixin, ManageProblemSubmissionActionMixin, BaseDetailView
):
    permission_required = "judge.rejudge_submission_lot"

    def perform_action(self):
        if self.request.POST.get("use_range", "off") == "on":
            try:
                start = int(self.request.POST.get("start"))
                end = int(self.request.POST.get("end"))
            except (KeyError, ValueError):
                return HttpResponseBadRequest()
            id_range = (start, end)
        else:
            id_range = None

        try:
            languages = list(map(int, self.request.POST.getlist("language")))
            results = list(map(str, self.request.POST.getlist("result")))
            contests = list(map(int, self.request.POST.getlist("contest")))
        except ValueError:
            return HttpResponseBadRequest()

        return self.generate_response(id_range, languages, results, contests)

    def generate_response(self, id_range, languages, results, contest):
        raise NotImplementedError()


class ActionSubmissionsView(BaseActionSubmissionsView):
    def rejudge_response(self, id_range, languages, results, contest):
        status = rejudge_problem_filter.delay(
            self.object.id, id_range, languages, results, contest
        )
        return redirect_to_task_status(
            status,
            message=_("Rejudging selected submissions for %s...") % (self.object.name,),
            redirect=reverse(
                "problem_submissions_rejudge_success",
                args=[self.object.code, status.id],
            ),
        )

    def download_response(self, id_range, languages, results, contest):
        queryset = Submission.objects.filter(problem_id=self.object.id)
        submissions = apply_submission_filter(
            queryset, id_range, languages, results, contest
        )

        with tempfile.SpooledTemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for submission in submissions:
                    file_name = (
                        str(submission.id)
                        + "_"
                        + str(submission.user)
                        + "."
                        + str(submission.language.key)
                    )
                    archive.writestr(file_name, submission.source.source)

            # Reset file pointer
            tmp.seek(0)

            # Write file data to response
            response = HttpResponse(
                tmp.read(), content_type="application/x-zip-compressed"
            )
            response["Content-Disposition"] = 'attachment; filename="%s"' % (
                str(self.object.code) + "_submissions.zip"
            )
            return response

    def generate_response(self, id_range, languages, results, contest):
        action = self.request.POST.get("action")
        if action == "rejudge":
            return self.rejudge_response(id_range, languages, results, contest)
        elif action == "download":
            return self.download_response(id_range, languages, results, contest)
        else:
            return Http404()


class PreviewActionSubmissionsView(BaseActionSubmissionsView):
    def generate_response(self, id_range, languages, results, contest):
        queryset = apply_submission_filter(
            self.object.submission_set.all(), id_range, languages, results, contest
        )
        return HttpResponse(str(queryset.count()))


class RescoreAllSubmissionsView(ManageProblemSubmissionActionMixin, BaseDetailView):
    def perform_action(self):
        status = rescore_problem.delay(self.object.id)
        return redirect_to_task_status(
            status,
            message=_("Rescoring all submissions for %s...") % (self.object.name,),
            redirect=reverse(
                "problem_submissions_rescore_success",
                args=[self.object.code, status.id],
            ),
        )


def rejudge_success(request, problem, task_id):
    count = AsyncResult(task_id).result
    if not isinstance(count, int):
        raise Http404()
    messages.success(
        request,
        ngettext(
            "Successfully scheduled %d submission for rejudging.",
            "Successfully scheduled %d submissions for rejudging.",
            count,
        )
        % (count,),
    )
    return HttpResponseRedirect(reverse("problem_manage_submissions", args=[problem]))


def rescore_success(request, problem, task_id):
    count = AsyncResult(task_id).result
    if not isinstance(count, int):
        raise Http404()
    messages.success(
        request,
        ngettext(
            "%d submission were successfully rescored.",
            "%d submissions were successfully rescored.",
            count,
        )
        % (count,),
    )
    return HttpResponseRedirect(reverse("problem_manage_submissions", args=[problem]))
