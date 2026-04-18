import logging
from collections import namedtuple
from operator import attrgetter
from threading import RLock

from judge.bridge.utils import VanishedSubmission

try:
    from llist import dllist
except ImportError:
    from pyllist import dllist

logger = logging.getLogger("judge.bridge")

PriorityMarker = namedtuple("PriorityMarker", "priority")
ValidateItem = namedtuple("ValidateItem", "validate_id problem_id")


class JudgeList(object):
    priorities = 5

    def __init__(self):
        self.queue = dllist()
        self.priority = [
            self.queue.append(PriorityMarker(i)) for i in range(self.priorities)
        ]
        self.judges = set()
        self.node_map = {}
        self.submission_map = {}
        self.validate_map = {}
        self.lock = RLock()

    def _handle_free_judge(self, judge):
        with self.lock:
            node = self.queue.first
            while node:
                if not isinstance(node.value, PriorityMarker):
                    if isinstance(node.value, ValidateItem):
                        item = node.value
                        if item.problem_id in judge.problems:
                            self.validate_map[item.validate_id] = judge
                            logger.info(
                                "Dispatched queued validation %s: %s",
                                item.validate_id,
                                judge.name,
                            )
                            try:
                                judge.submit_validate(item.validate_id, item.problem_id)
                            except Exception:
                                logger.exception(
                                    "Failed to dispatch validation %s (%s) to %s",
                                    item.validate_id,
                                    item.problem_id,
                                    judge.name,
                                )
                                self.judges.remove(judge)
                                return
                            self.queue.remove(node)
                            del self.node_map[item.validate_id]
                            break
                    else:
                        id, problem, language, source, judge_id = node.value
                        if judge.can_judge(problem, language, judge_id):
                            self.submission_map[id] = judge
                            logger.info(
                                "Dispatched queued submission %d: %s", id, judge.name
                            )
                            try:
                                judge.submit(id, problem, language, source)
                            except VanishedSubmission:
                                del self.submission_map[id]
                            except Exception:
                                logger.exception(
                                    "Failed to dispatch %d (%s, %s) to %s",
                                    id,
                                    problem,
                                    language,
                                    judge.name,
                                )
                                del self.submission_map[id]
                                self.judges.discard(judge)
                                return
                            self.queue.remove(node)
                            del self.node_map[id]
                            break
                node = node.next

    def register(self, judge):
        with self.lock:
            # Disconnect all judges with the same name, see <https://github.com/DMOJ/online-judge/issues/828>
            self.disconnect(judge, force=True)
            self.judges.add(judge)
            self._handle_free_judge(judge)

    def disconnect(self, judge_id, force=False):
        with self.lock:
            for judge in self.judges:
                if judge.name == judge_id:
                    judge.disconnect(force=force)

    def update_problems(self, judge):
        with self.lock:
            self._handle_free_judge(judge)

    def broadcast_update_problems(self):
        """Tell all connected judges to rescan their problem list."""
        with self.lock:
            for judge in self.judges:
                try:
                    judge.send({"name": "update-problems"})
                except Exception:
                    logger.exception("Failed to send update-problems to %s", judge.name)

    def remove(self, judge):
        with self.lock:
            sub = judge.get_current_submission()
            working_data = {}
            if sub is not None:
                try:
                    del self.submission_map[sub]
                except KeyError:
                    pass
                working_data = judge._working_data.copy()

            validate_id = judge._validating
            if validate_id is not None:
                self.validate_map.pop(validate_id, None)

            self.judges.discard(judge)
            return sub, working_data

    def __iter__(self):
        return iter(self.judges)

    def on_judge_free(self, judge, submission):
        with self.lock:
            logger.info("Judge available after grading %d: %s", submission, judge.name)
            del self.submission_map[submission]
            judge._working = False
            judge._working_data = {}
            self._handle_free_judge(judge)

    def abort(self, submission):
        with self.lock:
            logger.info("Abort request: %d", submission)
            try:
                self.submission_map[submission].abort()
                return True
            except KeyError:
                try:
                    node = self.node_map[submission]
                except KeyError:
                    pass
                else:
                    self.queue.remove(node)
                    del self.node_map[submission]
                return False

    def validate(self, validate_id, problem_id):
        """Dispatch validation to an available judge or queue it."""
        with self.lock:
            if validate_id in self.validate_map or validate_id in self.node_map:
                return True

            candidates = [
                judge
                for judge in self.judges
                if not judge.working and problem_id in judge.problems
            ]
            if candidates:
                judge = min(candidates, key=attrgetter("load"))
                logger.info("Dispatched validation %s to: %s", validate_id, judge.name)
                self.validate_map[validate_id] = judge
                try:
                    judge.submit_validate(validate_id, problem_id)
                except Exception:
                    logger.exception(
                        "Failed to dispatch validation %s (%s) to %s",
                        validate_id,
                        problem_id,
                        judge.name,
                    )
                    self.judges.discard(judge)
                    return self.validate(validate_id, problem_id)
            else:
                # Queue at lowest priority
                self.node_map[validate_id] = self.queue.insert(
                    ValidateItem(validate_id, problem_id),
                    self.priority[self.priorities - 1],
                )
                logger.info("Queued validation: %s", validate_id)
            return True

    def on_judge_free_validation(self, judge, validate_id):
        with self.lock:
            logger.info(
                "Judge available after validation %s: %s", validate_id, judge.name
            )
            self.validate_map.pop(validate_id, None)
            judge._working = False
            judge._working_data = {}
            self._handle_free_judge(judge)

    def check_priority(self, priority):
        return 0 <= priority < self.priorities

    def judge(self, id, problem, language, source, judge_id, priority):
        with self.lock:
            if id in self.submission_map or id in self.node_map:
                # Already judging, don't queue again. This can happen during batch rejudges, rejudges should be
                # idempotent.
                return

            candidates = [
                judge
                for judge in self.judges
                if not judge.working and judge.can_judge(problem, language, judge_id)
            ]
            if judge_id:
                logger.info(
                    "Specified judge %s is%savailable",
                    judge_id,
                    " " if candidates else " not ",
                )
            else:
                logger.info("Free judges: %d", len(candidates))
            if candidates:
                # Schedule the submission on the judge reporting least load.
                judge = min(candidates, key=attrgetter("load"))
                logger.info("Dispatched submission %d to: %s", id, judge.name)
                self.submission_map[id] = judge
                try:
                    judge.submit(id, problem, language, source)
                except Exception:
                    logger.exception(
                        "Failed to dispatch %d (%s, %s) to %s",
                        id,
                        problem,
                        language,
                        judge.name,
                    )
                    del self.submission_map[id]
                    self.judges.discard(judge)
                    return self.judge(id, problem, language, source, judge_id, priority)
            else:
                self.node_map[id] = self.queue.insert(
                    (id, problem, language, source, judge_id),
                    self.priority[priority],
                )
                logger.info("Queued submission: %d", id)
