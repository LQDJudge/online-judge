from abc import ABCMeta, abstractmethod, abstractproperty
from django.db.models import Max


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

    @abstractmethod
    def update_participation(self, participation):
        """
        Updates a ContestParticipation object's score, cumtime, and format_data fields based on this contest format.
        Implementations should call ContestParticipation.save().

        :param participation: A ContestParticipation object.
        :return: None
        """
        raise NotImplementedError()

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
