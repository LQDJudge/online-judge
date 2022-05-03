from django.http import HttpResponseBadRequest, JsonResponse
from django.db import transaction

from judge.models import VolunteerProblemVote, Problem, ProblemType


def vote_problem(request):
    if not request.user or not request.user.has_perm('judge.suggest_problem_changes'):
        return HttpResponseBadRequest()
    if not request.method == 'POST':
        return HttpResponseBadRequest()
    try:
        types_id = request.POST.getlist('types[]')
        types = ProblemType.objects.filter(id__in=types_id)
        problem = Problem.objects.get(code=request.POST['problem'])
        knowledge_points = request.POST['knowledge_points']
        thinking_points = request.POST['thinking_points']
        feedback = request.POST['feedback']
    except Exception as e:
        return HttpResponseBadRequest()

    with transaction.atomic():
        vote, _ = VolunteerProblemVote.objects.get_or_create(
            voter=request.profile,
            problem=problem,
            defaults={'knowledge_points': 0, 'thinking_points': 0},
        )
        vote.knowledge_points = knowledge_points
        vote.thinking_points = thinking_points
        vote.feedback = feedback
        vote.types.set(types)
        vote.save()
    return JsonResponse({})
