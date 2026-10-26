"""Separate, snapshot-bound publication requests and append-only decisions.

Object rows are always locked before request rows, matching content mutation guards.
These functions never start review work or rewrite a review's readiness.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import format_html
from django.utils.translation import gettext as _

from judge.models import (
    Contest,
    ContestPublicRequest,
    Problem,
    PublicationRequestEvent,
    PublicRequest,
    Profile,
)
from judge.models.notification import Notification, NotificationCategory
from judge.review import workflow
from judge.review.exposure import publication_exposure_concern
from judge.review.system_bot import post_publication_decision_comment


def _configuration(obj):
    if isinstance(obj, Problem):
        return PublicRequest, "problem", "problem_run"
    if isinstance(obj, Contest):
        return ContestPublicRequest, "contest", "contest_run"
    raise TypeError("Unsupported publication object")


def _lock_live_dependencies(obj):
    if isinstance(obj, Contest):
        problem_ids = obj.contest_problems.exclude(problem_id=None).values_list(
            "problem_id", flat=True
        )
        # Match contest admission's parent-then-stable-child ordering. A child Save
        # cannot change live judge content between the final hash check and approval.
        list(
            Problem.objects.select_for_update()
            .filter(pk__in=problem_ids)
            .order_by("pk")
        )


def _latest_equivalent_review_id(obj, run):
    """Compare technical identity across hash-policy versions, not raw old hashes."""
    content_hash = workflow.reviewed_content_hash(run)
    candidates = (
        type(run)
        .objects.filter(
            **{_configuration(obj)[1]: obj}, workflow_version=workflow.WORKFLOW_VERSION
        )
        .only("id", "snapshot", "content_hash")
        .order_by("-started_at", "-id")
    )
    return next(
        (
            candidate.pk
            for candidate in candidates
            if workflow.reviewed_content_hash(candidate) == content_hash
        ),
        None,
    )


def _event(obj, request_row, actor, reason=""):
    request_model, object_field, run_field = _configuration(obj)
    run = request_row.review_run
    return PublicationRequestEvent.objects.create(
        **{object_field: obj, run_field: run},
        actor=actor.profile,
        status=request_row.status,
        candidate_hash=request_row.candidate_hash,
        reason=reason,
        readiness=getattr(run, "readiness", "") if run else "",
    )


def publication_decision_token(request_row):
    """Bind a decision dialog to this request revision, including resubmissions."""
    if request_row is None:
        return ""
    identity = "%s:%s:%s:%s:%s" % (
        request_row.pk,
        request_row.review_run_id,
        request_row.candidate_hash,
        request_row.status,
        request_row.updated_at.isoformat(),
    )
    return salted_hmac("publication-request-decision", identity).hexdigest()


def publication_decision_blockers(obj, run):
    """Explain live approval safeguards without changing the bound candidate."""
    blockers = workflow.publication_blockers(run, obj)
    if publication_exposure_concern(obj) and not blockers:
        blockers.append(
            _(
                "Possible pre-release access requires administrator review. This is a risk signal, not proof of a leak."
            )
        )
    if run and _latest_equivalent_review_id(obj, run) != run.pk:
        blockers.append(
            _(
                "A newer review of the same content exists. This request still uses the earlier review; check the newer result before approving."
            )
        )
    return blockers


def _publish_locked(obj):
    visibility_field = "is_public" if isinstance(obj, Problem) else "is_visible"
    setattr(obj, visibility_field, True)
    obj.is_locked = True
    if isinstance(obj, Problem):
        obj.is_organization_private = False
        obj._bypass_points_cap = True
        obj.save(
            update_fields=[visibility_field, "is_locked", "is_organization_private"]
        )
        obj.organizations.clear()
    else:
        obj.is_private = False
        obj.is_organization_private = False
        obj.is_in_course = False
        obj.save(
            update_fields=[
                visibility_field,
                "is_locked",
                "is_private",
                "is_organization_private",
                "is_in_course",
            ]
        )
    transaction.on_commit(lambda obj=obj: workflow.invalidate_readiness(obj))


@transaction.atomic
def publish_without_review(obj, actor, override_reason):
    if not actor.is_superuser:
        raise PermissionDenied
    reason = override_reason.strip()
    if not reason:
        raise ValidationError(
            _(
                "Direct publication without a pending reviewed candidate requires an override reason."
            ),
            code="publication_override_required",
        )
    obj = type(obj).objects.select_for_update().get(pk=obj.pk)
    _lock_live_dependencies(obj)
    request_model, field, run_field = _configuration(obj)
    pending = request_model.objects.filter(
        **{field: obj}, status=request_model.PENDING
    ).first()
    if pending and pending.review_run_id:
        return decide_publication(obj, actor, override_reason=reason, publish=True)
    if pending:
        pending.status = request_model.APPROVED
        pending.candidate_hash = workflow.current_content_hash(obj)
        pending.override_reason = reason
        pending.reviewed_by = actor.profile
        pending.save()
        event = _event(obj, pending, actor, reason)
        _publish_locked(obj)
        _emit_request_decision(obj, pending, actor, published=True)
        return event
    event = PublicationRequestEvent.objects.create(
        **{field: obj},
        actor=actor.profile,
        status=request_model.APPROVED,
        candidate_hash=workflow.current_content_hash(obj),
        reason=reason,
    )
    _publish_locked(obj)
    return event


@transaction.atomic
def submit_publication(obj, actor, run_id=None):
    obj = type(obj).objects.select_for_update().get(pk=obj.pk)
    _lock_live_dependencies(obj)
    if not obj.can_request_public_by(actor):
        raise PermissionDenied
    request_model, field, run_field = _configuration(obj)
    run_queryset = obj.review_runs.all()
    run = (
        run_queryset.filter(pk=run_id).first()
        if run_id is not None
        else run_queryset.order_by("-started_at", "-pk").first()
    )
    if run is None:
        raise ValidationError(_("Run a review before requesting publication."))
    latest_run = run_queryset.order_by("-started_at", "-pk").first()
    if not actor.is_superuser and run.pk != latest_run.pk:
        raise ValidationError(
            _(
                "Use the latest review result when requesting publication. Ask a superuser to resolve conflicting reviews."
            )
        )
    if not actor.is_superuser and not workflow.publication_eligibility(run, obj):
        raise ValidationError(
            workflow.publication_blockers(run, obj)
            or [
                _("Publication requires a current READY or READY WITH WARNINGS review.")
            ]
        )
    if workflow.reviewed_content_hash(run) != workflow.current_content_hash(obj):
        raise ValidationError(
            _("The reviewed candidate has changed. Run a new review first.")
        )
    if not actor.is_superuser and publication_exposure_concern(obj):
        raise ValidationError(
            _("Current private-content exposure requires manual review by a superuser.")
        )
    request_row = (
        request_model.objects.select_for_update().filter(**{field: obj}).first()
    )
    if request_row and request_row.status == request_model.PENDING:
        if (
            request_row.review_run_id == run.pk
            and request_row.candidate_hash == run.content_hash
        ):
            request_row.new_submission = False
            return request_row
        raise ValidationError(
            _(
                "Withdraw the current publication request before submitting another candidate."
            )
        )
    if request_row is None:
        request_row = request_model(**{field: obj}, requested_by=actor.profile)
    request_row.status = request_model.PENDING
    request_row.requested_by = actor.profile
    request_row.review_run = run
    request_row.candidate_hash = run.content_hash
    request_row.feedback = ""
    request_row.override_reason = ""
    request_row.reviewed_by = None
    request_row.save()
    _event(obj, request_row, actor)
    request_row.new_submission = True
    if isinstance(obj, Contest):
        Notification.objects.bulk_create_notifications(
            user_ids=list(
                Profile.objects.filter(user__is_superuser=True).values_list(
                    "pk", flat=True
                )
            ),
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_NEW,
            html_link=format_html(
                '<a href="{}?tab=overview#run-status">{}</a>',
                reverse("contest_review_dashboard", args=[obj.key]),
                obj.name,
            ),
            author=actor.profile,
        )
    return request_row


@transaction.atomic
def withdraw_publication(obj, actor, reason=""):
    obj = type(obj).objects.select_for_update().get(pk=obj.pk)
    if not actor.is_superuser and not obj.can_request_public_by(actor):
        raise PermissionDenied
    return withdraw_pending_locked(obj, actor, reason)


def withdraw_pending_locked(obj, actor, reason=""):
    """Caller must hold the object lock; editors may withdraw by confirmed Save."""
    request_model, field, run_field = _configuration(obj)
    request_row = (
        request_model.objects.select_for_update()
        .filter(**{field: obj}, status=request_model.PENDING)
        .first()
    )
    if request_row is None:
        return None
    request_row.status = request_model.WITHDRAWN
    request_row.save(update_fields=["status", "updated_at"])
    _event(obj, request_row, actor, reason)
    return request_row


@transaction.atomic
def decide_publication(
    obj,
    actor,
    approve=True,
    feedback="",
    override_reason="",
    publish=False,
    expected_request_token=None,
):
    if not actor.is_superuser:
        raise PermissionDenied
    obj = type(obj).objects.select_for_update().get(pk=obj.pk)
    _lock_live_dependencies(obj)
    request_model, field, run_field = _configuration(obj)
    request_row = (
        request_model.objects.select_for_update().filter(**{field: obj}).first()
    )
    if request_row is None or request_row.status != request_model.PENDING:
        raise ValidationError(_("This publication request is no longer pending."))
    if expected_request_token is not None and not constant_time_compare(
        expected_request_token, publication_decision_token(request_row)
    ):
        raise ValidationError(
            _(
                "This publication request has changed. Refresh the review before deciding."
            )
        )
    run = request_row.review_run
    if approve:
        if (
            not run
            or run.workflow_version != workflow.WORKFLOW_VERSION
            or getattr(run, field + "_id") != obj.pk
            or request_row.candidate_hash != run.content_hash
            or workflow.reviewed_content_hash(run) != workflow.current_content_hash(obj)
        ):
            raise ValidationError(
                _(
                    "The requested candidate is stale or missing. Submit the current revision first."
                )
            )
        blockers = publication_decision_blockers(obj, run)
        if blockers and not override_reason.strip():
            raise ValidationError(
                " ".join(
                    blockers
                    + [
                        _(
                            "A superuser override reason is required for this review result."
                        )
                    ]
                ),
                code="publication_override_required",
            )
    request_row.status = request_model.APPROVED if approve else request_model.REJECTED
    request_row.feedback = feedback.strip()
    request_row.override_reason = override_reason.strip() if approve else ""
    request_row.reviewed_by = actor.profile
    request_row.save()
    _event(obj, request_row, actor, request_row.override_reason or feedback.strip())
    if approve and publish:
        _publish_locked(obj)
    _emit_request_decision(obj, request_row, actor, published=approve and publish)
    return request_row


def _emit_request_decision(obj, request_row, actor, *, published=False):
    comment = post_publication_decision_comment(
        obj, request_row.status, actor.profile, request_row.feedback
    )
    is_problem = isinstance(obj, Problem)
    code = obj.code if is_problem else obj.key
    review_url = reverse(
        "problem_review_dashboard" if is_problem else "contest_review_dashboard",
        args=[code],
    )
    decision_url = (
        "%s?tab=discussion&target_comment=%s#comment-%s"
        % (review_url, comment.pk, comment.pk)
        if comment
        else (
            reverse("problem_detail" if is_problem else "contest_view", args=[code])
            if published
            else review_url + "?tab=discussion"
        )
    )
    if request_row.requested_by_id:
        Notification.objects.create_notification(
            owner=request_row.requested_by,
            category=(
                (
                    NotificationCategory.PUBLIC_REQUEST_APPROVED
                    if is_problem
                    else NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
                )
                if request_row.status == request_row.APPROVED
                else (
                    NotificationCategory.PUBLIC_REQUEST_REJECTED
                    if is_problem
                    else NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED
                )
            ),
            html_link=format_html(
                '<a href="{}">{}</a>',
                decision_url,
                obj.name,
            ),
            author=actor.profile,
            extra_data={
                "feedback": request_row.feedback,
                "request_id": request_row.pk,
                "comment_id": comment.pk if comment else None,
            },
        )
