"""HTTP content mutation boundary: object-local Lock and publication races.

The explicit registry excludes submissions, attempts, comments, and review actions.
Every guarded edit locks its own object; contest locks never propagate to items.
"""

import hashlib
import json

from django.apps import apps
from django.db import transaction
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _

from judge.models import (
    BlogPost,
    Contest,
    ContestPublicRequest,
    ContestReviewRun,
    Problem,
    ProblemReviewRun,
    PublicRequest,
    Quiz,
    QuizQuestion,
)
from judge.auto_review import workflow
from judge.auto_review.context_utils import ReviewInputError
from judge.auto_review.publication import withdraw_pending_locked
from judge.auto_review.execution_freshness import (
    bump_package_revisions,
    bump_review_files,
)
from judge.widgets.direct_upload import get_upload_token_data

PROBLEM_MUTATIONS = frozenset(
    {
        "problem_edit",
        "problem_edit_language_limits",
        "problem_edit_language_templates",
        "problem_edit_solutions",
        "problem_edit_translations",
        "problem_data",
        "problem_data_init",
        "problem_zip_upload",
        "problem_validator_save",
        "problem_solution_codes_save",
        "problem_package_import_apply",
        "problem_attachment_upload",
        "problem_attachment_update",
        "problem_attachment_delete",
        "problem_attachment_reorder",
    }
)
CONTEST_MUTATIONS = frozenset({"contest_edit", "organization_contest_edit"})
QUIZ_MUTATIONS = frozenset(
    {
        "quiz_edit",
        "quiz_delete",
        "quiz_add_question",
        "quiz_remove_question",
        "quiz_reorder_questions",
        "quiz_update_points",
    }
)
BLOG_MUTATIONS = frozenset({"edit_blog_post", "edit_organization_blog"})
QUESTION_MUTATIONS = frozenset({"question_bank_edit", "question_bank_delete"})
DIRECT_MUTATIONS = frozenset({"direct_upload_save", "direct_upload_delete"})
ADMIN_OBJECTS = {
    "problem": Problem,
    "contest": Contest,
    "quiz": Quiz,
    "quizquestion": QuizQuestion,
    "blogpost": BlogPost,
}
DEFERRED_CONFIRMATION = frozenset(
    {
        "problem_edit",
        "problem_edit_language_limits",
        "problem_edit_language_templates",
        "problem_edit_solutions",
        "problem_edit_translations",
        "contest_edit",
        "organization_contest_edit",
        "problem_solution_codes_save",
    }
)
VIEW_GUARDED_CONFIRMATION = frozenset({"problem_data", "problem_validator_save"})


def _body(request):
    if request.content_type == "application/json":
        try:
            parsed = json.loads(request.body)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST


def mutation_target(request, match):
    name, kwargs = match.url_name, match.kwargs
    if match.app_name == "admin":
        for label, model in ADMIN_OBJECTS.items():
            if name in (
                "judge_%s_change" % label,
                "judge_%s_delete" % label,
                "judge_%s_revision" % label,
            ):
                return model, {"pk": kwargs["object_id"]}
    if name in PROBLEM_MUTATIONS:
        return Problem, {"code": kwargs["problem"]}
    if name in CONTEST_MUTATIONS:
        return Contest, {"key": kwargs["contest"]}
    if name in QUIZ_MUTATIONS:
        return Quiz, {"code": kwargs["code"]}
    if name in QUESTION_MUTATIONS:
        return QuizQuestion, {"pk": kwargs["pk"]}
    if name in BLOG_MUTATIONS:
        return BlogPost, {"pk": kwargs.get("id", kwargs.get("blog_pk"))}
    if name in DIRECT_MUTATIONS:
        token = get_upload_token_data(_body(request).get("upload_token", ""))
        if token and token.get("profile_id") == request.profile.pk:
            try:
                model = apps.get_model(token["model_name"])
            except (LookupError, ValueError):
                return None
            if model in (Problem, Contest, Quiz, QuizQuestion, BlogPost):
                return model, {"pk": token["object_id"]}
    return None


