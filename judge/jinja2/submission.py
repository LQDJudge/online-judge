from . import registry


@registry.function
def submission_layout(
    submission,
    profile_id,
    user,
    editable_problem_ids,
    completed_problem_ids,
    tester_problem_ids,
):
    problem_id = submission.problem_id

    if problem_id in editable_problem_ids:
        return True

    if problem_id in tester_problem_ids:
        return True

    if profile_id == submission.user_id:
        return True

    if user.has_perm("judge.change_submission"):
        return True

    if user.has_perm("judge.view_all_submission"):
        return True

    if submission.problem.is_public and user.has_perm("judge.view_public_submission"):
        return True

    if hasattr(submission, "contest"):
        contest = submission.contest.participation.contest
        if contest.is_editable_by(user):
            return True

    if submission.problem_id in completed_problem_ids and submission.problem.is_public:
        return True

    return False
