"""HTTP content mutation boundary: object-local Lock and publication races.

The explicit registry excludes submissions, attempts, comments, and review actions.
Every guarded edit locks its own object; contest locks never propagate to items.
"""

import hashlib
import json

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _

from judge.models import (
    BlogPost,
    Contest,
    ContestPublicRequest,
    Problem,
    PublicRequest,
    Quiz,
    QuizQuestion,
)
from judge.review import workflow
from judge.review.publication import withdraw_pending_locked
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


def _publication_change_confirmed(request, pending):
    data = _body(request)
    consent = (
        str(data.get("confirm_withdraw_publication", "")) == "1"
        or request.headers.get("X-Confirm-Withdraw-Publication") == "1"
    )
    token = data.get("publication_confirmation_token", "") or request.headers.get(
        "X-Publication-Confirmation-Token", ""
    )
    return consent and token == publication_confirmation_token(pending)


def publication_change_confirmation(request, obj, changed):
    """Call after validation and before any irreversible file write."""
    if not changed:
        return None
    pending = _request_row(obj)
    if pending and (
        pending.review_run_id
        or getattr(settings, "AUTO_REVIEW_COORDINATOR_ENABLED", False)
    ):
        confirmed = _publication_change_confirmed(request, pending)
        if not confirmed:
            return ContentMutationMiddleware._confirmation(pending, request)
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
            pending = _request_row(obj)
            binding_enabled = pending and (
                pending.review_run_id
                or getattr(settings, "AUTO_REVIEW_COORDINATOR_ENABLED", False)
            )
            track_freshness = isinstance(obj, (Problem, Contest)) and getattr(
                settings, "AUTO_REVIEW_COORDINATOR_ENABLED", False
            )
            confirmed = _publication_change_confirmed(request, pending)
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
            if binding_enabled:
                deferred = match.url_name in VIEW_GUARDED_CONFIRMATION or (
                    match.url_name in DEFERRED_CONFIRMATION and not request.FILES
                )
                if not confirmed and not deferred:
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
                    return self._confirmation(pending, request)
            before = (
                workflow.current_content_hash(obj)
                if binding_enabled or track_freshness
                else None
            )
            response = self.get_response(request)
            if (binding_enabled or track_freshness) and response.status_code < 400:
                if not model.objects.filter(pk=obj.pk).exists():
                    return response
                obj.refresh_from_db()
                if workflow.current_content_hash(obj) != before:
                    if binding_enabled and not confirmed:
                        transaction.set_rollback(True)
                        return self._confirmation(pending, request)
                    if binding_enabled:
                        withdraw_pending_locked(
                            obj,
                            request.user,
                            _("Withdrawn after a confirmed content change."),
                        )
                    transaction.on_commit(lambda: workflow.invalidate_readiness(obj))
            return response

    @staticmethod
    def _confirmation(pending, request):
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
                "review/publication_save_confirmation.html",
                {
                    "title": _("Confirm changes"),
                    "save_url": request.get_full_path(),
                    "publication_confirmation_token": publication_confirmation_token(
                        pending
                    ),
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
                "publication_request_id": pending.pk,
                "publication_confirmation_token": publication_confirmation_token(
                    pending
                ),
                "error": _(
                    "Saving changes will withdraw the pending publication request. Confirm to save the changed draft."
                ),
            },
            status=409,
        )
