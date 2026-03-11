import json

from django.db.models import Max
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from judge.models import Contest


class Resolver(TemplateView):
    title = _("Resolver")
    template_name = "resolver/resolver.html"

    def get(self, request, *args, **kwargs):
        self.contest = get_object_or_404(Contest, key=kwargs.get("contest"))
        if not self.contest.is_editable_by(request.user):
            return HttpResponseForbidden()

        has_hidden = self.contest.format.has_hidden_subtasks
        has_freeze = bool(self.contest.freeze_after)
        if not (has_hidden or has_freeze):
            return HttpResponseForbidden()

        if self.request.GET.get("json"):
            return JsonResponse(
                self.get_contest_json(),
                json_dumps_params={"ensure_ascii": False},
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["contest_json"] = mark_safe(json.dumps(self.get_contest_json()))
        contest = self.contest
        context["title"] = _("Resolver") + " - " + contest.name
        context["content_title"] = contest.name + " " + _("Resolver")
        context["contest"] = contest
        context["can_edit"] = contest.is_editable_by(self.request.user)
        context["can_access"] = True
        context["contest_has_hidden_subtasks"] = contest.format.has_hidden_subtasks
        context["show_final_ranking"] = (
            contest.format.has_hidden_subtasks and context["can_edit"]
        )
        context["is_clonable"] = False
        context["now"] = timezone.now()
        context["page_type"] = "resolver"
        context["has_moss_api_key"] = False
        return context

    def get_contest_json(self):
        if self.contest.format.has_hidden_subtasks:
            return self._build_subtask_data()
        return self._build_frozen_data()

    # ------------------------------------------------------------------
    # Mode: frozen (default, ICPC, atcoder, etc. with freeze_after)
    # ------------------------------------------------------------------
    def _build_frozen_data(self):
        contest = self.contest
        precision = contest.points_precision
        contest_problems = list(
            contest.contest_problems.select_related("problem")
            .order_by("order")
            .values_list("id", "order", "points", "problem__code")
        )

        # Build problem info with labels
        problems = []
        id_to_idx = {}
        for idx, (cp_id, order, max_points, code) in enumerate(contest_problems):
            label = contest.get_label_for_problem(idx)
            problems.append(
                {
                    "order": order,
                    "label": label,
                    "code": code,
                    "max_points": float(max_points),
                }
            )
            id_to_idx[str(cp_id)] = order

        users = []
        participations = contest.users.filter(virtual=0).select_related(
            "user__user",
        )
        for participation in participations:
            profile = participation.user
            # Frozen scores: best per problem BEFORE freeze
            frozen_scores = {}
            if contest.freeze_after:
                for row in (
                    participation.submissions.filter(
                        submission__date__lt=participation.start + contest.freeze_after
                    )
                    .values("problem_id")
                    .annotate(points=Max("points"))
                ):
                    cp_id = str(row["problem_id"])
                    if cp_id in id_to_idx:
                        frozen_scores[id_to_idx[cp_id]] = round(
                            float(row["points"]), precision
                        )

            # Final scores: query ALL submissions (not format_data which
            # may already be filtered by freeze_after in some formats)
            final_scores = {}
            for row in participation.submissions.values("problem_id").annotate(
                points=Max("points")
            ):
                cp_id = str(row["problem_id"])
                if cp_id in id_to_idx:
                    final_scores[id_to_idx[cp_id]] = round(
                        float(row["points"]), precision
                    )

            # Build per-problem data
            problem_data = {}
            frozen_total = 0.0
            final_total = 0.0
            for p in problems:
                order = p["order"]
                fs = frozen_scores.get(order, 0)
                fn = final_scores.get(order, 0)
                problem_data[str(order)] = {
                    "frozen": round(fs, precision),
                    "final": round(fn, precision),
                }
                frozen_total += fs
                final_total += fn

            users.append(
                {
                    "username": profile.username,
                    "display_name": profile.first_name or "",
                    "school": profile.last_name or "",
                    "css_class": profile.css_class,
                    "problems": problem_data,
                    "frozen_total": round(frozen_total, precision),
                    "final_total": round(final_total, precision),
                    "cumtime": participation.cumtime,
                }
            )

        show_cumtime = (getattr(contest.format, "config", None) or {}).get(
            "cumtime", True
        )
        return {
            "contest_name": contest.name,
            "mode": "frozen",
            "show_cumtime": show_cumtime,
            "problems": problems,
            "users": users,
        }

    # ------------------------------------------------------------------
    # Mode: subtask (new_ioi with hidden subtasks)
    # ------------------------------------------------------------------
    def _build_subtask_data(self):
        contest = self.contest
        precision = contest.points_precision
        contest_problems = list(
            contest.contest_problems.select_related("problem")
            .order_by("order")
            .values_list("id", "order", "points", "problem__code")
        )

        hidden_subtasks = contest.format.get_hidden_subtasks()

        problems = []
        id_to_idx = {}
        for idx, (cp_id, order, max_points, code) in enumerate(contest_problems):
            label = contest.get_label_for_problem(idx)
            problems.append(
                {
                    "order": order,
                    "label": label,
                    "code": code,
                    "max_points": float(max_points),
                }
            )
            id_to_idx[str(cp_id)] = order

        # First pass: determine max subtask count per problem and total_subtask_points
        subtask_counts = {}
        total_subtask_points_map = {}
        participations = list(
            contest.users.filter(virtual=0).select_related("user__user")
        )
        for participation in participations:
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in contest.format.get_results_by_subtask(participation, True):
                subtask = subtask or 1
                pid = str(problem_id)
                if pid not in id_to_idx:
                    continue
                order = id_to_idx[pid]
                subtask_counts[order] = max(subtask_counts.get(order, 0), subtask)
                if total_subtask_points:
                    total_subtask_points_map[(order, subtask)] = total_subtask_points

        users = []
        for participation in participations:
            profile = participation.user
            # Get final (all subtasks) and frozen (without hidden) results
            final_results = {}
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in contest.format.get_results_by_subtask(participation, True):
                subtask = subtask or 1
                pid = str(problem_id)
                if pid not in id_to_idx:
                    continue
                order = id_to_idx[pid]
                final_results[(order, subtask)] = (
                    subtask_points,
                    total_subtask_points,
                    problem_points,
                )

            frozen_results = {}
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in contest.format.get_results_by_subtask(participation, False):
                subtask = subtask or 1
                pid = str(problem_id)
                if pid not in id_to_idx:
                    continue
                order = id_to_idx[pid]
                frozen_results[(order, subtask)] = (
                    subtask_points,
                    total_subtask_points,
                    problem_points,
                )

            problem_data = {}
            frozen_total = 0.0
            final_total = 0.0

            for p in problems:
                order = p["order"]
                num_subtasks = subtask_counts.get(order, 0)
                if num_subtasks == 0:
                    continue

                # Find the CP id for this order to get hidden subtasks
                cp_id = None
                for cid, o, pts, code in contest_problems:
                    if o == order:
                        cp_id = str(cid)
                        break
                hidden = hidden_subtasks.get(cp_id, set()) if cp_id else set()

                subtasks = []
                frozen_sum = 0.0
                final_sum = 0.0

                # Calculate total points for this problem across all subtasks
                total_pts = 0
                for s in range(1, num_subtasks + 1):
                    total_pts += total_subtask_points_map.get((order, s), 0)

                # Get problem max points from contest_problems
                problem_max_pts = p["max_points"]

                for s in range(1, num_subtasks + 1):
                    fr = frozen_results.get((order, s))
                    fi = final_results.get((order, s))
                    total_sp = total_subtask_points_map.get((order, s), 0)

                    if total_pts and total_sp:
                        max_sub = round(
                            total_sp / total_pts * problem_max_pts, precision
                        )
                    else:
                        max_sub = 0

                    # For hidden subtasks, frozen score is always 0
                    # (participants can't see hidden subtask results)
                    frozen_pts = 0
                    if fr and total_pts and s not in hidden:
                        frozen_pts = round(
                            fr[0] / total_pts * problem_max_pts, precision
                        )
                    final_pts = 0
                    if fi and total_pts:
                        final_pts = round(
                            fi[0] / total_pts * problem_max_pts, precision
                        )

                    subtasks.append(
                        {
                            "frozen": frozen_pts,
                            "final": final_pts,
                            "max": max_sub,
                            "hidden": s in hidden,
                        }
                    )
                    frozen_sum += frozen_pts
                    final_sum += final_pts

                problem_data[str(order)] = {
                    "frozen": round(frozen_sum, precision),
                    "final": round(final_sum, precision),
                    "subtasks": subtasks,
                }
                frozen_total += frozen_sum
                final_total += final_sum

            users.append(
                {
                    "username": profile.username,
                    "display_name": profile.first_name or "",
                    "school": profile.last_name or "",
                    "css_class": profile.css_class,
                    "problems": problem_data,
                    "frozen_total": round(frozen_total, precision),
                    "final_total": round(final_total, precision),
                    "cumtime": participation.cumtime_final,
                }
            )

        show_cumtime = (getattr(contest.format, "config", None) or {}).get(
            "cumtime", True
        )
        return {
            "contest_name": contest.name,
            "mode": "subtask",
            "show_cumtime": show_cumtime,
            "problems": problems,
            "users": users,
        }
