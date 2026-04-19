import copy

from abc import ABCMeta, abstractmethod, abstractproperty
from django.db.models import Max
from django.utils.translation import gettext as _


class abstractclassmethod(classmethod):
    __isabstractmethod__ = True

    def __init__(self, callable):
        callable.__isabstractmethod__ = True
        super(abstractclassmethod, self).__init__(callable)


class BaseContestFormat(metaclass=ABCMeta):
    has_hidden_subtasks = False

    @abstractmethod
    def __init__(self, contest, config):
        self.config = config
        self.contest = contest

    @abstractproperty
    def name(self):
        """
        Name of this contest format. Should be invoked with gettext_lazy.

        :return: str
        """
        raise NotImplementedError()

    @abstractclassmethod
    def validate(cls, config):
        """
        Validates the contest format configuration.

        :param config: A dictionary containing the configuration for this contest format.
        :return: None
        :raises: ValidationError
        """
        raise NotImplementedError()

    def update_participation(self, participation):
        """
        Template method — shared flow for all standard formats.
        Subclasses override gather_results() and optionally compute_score(),
        compute_tiebreaker(), compute_cumtime(). Formats with fundamentally
        different flows (e.g. new_ioi) may override this entirely.

        :param participation: A ContestParticipation object.
        :return: None
        """
        format_data = self.gather_results(participation)
        self.calculate_quiz_scores(participation, format_data)
        self.handle_frozen_state(participation, format_data)

        participation.score = round(
            self.compute_score(format_data),
            self.contest.points_precision,
        )
        participation.cumtime = self.compute_cumtime(format_data)
        participation.tiebreaker = self.compute_tiebreaker(format_data)
        participation.format_data = format_data

        self.apply_result_hidden(participation, format_data)
        participation.save()

    def gather_results(self, participation):
        """
        Query problem submissions and return a populated format_data dict.
        Each key is a string problem_id, each value has at least 'time' and 'points'.
        Must be overridden by formats that use the template update_participation().

        :param participation: A ContestParticipation object.
        :return: dict — the format_data for this participation.
        """
        raise NotImplementedError

    def compute_score(self, format_data, entries=None):
        """
        Compute total score from format_data. Override for bonus points, etc.

        :param format_data: The full format_data dict.
        :param entries: If provided, only consider these keys. If None, use all.
        :return: Computed score value.
        """
        score = 0
        for key, entry in format_data.items():
            if entries is not None and key not in entries:
                continue
            score += entry.get("points", 0)
        return max(score, 0)

    def compute_tiebreaker(self, format_data, entries=None):
        """
        Compute tiebreaker value. Override for ICPC (last solve time).
        Default: 0 (no tiebreaker).

        :param format_data: The full format_data dict.
        :param entries: If provided, only consider these keys. If None, use all.
        :return: Tiebreaker value (lower is better).
        """
        return 0

    @abstractmethod
    def display_user_problem(self, participation, contest_problem, show_final):
        """
        Returns the HTML fragment to show a user's performance on an individual problem. This is expected to use
        information from the format_data field instead of computing it from scratch.

        :param participation: The ContestParticipation object linking the user to the contest.
        :param contest_problem: The ContestProblem object representing the problem in question.
        :return: An HTML fragment, marked as safe for Jinja2.
        """
        raise NotImplementedError()

    @abstractmethod
    def display_participation_result(self, participation, show_final):
        """
        Returns the HTML fragment to show a user's performance on the whole contest. This is expected to use
        information from the format_data field instead of computing it from scratch.

        :param participation: The ContestParticipation object.
        :param show_final: Whether to show final (full) scores or public (hidden-adjusted) scores.
        :return: An HTML fragment, marked as safe for Jinja2.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_problem_breakdown(self, participation, contest_problems):
        """
        Returns a machine-readable breakdown for the user's performance on every problem.

        :param participation: The ContestParticipation object.
        :param contest_problems: The list of ContestProblem objects to display performance for.
        :return: A list of dictionaries, whose content is to be determined by the contest system.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_contest_problem_label_script(self):
        """
        Returns the default Lua script to generate contest problem labels.
        :return: A string, the Lua script.
        """
        raise NotImplementedError()

    @classmethod
    def best_solution_state(cls, points, total):
        if not points:
            return "failed-score"
        if points == total:
            return "full-score"
        return "partial-score"

    def get_problem_tooltip(self, contest_problem):
        """
        Returns the tooltip text for a problem cell in the ranking table.

        :param contest_problem: The ContestProblem object.
        :return: A translated string like "Problem 1".
        """
        return _("Problem %(order)s") % {"order": contest_problem.order}

    def handle_frozen_state(self, participation, format_data):
        hidden_subtasks = {}
        if hasattr(self, "get_hidden_subtasks"):
            hidden_subtasks = self.get_hidden_subtasks()

        queryset = participation.submissions.values("problem_id").annotate(
            time=Max("submission__date")
        )
        for result in queryset:
            problem = str(result["problem_id"])
            if not (self.contest.freeze_after or hidden_subtasks.get(problem)):
                continue
            if format_data.get(problem):
                is_after_freeze = (
                    self.contest.freeze_after
                    and result["time"]
                    >= self.contest.freeze_after + participation.start
                )
                if is_after_freeze or hidden_subtasks.get(problem):
                    format_data[problem]["frozen"] = True
            else:
                format_data[problem] = {"time": 0, "points": 0, "frozen": True}

    def compute_cumtime(self, format_data, entries=None):
        """
        Compute cumtime from format_data entries. Each format can override
        this to match its own cumtime logic (sum vs max, penalty, etc.).

        :param format_data: The full format_data dict.
        :param entries: If provided, only consider these keys. If None, use all.
        :return: Computed cumtime value.
        """
        cumtime = 0
        for key, entry in format_data.items():
            if entries is not None and key not in entries:
                continue
            if entry.get("points", 0) > 0:
                cumtime += entry.get("time", 0)
        return max(cumtime, 0)

    def apply_result_hidden(self, participation, format_data):
        """
        Save full scores as final, then subtract is_result_hidden problems
        from the public score. Must be called after the format sets
        participation.score/cumtime/format_data, but before save().

        For formats that already compute score_final (e.g., new_ioi),
        this preserves their final values and only adjusts the public score.
        """
        from judge.models import ContestProblem

        has_final = self.has_hidden_subtasks  # format already set score_final

        # Save full values as final (if format didn't already)
        if not has_final:
            participation.score_final = participation.score
            participation.cumtime_final = participation.cumtime
            participation.format_data_final = copy.deepcopy(format_data)

        # Find is_result_hidden problems
        hidden_cp_ids = set(
            ContestProblem.objects.filter(
                contest=self.contest, is_result_hidden=True
            ).values_list("id", flat=True)
        )
        if not hidden_cp_ids:
            return

        # Determine which format_data keys are NOT hidden
        non_hidden_keys = set()
        for key in format_data:
            is_hidden = False
            for cp_id in hidden_cp_ids:
                if key in (str(cp_id), f"quiz_{cp_id}"):
                    is_hidden = True
                    break
            if not is_hidden:
                non_hidden_keys.add(key)

        # Recompute public score/cumtime/tiebreaker from non-hidden entries
        participation.score = round(
            self.compute_score(format_data, non_hidden_keys),
            self.contest.points_precision,
        )
        participation.cumtime = self.compute_cumtime(format_data, non_hidden_keys)
        participation.tiebreaker = self.compute_tiebreaker(format_data, non_hidden_keys)

    def calculate_quiz_scores(self, participation, format_data):
        """
        Calculate quiz scores for this participation and add to format_data.
        This method can be called by any contest format to include quiz scores.

        :param participation: The ContestParticipation object.
        :param format_data: Dictionary to store quiz format data.
        :return: Total quiz points earned.
        """
        # Import here to avoid circular imports
        from judge.models import QuizAttempt, ContestProblem

        quiz_points = 0

        # Get all quiz ContestProblems in this contest
        contest_quizzes = ContestProblem.objects.filter(
            contest=self.contest, quiz__isnull=False
        ).select_related("quiz")

        for cp in contest_quizzes:
            # Get best completed attempt for this quiz in this contest participation
            best_attempt = (
                QuizAttempt.objects.filter(
                    quiz=cp.quiz,
                    user=participation.user,
                    contest_participation=participation,
                    is_submitted=True,
                )
                .order_by("-score")
                .first()
            )

            if best_attempt and best_attempt.score is not None:
                # Calculate points based on contest problem points and quiz score ratio
                if best_attempt.max_score and best_attempt.max_score > 0:
                    earned_ratio = float(best_attempt.score) / float(
                        best_attempt.max_score
                    )
                    earned_points = cp.points * earned_ratio
                else:
                    earned_points = 0

                # Store in format_data with 'quiz_' prefix to distinguish from problems
                quiz_key = f"quiz_{cp.id}"
                dt = 0
                if best_attempt.end_time:
                    dt = (best_attempt.end_time - participation.start).total_seconds()

                format_data[quiz_key] = {
                    "time": dt,
                    "points": earned_points,
                    "quiz_score": float(best_attempt.score),
                    "quiz_max_score": (
                        float(best_attempt.max_score) if best_attempt.max_score else 0
                    ),
                    "is_quiz": True,
                }
                quiz_points += earned_points

        return quiz_points
