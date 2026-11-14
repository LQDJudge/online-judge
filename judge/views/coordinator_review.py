"""Coordinator actions; publication requests are intentionally separate."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Max, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.vary import vary_on_headers

from judge.models import (
    Contest,
    ContestReviewRun,
    AIUsageLog,
    Problem,
    ProblemData,
    ProblemReviewRun,
)
from judge.auto_review.workflow import (
    ACTIVE,
    _authorize,
    cancel_review,
    current_content_hash,
    reviewed_content_hash,
    is_review_readable,
    publication_blockers,
    remaining_credits,
    retry_stage,
    start_contest_review,
    start_problem_review,
    review_change_reasons,
)
from judge.auto_review.contest_exposure import publication_access_details
from judge.auto_review.review_context import run_context
from judge.auto_review.context_utils import ReviewInputError
from judge.utils.public_visibility import is_site_public
from judge.auto_review.problem_types import is_supported_problem, problem_profile_labels
from judge.auto_review.phases import profile_evidence_gaps
from judge.auto_review.publication import (
    publication_decision_blockers,
    publication_decision_token,
)
from judge.auto_review.presentation import (
    publication_request_hint,
    review_author_feedback,
    review_finding_section,
    review_findings,
    review_prose,
    review_section_groups,
    review_stage_badge,
    review_stage_notes,
)


def _safe_content_hash(obj, **kwargs):
    try:
        return current_content_hash(obj, **kwargs)
    except (ReviewInputError, OSError, ValueError):
        return ""


def _object(kind, code):
    if kind == "problem":
        return get_object_or_404(Problem, code=code)
    if kind == "contest":
        return get_object_or_404(Contest, key=code)
    raise PermissionDenied()


def _readiness_labels():
    return {
        "READY": _("Ready"),
        "READY_WITH_WARNINGS": _("Ready with warnings"),
        "NEEDS_AUTHOR_FIX": _("Needs author fix"),
        "MANUAL_REVIEW": _("Manual review"),
        "INCOMPLETE": _("Incomplete"),
    }


@login_required
@never_cache
@vary_on_headers("X-Review-Fragment")
def run_review(request, kind, code):
    obj = _object(kind, code)
    _authorize(obj, request.profile)
    selected = request.POST.getlist("problems") if request.method == "POST" else []
    errors = []
    preparation_blocked = False
    if request.method == "POST":
        try:
            if request.POST.get("confirm") != "yes":
                raise ValidationError(
                    _("Please confirm the review and credit consequences.")
                )
            if kind == "problem":
                start_problem_review(
                    obj,
                    request.profile,
                    confirm_unchanged=True,
                    expected_content_hash=request.POST.get("expected_content_hash", ""),
                )
            else:
                start_contest_review(
                    obj,
                    request.profile,
                    selected_problem_ids=selected,
                    confirm_unchanged=True,
                    expected_content_hash=request.POST.get("expected_content_hash", ""),
                )
            if request.headers.get("X-Review-Fragment") == "1":
                return JsonResponse(
                    {
                        "success": True,
                        "redirect": reverse(kind + "_review_dashboard", args=[code]),
                    }
                )
            return redirect(kind + "_review_dashboard", code)
        except ValidationError as exc:
            errors = exc.messages
            preparation_blocked = all(
                item.code in ("preparation", "unsupported", "public")
                for item in exc.error_list
            )
    elif request.method != "GET":
        return JsonResponse({"error": _("Invalid request method.")}, status=405)
    children = []
    unsupported_quizzes = []
    if kind == "contest":
        unsupported_quizzes = list(
            obj.contest_problems.filter(quiz__isnull=False)
            .order_by("order", "id")
            .values_list("quiz__title", flat=True)
        )
        ids = list(
            obj.contest_problems.exclude(problem_id=None).values_list(
                "problem_id", flat=True
            )
        )
        problems = Problem.get_cached_instances(*ids)
        configs = {
            row["problem_id"]: row
            for row in ProblemData.objects.filter(problem_id__in=ids).values()
        }
        # One batched read for runs; quota balances computed from the same rows.
        runs = list(ProblemReviewRun.objects.filter(problem_id__in=ids).order_by("-id"))
        latest, charges = {}, {}
        for run in runs:
            latest.setdefault(run.problem_id, run)
            if run.credit_consumed and not run.trigger_is_superuser:
                charges[run.problem_id] = charges.get(run.problem_id, 0) + 1
        for problem in problems:
            previous = (
                latest.get(problem.pk)
                if is_review_readable(problem, request.user)
                else None
            )
            fresh = previous and reviewed_content_hash(previous) == _safe_content_hash(
                problem
            )
            credits = max(0, 3 - charges.get(problem.pk, 0))
            supported = is_supported_problem(configs.get(problem.pk))
            try:
                _authorize(problem, request.profile)
                permitted = True
            except PermissionDenied:
                permitted = False
            children.append(
                {
                    "problem": problem,
                    "latest": previous,
                    "fresh": fresh,
                    "credits": credits,
                    "disabled": not supported
                    or not permitted
                    or not request.user.is_superuser
                    and credits == 0,
                    "supported": supported,
                    "permitted": permitted,
                    "readiness_display": (
                        _readiness_labels().get(
                            previous.readiness, previous.get_status_display()
                        )
                        if previous
                        else _("Missing review")
                    ),
                    "selected": str(problem.pk) in selected if errors else not fresh,
                }
            )
    expected_hash = _safe_content_hash(obj)
    model = ProblemReviewRun if kind == "problem" else ContestReviewRun
    previous = (
        model.objects.filter(**{kind: obj}).order_by("-id").only("input_hash").first()
    )
    previous_hash = reviewed_content_hash(previous) if previous else None
    return render(
        request,
        (
            "auto_review/coordinator_start_form.html"
            if request.headers.get("X-Review-Fragment") == "1"
            else "auto_review/coordinator_start.html"
        ),
        {
            "title": _("Run review"),
            "object": obj,
            "kind": kind,
            "errors": errors,
            "preparation_blocked": preparation_blocked,
            "is_modal": request.headers.get("X-Review-Fragment") == "1",
            "remaining_credits": remaining_credits(obj),
            "children": children,
            "unsupported_quizzes": unsupported_quizzes,
            "expected_content_hash": expected_hash,
            "unchanged_since_last_review": previous_hash == expected_hash,
            "affects_contest_reviews": kind == "problem"
            and ContestReviewRun.objects.filter(
                contest__contest_problems__problem=obj
            ).exists(),
            "dashboard_url": reverse(kind + "_review_dashboard", args=[code]),
            "run_url": reverse("coordinator_run_review", args=[kind, code]),
        },
        status=400 if errors else 200,
    )


@require_POST
def manage_review(request, kind, code, run_id, action):
    if not request.user.is_authenticated:
        return JsonResponse({"error": _("Authentication required.")}, status=401)
    obj = _object(kind, code)
    model = ProblemReviewRun if kind == "problem" else ContestReviewRun
    run = get_object_or_404(model, pk=run_id, **{kind: obj})
    try:
        if request.POST.get("confirm") != "yes":
            raise ValidationError(_("Please confirm this action."))
        if action == "cancel":
            cancel_review(run, request.profile)
        elif action == "retry":
            retry_stage(run, request.POST.get("stage_id", ""), request.profile)
        else:
            raise ValidationError(_("Invalid review action."))
    except ValidationError as exc:
        return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
    if request.headers.get("X-Review-Fragment") == "1":
        return JsonResponse(
            {
                "success": True,
                "redirect": reverse(kind + "_review_dashboard", args=[code]),
            }
        )
    return redirect(kind + "_review_dashboard", code)


def coordinator_context(obj, run, user):
    can_run = False
    try:
        _authorize(obj, user.profile)
        can_run = True
    except PermissionDenied:
        pass
    stages = list(run.coordinator_stages.all().order_by("id")) if run else []
    try:
        review_context = run_context(run, for_display=True) if run else {}
    except (ReviewInputError, OSError, ValueError):
        # Historical feedback remains readable when a current upload vanished.
        review_context = {}
    if run and isinstance(obj, Problem):
        run._current_review_context = review_context
    readiness = run.readiness if run else ""
    labels = {
        "similarity_search": _("Similarity search"),
        "problem_contract": _("Formal problem contract"),
        "contest_context": _("Contest evidence package"),
        "independent_analysis": _("Independent solver and test author"),
        "statement_review": _("Statement and editorial"),
        "composition_review": _("Difficulty and diversity"),
        "consistency_review": _("Rules and consistency"),
        "solutions_review": _("Solution Codes and observed results"),
        "test_review": _("Generator and test coverage"),
        "validator_review": _("Validator assessment"),
        "checker_review": _("Checker assessment"),
        "limits_review": _("Limits and difficulty"),
        "similarity_review": _("Problem equivalence assessment"),
        "extract_stats": _("Test statistics extraction"),
        "final_assessment": _("Final readiness decision"),
    }
    retrieval_rows = {}
    for stage in stages:
        output = stage.output_json if isinstance(stage.output_json, dict) else {}
        if stage.stage_id == "similarity_search" and stage.status == stage.DONE:
            candidates = output.get("candidates")
            if isinstance(candidates, list):
                retrieval_rows[stage.pk] = [
                    row for row in candidates[:5] if isinstance(row, dict)
                ]
    candidate_ids = {
        row["problem_id"]
        for rows in retrieval_rows.values()
        for row in rows
        if type(row.get("problem_id")) is int and row["problem_id"] > 0
    }
    candidate_problems = (
        {
            problem.pk: problem
            for problem in Problem.get_cached_instances(*candidate_ids)
        }
        if candidate_ids
        else {}
    )
    for stage in stages:
        stage.display_name = labels.get(stage.stage_id, stage.stage_id)
        output = stage.output_json if isinstance(stage.output_json, dict) else {}
        stage.ui_output = output
        stage.ui_summary = review_prose(review_author_feedback(output))
        stage.ui_notes = review_stage_notes(stage)
        stage.ui_retrieval_complete = stage.pk in retrieval_rows
        stage.ui_candidates = []
        for row in retrieval_rows.get(stage.pk, []):
            identity = row.get("problem_id")
            problem = (
                candidate_problems.get(identity) if type(identity) is int else None
            )
            if problem and problem.is_public and not problem.is_organization_private:
                stage.ui_candidates.append(
                    {
                        "ref": row.get("ref"),
                        "code": problem.code,
                        "name": problem.name,
                        "url": problem.get_absolute_url(),
                    }
                )
            else:
                stage.ui_candidates.append({"ref": row.get("ref"), "url": None})
        stage.ui_retrieval_title = _("Retrieved problems (%(count)s)") % {
            "count": len(stage.ui_candidates)
        }
        stage.ui_findings = review_findings(output)
        stage.ui_class, stage.ui_status = review_stage_badge(stage)
        stage.ui_reason = stage.reason
        raw_missing = output.get("_missing_evidence", [])
        missing_evidence = (
            [
                item
                for item in raw_missing
                if isinstance(item, dict) and isinstance(item.get("stage_id"), str)
            ]
            if isinstance(raw_missing, list)
            else []
        )
        stage.ui_missing_label = (
            _("Unavailable upstream evidence: %(sections)s")
            % {
                "sections": ", ".join(
                    labels.get(item["stage_id"], item["stage_id"])
                    for item in missing_evidence
                )
            }
            if missing_evidence
            else ""
        )
        if stage.status == stage.ERROR:
            stage.ui_result_detail = (
                _("Invalid AI response")
                if stage.error_category == "invalid_response"
                else stage.get_status_display()
            )
            if stage.error_category == "invalid_response":
                stage.ui_reason = _("The AI response was incomplete or invalid.")
            elif stage.error_category.startswith(("timeout_", "provider_")):
                stage.ui_reason = stage.reason
            else:
                stage.ui_reason = _("Service unavailable; this check could not finish.")
        elif output.get("availability") == "unavailable":
            stage.ui_result_detail = _("Unavailable — missing required inputs")
        elif stage.stage_id == "final_assessment":
            stage.ui_result_detail = {
                "READY": _("Ready"),
                "READY_WITH_WARNINGS": _("Ready with warnings"),
                "NEEDS_AUTHOR_FIX": _("Needs author fix"),
                "MANUAL_REVIEW": _("Manual review"),
                "INCOMPLETE": _("Incomplete"),
            }.get(output.get("readiness"), "")
    public = is_site_public(obj)
    publication_request = getattr(obj, "public_request", None)
    content_status = (
        _("Published")
        if public
        else (
            _("Submitted")
            if publication_request and publication_request.status == "P"
            else _("Draft")
        )
    )
    content_tone = (
        "green"
        if public
        else (
            "blue"
            if publication_request and publication_request.status == "P"
            else "gray"
        )
    )
    problem_hashes = {}
    current_digest = (
        (
            review_context.get("input_hash", "")
            if isinstance(obj, Problem)
            else _safe_content_hash(obj, problem_hashes=problem_hashes)
        )
        if run
        else None
    )
    change_reasons = (
        review_change_reasons(run, obj, content_hash=current_digest) if run else []
    )
    stale = bool(change_reasons)
    model = ProblemReviewRun if isinstance(obj, Problem) else ContestReviewRun
    newest = (
        model.objects.filter(
            **{"problem" if isinstance(obj, Problem) else "contest": obj}
        )
        .order_by("-started_at", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    current_blockers = (
        publication_blockers(
            run, obj, current_context=getattr(run, "_current_review_context", None)
        )
        if run and not public
        else []
    )
    eligible = bool(run and not public and not current_blockers)
    can_submit = bool(
        not public
        and not (publication_request and publication_request.status == "P")
        and obj.can_request_public_by(user)
        and run
        and run.pk == newest
        and not stale
        and (user.is_superuser or eligible)
    )
    blockers = []
    if not public and run and run.status == run.DONE:
        if publication_request and publication_request.status == "P":
            blockers = publication_decision_blockers(
                obj, publication_request.review_run
            )
        elif not eligible:
            blockers = list(current_blockers)
        if run.pk != newest and not (
            publication_request and publication_request.status == "P"
        ):
            blockers.append(
                _(
                    "You are viewing an earlier review. Use the latest review when requesting publication."
                )
            )
        if not obj.can_request_public_by(user) and not (
            publication_request and publication_request.status == "P"
        ):
            blockers.append(
                _(
                    "Your account cannot request publication. Ask a qualified author or an administrator."
                )
            )
    usage = (
        AIUsageLog.objects.filter(
            metadata__review_kind="problem" if isinstance(obj, Problem) else "contest",
            metadata__review_run_id=run.pk,
        ).aggregate(input=Sum("input_chars"), output=Sum("output_chars"))
        if run
        else {}
    )
    tokens = AIUsageLog.estimate_tokens(
        usage.get("input")
    ) + AIUsageLog.estimate_tokens(usage.get("output"))
    cancel_targets = []
    child_reports = []
    if isinstance(run, ContestReviewRun):
        dependencies = list(
            run.child_dependencies.select_related("problem_run").prefetch_related(
                "problem_run__coordinator_stages"
            )
        )
        # Reused/attached historical reports did not incur this contest's API
        # expense. Include only the child runs created by this contest, in one
        # query, including failed paid attempts rather than only final outputs.
        owned_ids = [
            dep.problem_run_id
            for dep in dependencies
            if dep.owned and dep.problem_run_id
        ]
        child_usage = AIUsageLog.objects.filter(
            metadata__review_kind="problem", metadata__review_run_id__in=owned_ids
        ).aggregate(input=Sum("input_chars"), output=Sum("output_chars"))
        tokens += AIUsageLog.estimate_tokens(
            child_usage["input"]
        ) + AIUsageLog.estimate_tokens(child_usage["output"])
        problems = {
            problem.pk: problem
            for problem in Problem.get_cached_instances(
                *(dep.problem_id for dep in dependencies)
            )
        }
        latest_child_runs = dict(
            ProblemReviewRun.objects.filter(problem_id__in=problems)
            .values("problem_id")
            .annotate(latest_id=Max("id"))
            .values_list("problem_id", "latest_id")
        )
        latest_child_statuses = dict(
            ProblemReviewRun.objects.filter(
                pk__in=latest_child_runs.values()
            ).values_list("pk", "status")
        )
        child_reports = [
            {
                "problem": problems[dep.problem_id],
                "run": dep.problem_run,
                "owned": dep.owned,
                "url": (
                    reverse(
                        "problem_review_dashboard", args=[problems[dep.problem_id].code]
                    )
                    + "?run_id=%s" % dep.problem_run_id
                    if dep.problem_run_id
                    and is_review_readable(problems[dep.problem_id], user)
                    else None
                ),
                "unsupported": any(
                    item.get("problem_id") == dep.problem_id and item.get("unsupported")
                    for item in review_context.get("children", [])
                ),
            }
            for dep in dependencies
        ]
        for child in child_reports:
            child_run = child["run"]
            latest_id = latest_child_runs.get(child["problem"].pk)
            # This is a navigation hint, not a replacement for the immutable
            # child review captured by this contest run, nor content staleness.
            child["newer_review_url"] = (
                reverse("problem_review_dashboard", args=[child["problem"].code])
                + "?run_id=%s" % latest_id
                if child["url"] and latest_id and latest_id > child_run.pk
                else None
            )
            child["newer_review_in_progress"] = bool(
                child["newer_review_url"]
                and latest_child_statuses.get(latest_id) == "R"
            )
            child["name"] = child["problem"].name
            child["difficulty"] = (
                next(
                    (
                        stage.output_json.get("evidence", {}).get(
                            "difficulty", stage.output_json.get("difficulty")
                        )
                        for stage in child_run.coordinator_stages.all()
                        if stage.stage_id == "limits_review"
                        and stage.status == stage.DONE
                        and isinstance(stage.output_json, dict)
                        and type(
                            stage.output_json.get("evidence", {}).get(
                                "difficulty", stage.output_json.get("difficulty")
                            )
                        )
                        is int
                        and 100
                        <= stage.output_json.get("evidence", {}).get(
                            "difficulty", stage.output_json.get("difficulty")
                        )
                        <= 3000
                    ),
                    None,
                )
                if child_run
                else None
            )
        order = {
            item.get("problem_id"): index
            for index, item in enumerate(review_context.get("items", []))
        }
        child_reports.sort(key=lambda child: order.get(child["problem"].pk, 0))
    if run and run.status in ACTIVE:
        child_infra = any(
            child["run"]
            and (
                child["run"].status == "B"
                or any(
                    stage.error_category and stage.status != stage.DONE
                    for stage in child["run"].coordinator_stages.all()
                )
            )
            for child in child_reports
        )
        targets = [(obj, run)]
        if isinstance(run, ContestReviewRun):
            dependencies = list(
                run.child_dependencies.filter(
                    owned=True, problem_run__status__in=ACTIVE
                )
                .select_related("problem_run")
                .prefetch_related("problem_run__coordinator_stages")
            )
            problems = {
                problem.pk: problem
                for problem in Problem.get_cached_instances(
                    *(dep.problem_id for dep in dependencies)
                )
            }
            targets += [
                (problems[dep.problem_id], dep.problem_run) for dep in dependencies
            ]
        for target_obj, target_run in targets:
            infrastructure_blocked = target_run.status == target_run.BLOCKED or any(
                stage.error_category and stage.status != stage.DONE
                for stage in target_run.coordinator_stages.all()
            )
            infrastructure_blocked |= target_run is run and child_infra
            charge = not (target_run.trigger_is_superuser or infrastructure_blocked)
            cancel_targets.append(
                {
                    "name": target_obj.name,
                    "run_id": target_run.pk,
                    "credit": int(charge),
                }
            )
    credits = remaining_credits(obj)
    can_start = bool(
        not public
        and can_run
        and (user.is_superuser or credits > 0)
        and not model.objects.filter(
            **{"problem" if isinstance(obj, Problem) else "contest": obj},
            status__in=ACTIVE,
        ).exists()
    )
    readiness_labels = _readiness_labels()
    readiness_tones = {
        "READY": "green",
        "READY_WITH_WARNINGS": "orange",
        "NEEDS_AUTHOR_FIX": "red",
        "MANUAL_REVIEW": "orange",
        "INCOMPLETE": "orange",
    }
    for child in child_reports:
        child_run = child["run"]
        child_stale = bool(
            child_run
            and reviewed_content_hash(child_run)
            != (
                problem_hashes[child["problem"].pk]
                if child["problem"].pk in problem_hashes
                else _safe_content_hash(child["problem"])
            )
        )
        child["readiness_display"] = (
            _("Manual review")
            if child["unsupported"]
            else (
                _("Missing review")
                if not child_run
                else (
                    _("Needs re-review")
                    if child_stale
                    else (
                        child_run.get_status_display()
                        if child_run.status != "D"
                        else readiness_labels.get(child_run.readiness, _("Incomplete"))
                    )
                )
            )
        )
        child["readiness_class"] = (
            "orange"
            if child["unsupported"] or child_stale or not child_run
            else {"R": "blue", "B": "red", "E": "red", "C": "gray"}.get(
                child_run.status, readiness_tones.get(child_run.readiness, "orange")
            )
        )
    final_stage = next(
        (
            stage
            for stage in reversed(stages)
            if stage.stage_id == "final_assessment" and stage.status == stage.DONE
        ),
        None,
    )
    sections = review_section_groups(
        stages,
        "problem" if isinstance(obj, Problem) else "contest",
        context=review_context,
    )
    findings = []
    severity_labels = {
        "blocking": _("Blocking"),
        "warning": _("Warning"),
        "informational": _("Information"),
    }
    finding_stages = [final_stage] if final_stage else stages
    for stage in finding_stages:
        output = stage.output_json if isinstance(stage.output_json, dict) else {}
        stage_findings = review_findings(output)
        if not isinstance(stage_findings, list):
            continue
        for finding in stage_findings:
            if isinstance(finding, dict) and finding.get("claim"):
                section = review_finding_section(finding, stage, sections)
                findings.append(
                    {
                        **finding,
                        "stage": stage,
                        "section_id": section["id"] if section else None,
                        "section_title": (
                            section["title"] if section else stage.display_name
                        ),
                        "severity_display": severity_labels.get(
                            finding.get("severity"), _("Needs attention")
                        ),
                    }
                )
    title = readiness_labels.get(readiness, _("Pending")) if run else _("No review yet")
    tone = "gray"
    if run:
        if run.status == "R":
            title = _("Review in progress…")
            tone = "blue"
        elif run.status == "B":
            title = _("Review needs recovery")
            tone = "red"
        elif run.status == "C":
            title = _("Cancelled")
        elif run.status == "D":
            tone = readiness_tones.get(readiness, "gray")
        else:
            title = run.get_status_display()
    reviewed_readiness = readiness_labels.get(readiness, _("Pending"))
    if stale:
        title, tone = _("Needs re-review"), "orange"
    unsupported_items = []
    if isinstance(run, ContestReviewRun):
        quizzes = {
            row.get("id"): row
            for row in review_context.get("quiz_content", {}).get("quizzes", [])
        }
        for item in review_context.get("items", []):
            if not item.get("problem_id"):
                quiz = quizzes.get(item.get("quiz_id"), {})
                unsupported_items.append(
                    quiz.get("name") or quiz.get("title") or _("Quiz")
                )
    return {
        "coordinator_can_run": can_run,
        "coordinator_can_start": can_start,
        "coordinator_reviewed_readiness_display": reviewed_readiness,
        "coordinator_readiness_display": (
            _("Needs re-review") if stale else reviewed_readiness
        ),
        "coordinator_readiness_class": (
            "orange" if stale else readiness_tones.get(readiness, "gray")
        ),
        "coordinator_stages": stages,
        "coordinator_sections": sections,
        "coordinator_partial": bool(run and run.status == "B")
        or any(
            stage.status == stage.ERROR or stage.ui_output.get("_missing_evidence")
            for stage in stages
        ),
        "coordinator_blocked_stages": [
            stage for stage in stages if stage.status == stage.ERROR
        ],
        "coordinator_search_disabled": not getattr(settings, "USE_ML", False)
        and any(
            stage.stage_id == "similarity_search" and stage.status == stage.ERROR
            for stage in stages
        ),
        "coordinator_credits": credits,
        "coordinator_stale": stale,
        "coordinator_changes": change_reasons if stale else [],
        "coordinator_publication_access": (
            publication_access_details(obj) if not public and run else None
        ),
        "coordinator_can_submit": can_submit,
        "coordinator_show_submit": not public and obj.can_request_public_by(user),
        "coordinator_can_withdraw": bool(
            not public
            and obj.can_request_public_by(user)
            and publication_request
            and publication_request.status == "P"
        ),
        "coordinator_publication_request": publication_request,
        "coordinator_can_decide_request": bool(
            user.is_superuser
            and not public
            and publication_request
            and publication_request.status == "P"
        ),
        "coordinator_request_decision_token": (
            publication_decision_token(publication_request)
            if publication_request
            else ""
        ),
        "publication_request_hint": (
            publication_request_hint(
                run,
                blockers,
                pending=bool(publication_request and publication_request.status == "P"),
                stale=stale,
                earlier=bool(run and run.pk != newest),
            )
            if not can_submit
            else ""
        ),
        "publication_blockers": blockers,
        "coordinator_usage_tokens": tokens if tokens else None,
        "coordinator_cancel_targets": cancel_targets,
        "coordinator_child_reports": child_reports,
        "coordinator_content_status": content_status,
        "coordinator_content_class": content_tone,
        "coordinator_kind": "problem" if isinstance(obj, Problem) else "contest",
        "coordinator_problem_type_labels": (
            problem_profile_labels(review_context)
            if isinstance(obj, Problem) and run
            else []
        ),
        "coordinator_code": obj.code if isinstance(obj, Problem) else obj.key,
        "coordinator_state_title": title,
        "coordinator_state_class": tone,
        "coordinator_execution_class": (
            {"R": "blue", "B": "red", "E": "red", "D": tone}.get(run.status, "gray")
            if run
            else "gray"
        ),
        "coordinator_findings": findings[:8],
        "coordinator_profile_gaps": (
            profile_evidence_gaps(
                review_context,
                {
                    stage.stage_id: stage.output_json
                    for stage in stages
                    if stage.status == stage.DONE
                },
            )
            if run and run.status == "D" and readiness == "MANUAL_REVIEW"
            else []
        ),
        "coordinator_findings_count": len(findings),
        "coordinator_findings_provisional": not final_stage,
        "coordinator_completed_stages": sum(
            stage.status == stage.DONE for stage in stages
        ),
        "coordinator_progress_label": _(
            "%(done)s of %(total)s processing steps completed"
        )
        % {
            "done": sum(stage.status == stage.DONE for stage in stages),
            "total": len(stages),
        },
        "coordinator_stage_progress": [stage.status for stage in stages],
        "coordinator_unsupported_items": unsupported_items,
        "coordinator_usage_display": (
            ("%.1fk" % (tokens / 1000) if tokens >= 1000 else str(int(tokens)))
            if tokens
            else None
        ),
        "coordinator_status_url": reverse(
            (
                "problem_review_status"
                if isinstance(obj, Problem)
                else "contest_review_status"
            ),
            args=[obj.code if isinstance(obj, Problem) else obj.key],
        ),
    }