def _request_row(obj):
    if isinstance(obj, Problem):
        return (
            PublicRequest.objects.select_for_update()
            .filter(problem=obj, status=PublicRequest.PENDING)
            .first()
        )
    if isinstance(obj, Contest):
        return (
            ContestPublicRequest.objects.select_for_update()
            .filter(contest=obj, status=ContestPublicRequest.PENDING)
            .first()
        )
    return None


def publication_confirmation_token(pending):
    if pending is None:
        return ""
    identity = "%s:%s:%s" % (pending.pk, pending.review_run_id, pending.candidate_hash)
    return hashlib.sha256(identity.encode()).hexdigest()


def _review_impact(obj):
    """Identifiers for a confirmation, never authority to bypass edit permission."""
    if isinstance(obj, Problem):
        problem_ids = list(
            ProblemReviewRun.objects.filter(
                problem=obj, status__in=workflow.ACTIVE
            ).values_list("pk", flat=True)
        )
        contest_ids = list(
            ContestReviewRun.objects.filter(
                child_dependencies__problem=obj, status__in=workflow.ACTIVE
            )
            .distinct()
            .values_list("pk", flat=True)
        )
    elif isinstance(obj, Contest):
        problem_ids = []
        contest_ids = list(
            ContestReviewRun.objects.filter(
                contest=obj, status__in=workflow.ACTIVE
            ).values_list("pk", flat=True)
        )
    else:
        return {"problems": [], "contests": [], "children": []}
    children = list(
        ProblemReviewRun.objects.filter(
            parent_dependencies__contest_run_id__in=contest_ids,
            parent_dependencies__owned=True,
            status__in=workflow.ACTIVE,
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    return {
        "problems": sorted(problem_ids),
        "contests": sorted(contest_ids),
        "children": sorted(children),
    }


def _save_confirmation_token(pending, impact):
    if not any(impact.values()):
        return publication_confirmation_token(pending)
    return hashlib.sha256(
        json.dumps(
            {"publication": publication_confirmation_token(pending), "reviews": impact},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _publication_change_confirmed(request, pending, impact=None):
    data = _body(request)
    consent = (
        str(data.get("confirm_withdraw_publication", "")) == "1"
        or request.headers.get("X-Confirm-Withdraw-Publication") == "1"
    )
    token = data.get("publication_confirmation_token", "") or request.headers.get(
        "X-Publication-Confirmation-Token", ""
    )
    return consent and token == _save_confirmation_token(pending, impact or {})


def publication_change_confirmation(request, obj, changed):
    """Call after validation and before any irreversible file write."""
    impact = _review_impact(obj)
    pending = _request_row(obj)
    if any(impact.values()) or (changed and pending):
        confirmed = _publication_change_confirmed(request, pending, impact)
        if not confirmed:
            return ContentMutationMiddleware._confirmation(pending, request, impact)
    return None


def _save_succeeded(response):
    if 300 <= response.status_code < 400:
        return True
    if response.status_code >= 400:
        return False
    if response.get("Content-Type", "").startswith("application/json"):
        try:
            data = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(data, dict) and (
            data.get("success") is True or data.get("status") == "ok"
        )
    # Bound HTML forms return 200 on validation failure. Do not cancel merely
    # because the server rendered an editor successfully.
    return False


def _bump_editor_revisions(obj, name, request):
    if not isinstance(obj, Problem):
        return
    if name == "problem_data" and getattr(request, "_review_pretest_only", False):
        return
    groups = {
        "problem_data": {"test", "validator", "judging"},
        "problem_data_init": {"test", "validator", "judging"},
        "problem_zip_upload": {"test"},
        "problem_validator_save": {"validator"},
        "problem_package_import_apply": {"test", "validator", "judging"},
    }.get(name, set())
    if name in DIRECT_MUTATIONS:
        token = get_upload_token_data(_body(request).get("upload_token", "")) or {}
        field = token.get("field_name", "")
        if field in {"zipfile", "generator"}:
            groups = {"test"}
        elif field == "testcase_validator":
            groups = {"validator"}
        elif field == "pdf_description":
            bump_review_files(obj.pk)
        elif field:
            groups = {"judging"}
    bump_package_revisions(obj.pk, groups)
    if name in {
        "problem_edit_language_templates",
        "problem_edit_translations",
    } or request.FILES.get("pdf_description"):
        bump_review_files(obj.pk)


def _read_content_hash(obj):
    try:
        return workflow.current_content_hash(obj)
    except (ReviewInputError, OSError, ValueError):
        # An unavailable identity requires consent; it must not prevent an
        # editor from repairing the files that made the identity unreadable.
        return None


class ContentMutationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self._process(request)
        # A changed, unconfirmed Save rolls its transaction back. Render the
        # normal page only after leaving that transaction: context processors
        # legitimately query navigation/profile data while rendering base.html.
        if isinstance(response, TemplateResponse) and not response.is_rendered:
            response.render()
        return response

    def _process(self, request):
        if (
            request.method not in ("POST", "PUT", "PATCH", "DELETE")
            or not request.user.is_authenticated
        ):
            return self.get_response(request)
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        target = mutation_target(request, match)
        if match.app_name == "admin" and not request.user.is_superuser:
            for label, model in ADMIN_OBJECTS.items():
                if match.url_name == "judge_%s_changelist" % label:
                    ids = request.POST.getlist("_selected_action")
                    if model.objects.filter(pk__in=ids, is_locked=True).exists():
                        return JsonResponse(
                            {
                                "success": False,
                                "error": _(
                                    "Locked content cannot be changed or deleted by this account."
                                ),
                            },
                            status=403,
                        )
        if target is None:
            return self.get_response(request)
        model, lookup = target
        with transaction.atomic():
            obj = model.objects.select_for_update().filter(**lookup).first()
            if obj is None:
                return self.get_response(request)
            if obj.is_locked and not request.user.is_superuser:
                return JsonResponse(
                    {
                        "success": False,
                        "error": _(
                            "This content is locked. Contact a superuser to request an edit."
                        ),
                    },
                    status=403,
                )
            # This contest-editor request only previews the effect of removing
            # rows; it does not save content or cancel a running review.
            if (
                match.url_name in CONTEST_MUTATIONS
                and request.POST.get("preview_contest_problem_submission_delete") == "1"
            ):
                return self.get_response(request)
            pending = _request_row(obj)
            impact = _review_impact(obj)
            if request.headers.get("X-Review-Save-Probe") == "1" and match.url_name in {
                "problem_edit",
                "contest_edit",
                "organization_contest_edit",
            }:
                if not (request.user.is_superuser or obj.is_editable_by(request.user)):
                    return JsonResponse({"error": _("Permission denied.")}, status=403)
                if any(impact.values()):
                    return self._confirmation(pending, request, impact)
                return JsonResponse({"requires_review_confirmation": False})
            binding_enabled = pending
            track_freshness = isinstance(obj, (Problem, Contest))
            confirmed = _publication_change_confirmed(request, pending, impact)
            if match.url_name in DIRECT_MUTATIONS and not (
                obj.is_editable_by(request.user)
                or request.user.has_perm(
                    "judge.edit_all_problem"
                    if isinstance(obj, Quiz)
                    else (
                        "judge.edit_all_post"
                        if isinstance(obj, BlogPost)
                        else (
                            "judge.edit_all_contest"
                            if isinstance(obj, Contest)
                            else "judge.edit_all_problem"
                        )
                    )
                )
                or (
                    hasattr(obj, "get_organizations")
                    and any(
                        request.profile.can_edit_organization(org)
                        for org in obj.get_organizations()
                    )
                )
            ):
                return JsonResponse(
                    {
                        "success": False,
                        "error": _(
                            "Your permission to edit this content has changed. Reload the editor."
                        ),
                    },
                    status=403,
                )
            if binding_enabled or any(impact.values()):
                deferred = match.url_name in VIEW_GUARDED_CONFIRMATION or (
                    match.url_name in DEFERRED_CONFIRMATION and not request.FILES
                )
                if not confirmed and (any(impact.values()) or not deferred):
                    # The underlying view remains the authority. Do not reveal
                    # candidate identifiers to a caller it has not authorized.
                    can_confirm = (
                        obj.is_editable_by(request.user) or request.user.is_superuser
                    )
                    if match.app_name == "admin":
                        permission = "judge.edit_all_" + (
                            "contest"
                            if isinstance(obj, Contest)
                            else "post" if isinstance(obj, BlogPost) else "problem"
                        )
                        can_confirm |= request.user.has_perm(permission)
                    if not can_confirm:
                        return self.get_response(request)
                    return self._confirmation(pending, request, impact)
            before = _read_content_hash(obj) if binding_enabled else None
            response = self.get_response(request)
            if _save_succeeded(response):
                if not model.objects.filter(pk=obj.pk).exists():
                    return response
                if isinstance(obj, (Problem, Contest)):
                    # A successful relevant Save cancels irrespective of value
                    # equality. The confirmation is tied to the active run set.
                    if any(impact.values()):
                        workflow.cancel_for_save(obj, request.user)
                    _bump_editor_revisions(obj, match.url_name, request)
                obj.refresh_from_db()
                if binding_enabled and (
                    before is None or _read_content_hash(obj) != before
                ):
                    if binding_enabled and not confirmed:
                        transaction.set_rollback(True)
                        return self._confirmation(pending, request, impact)
                    if binding_enabled:
                        withdraw_pending_locked(
                            obj,
                            request.user,
                            _("Withdrawn after a confirmed content change."),
                        )
                if track_freshness:
                    transaction.on_commit(lambda: workflow.invalidate_readiness(obj))
            return response

    @staticmethod
    def _confirmation(pending, request, impact=None):
        impact = impact or {}
        messages = []
        if impact.get("problems"):
            messages.append(_("Saving will cancel this problem's running review."))
        if impact.get("contests"):
            messages.append(_("Saving will cancel affected running contest reviews."))
        if impact.get("children"):
            messages.append(
                _(
                    "Unfinished problem reviews started by those contest reviews will also be cancelled."
                )
            )
        if pending:
            messages.append(
                _("Saving changes will withdraw the pending publication request.")
            )
        message = " ".join(messages)
        token = _save_confirmation_token(pending, impact)
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.content_type == "application/json"
            or "application/json" in request.headers.get("Accept", "")
            or request.method != "POST"
        )
        if not wants_json:
            excluded = {
                "csrfmiddlewaretoken",
                "confirm_withdraw_publication",
                "publication_confirmation_token",
            }
            response = TemplateResponse(
                request,
                "auto_review/publication_save_confirmation.html",
                {
                    "title": (
                        _("Save and cancel reviews?")
                        if any(impact.values())
                        else _("Confirm changes")
                    ),
                    "confirmation_message": message,
                    "save_url": request.get_full_path(),
                    "publication_confirmation_token": token,
                    "submitted_fields": [
                        (name, value)
                        for name, values in request.POST.lists()
                        if name not in excluded
                        for value in values
                    ],
                    "submitted_files": [
                        {"field": name, "names": [file.name for file in files]}
                        for name, files in request.FILES.lists()
                    ],
                },
                status=409,
            )
            response["Cache-Control"] = "private, no-store"
            return response
        return JsonResponse(
            {
                "success": False,
                "requires_publication_confirmation": True,
                "requires_review_confirmation": any(impact.values()),
                "publication_request_id": pending.pk if pending else None,
                "publication_confirmation_token": token,
                "error": message,
            },
            status=409,
        )
