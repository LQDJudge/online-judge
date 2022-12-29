from django.views.generic import TemplateView
from django.utils.translation import gettext as _
from django.http import HttpResponseForbidden
from judge.models import Contest
from django.utils.safestring import mark_safe

import json


class Resolver(TemplateView):
    title = _("Resolver")
    template_name = "resolver/resolver.html"

    def get_contest_json(self):
        problems = self.contest.contest_problems.values_list("order", "id")
        order_to_id = {}
        id_to_order = {}
        for order, problem_id in problems:
            id_to_order[str(problem_id)] = order

        frozen_subtasks = self.contest.format.get_frozen_subtasks()
        num_problems = len(problems)
        problem_sub = [0] * num_problems
        sub_frozen = [0] * num_problems
        problems_json = {str(i): {} for i in range(1, num_problems + 1)}

        users = {}
        cnt_user = 0
        total_subtask_points_map = {}

        for participation in self.contest.users.filter(virtual=0):
            cnt_user += 1
            users[str(cnt_user)] = {
                "username": participation.user.user.username,
                "name": participation.user.user.first_name
                or participation.user.user.username,
                "school": participation.user.user.last_name,
                "last_submission": participation.cumtime_final,
                "problems": {},
            }
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in self.contest.format.get_results_by_subtask(participation, True):
                problem_id = str(problem_id)
                order = id_to_order[problem_id]
                problem_sub[order - 1] = max(problem_sub[order - 1], subtask)
                if total_subtask_points:
                    total_subtask_points_map[(order, subtask)] = total_subtask_points

        cnt_user = 0
        for participation in self.contest.users.filter(virtual=0):
            cnt_user += 1
            total_points = {}
            points_map = {}
            frozen_points_map = {}
            problem_points_map = {}
            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in self.contest.format.get_results_by_subtask(participation, True):
                problem_id = str(problem_id)
                order = id_to_order[problem_id]
                points_map[(order, subtask)] = subtask_points
                if order not in total_points:
                    total_points[order] = 0
                total_points[order] += total_subtask_points
                problem_points_map[order] = problem_points

            for (
                problem_id,
                problem_points,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in self.contest.format.get_results_by_subtask(participation, False):
                problem_id = str(problem_id)
                order = id_to_order[problem_id]
                frozen_points_map[(order, subtask)] = subtask_points

            for order in range(1, num_problems + 1):
                for subtask in range(1, problem_sub[order - 1] + 1):
                    if not total_points.get(order, 0):
                        continue
                    if str(order) not in users[str(cnt_user)]["problems"]:
                        users[str(cnt_user)]["problems"][str(order)] = {
                            "points": {},
                            "frozen_points": {},
                        }
                    problems_json[str(order)][str(subtask)] = round(
                        total_subtask_points_map[(order, subtask)]
                        / total_points[order]
                        * problem_points_map[order],
                        self.contest.points_precision,
                    )
                    users[str(cnt_user)]["problems"][str(order)]["points"][
                        str(subtask)
                    ] = round(
                        points_map.get((order, subtask), 0)
                        / total_points[order]
                        * problem_points_map[order],
                        self.contest.points_precision,
                    )
                    users[str(cnt_user)]["problems"][str(order)]["frozen_points"][
                        str(subtask)
                    ] = round(
                        frozen_points_map.get((order, subtask), 0)
                        / total_points[order]
                        * problem_points_map[order],
                        self.contest.points_precision,
                    )

        for i in frozen_subtasks:
            order = id_to_order[i]
            if frozen_subtasks[i]:
                sub_frozen[order - 1] = min(frozen_subtasks[i])
            else:
                sub_frozen[order - 1] = problem_sub[order - 1] + 1
        return {
            "problem_sub": problem_sub,
            "sub_frozen": sub_frozen,
            "problems": problems_json,
            "users": users,
        }

    def get_context_data(self, **kwargs):
        context = super(Resolver, self).get_context_data(**kwargs)
        context["contest_json"] = mark_safe(json.dumps(self.get_contest_json()))
        return context

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            self.contest = Contest.objects.get(key=kwargs.get("contest"))
            if self.contest.format_name == "ioi16":
                return super(Resolver, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()
