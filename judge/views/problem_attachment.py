import json
import os

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Max
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from judge.forms import (
    PROBLEM_ATTACHMENT_MAX_COUNT,
    PROBLEM_ATTACHMENT_MAX_COUNT_SUPERUSER,
    PROBLEM_ATTACHMENT_MAX_SIZE,
    PROBLEM_ATTACHMENT_MAX_SIZE_SUPERUSER,
    ProblemAttachmentForm,
)
from judge.models import Problem, ProblemAttachment
from judge.utils.storage_helpers import serve_file_with_nginx


def _attachment_limits(user):
    """Returns (max_size_bytes, max_count) for the given user."""
    if user.is_superuser:
        return (
            PROBLEM_ATTACHMENT_MAX_SIZE_SUPERUSER,
            PROBLEM_ATTACHMENT_MAX_COUNT_SUPERUSER,
        )
    return PROBLEM_ATTACHMENT_MAX_SIZE, PROBLEM_ATTACHMENT_MAX_COUNT


def _get_editable(request, code):
    problem = get_object_or_404(Problem, code=code)
    if not problem.is_editable_by(request.user):
        raise PermissionDenied
    return problem


def _get_viewable(request, code):
    problem = get_object_or_404(Problem, code=code)
    if not problem.is_accessible_by(request.user):
        raise PermissionDenied
    return problem


@login_required
@ensure_csrf_cookie
def attachments_tab(request, problem):
    problem_obj = _get_editable(request, problem)
    max_size, max_count = _attachment_limits(request.user)
    return render(
        request,
        "problem/attachments.html",
        {
            "problem": problem_obj,
            "attachments": problem_obj.attachments.all(),
            "title": _("Attachments for %s") % problem_obj.name,
            "page_type": "attachments",
            "attachment_limits_msg": _(
                "Limit: up to %(count)d files, each up to %(mb)d MB."
            )
            % {"count": max_count, "mb": max_size // (1024 * 1024)},
        },
    )


@login_required
@require_POST
def attachment_upload(request, problem):
    problem_obj = _get_editable(request, problem)

    max_size, max_count = _attachment_limits(request.user)

    if problem_obj.attachments.count() >= max_count:
        return JsonResponse(
            {
                "success": False,
                "error": _("Attachment limit reached (%(n)d files maximum).")
                % {"n": max_count},
            },
            status=400,
        )

    uploaded = request.FILES.get("file")
    if uploaded and uploaded.size > max_size:
        return JsonResponse(
            {
                "success": False,
                "error": _("File too large. Maximum size is %(mb)d MB.")
                % {"mb": max_size // (1024 * 1024)},
            },
            status=400,
        )

    # Optional rename — caller may pass a custom basename. We keep the original
    # extension to avoid mismatches with the test_data filename (output-only
    # judging looks files up by extension/name).
    custom_name = (request.POST.get("rename") or "").strip()
    if custom_name and uploaded is not None:
        custom_name = os.path.basename(custom_name)
        custom_name = "".join(
            c for c in custom_name if c.isprintable() and c not in "/\\"
        )
        if custom_name:
            _, orig_ext = os.path.splitext(uploaded.name)
            # Always preserve original extension — strip any extension the
            # user typed and append the original.
            base, _ = os.path.splitext(custom_name)
            uploaded.name = (base or custom_name) + orig_ext

    next_order = (problem_obj.attachments.aggregate(m=Max("order"))["m"] or 0) + 1
    post_data = request.POST.copy()
    if not post_data.get("order"):
        post_data["order"] = str(next_order)
    form = ProblemAttachmentForm(post_data, request.FILES)
    if not form.is_valid():
        return JsonResponse({"success": False, "errors": form.errors}, status=400)

    att = form.save(commit=False)
    att.problem = problem_obj
    if not att.order:
        att.order = next_order
    att.save()
    return JsonResponse(
        {
            "success": True,
            "id": att.id,
            "filename": att.filename,
            "description": att.description,
            "order": att.order,
            "size": att.file.size,
        }
    )


@login_required
@require_POST
def attachment_update(request, problem, attachment_id):
    problem_obj = _get_editable(request, problem)
    att = get_object_or_404(ProblemAttachment, id=attachment_id, problem=problem_obj)

    update_fields = []
    description = request.POST.get("description")
    if description is not None:
        att.description = description[:255]
        update_fields.append("description")

    new_name = (request.POST.get("filename") or "").strip()
    if new_name:
        new_name = os.path.basename(new_name)
        new_name = "".join(c for c in new_name if c.isprintable() and c not in "/\\")
        if new_name and new_name != att.filename:
            _, orig_ext = os.path.splitext(att.filename)
            # Always preserve original extension — strip user-typed extension.
            base, _ = os.path.splitext(new_name)
            new_name = (base or new_name) + orig_ext
            # Move file in storage to the new name (per-problem dir)
            storage = att.file.storage
            old_path = att.file.name
            new_path = os.path.join(os.path.dirname(old_path), new_name)
            if storage.exists(new_path):
                return JsonResponse(
                    {
                        "success": False,
                        "error": _("A file with that name already exists."),
                    },
                    status=400,
                )
            with storage.open(old_path, "rb") as src:
                storage.save(new_path, src)
            att.file.name = new_path
            update_fields.append("file")
            # Save DB before deleting old file: a crash here leaves an orphan, never a broken row.
            att.save(update_fields=update_fields)
            update_fields = []
            storage.delete(old_path)

    if update_fields:
        att.save(update_fields=update_fields)
    return JsonResponse(
        {
            "success": True,
            "description": att.description,
            "filename": att.filename,
        }
    )


@login_required
@require_POST
def attachment_delete(request, problem, attachment_id):
    problem_obj = _get_editable(request, problem)
    att = get_object_or_404(ProblemAttachment, id=attachment_id, problem=problem_obj)
    att.file.delete(save=False)
    att.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def attachment_reorder(request, problem):
    problem_obj = _get_editable(request, problem)

    ids = request.POST.getlist("order") or request.POST.getlist("order[]")
    if not ids:
        try:
            ids = json.loads(request.body or "{}").get("order", [])
        except json.JSONDecodeError:
            return HttpResponseBadRequest("invalid body")

    for new_order, raw_id in enumerate(ids):
        try:
            att_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        ProblemAttachment.objects.filter(id=att_id, problem=problem_obj).update(
            order=new_order
        )
    return JsonResponse({"success": True})


@login_required
@require_GET
def attachment_download(request, problem, attachment_id):
    problem_obj = _get_viewable(request, problem)
    att = get_object_or_404(ProblemAttachment, id=attachment_id, problem=problem_obj)
    return serve_file_with_nginx(
        request,
        att.file.storage,
        att.file.name,
        attachment_filename=att.filename,
    )
