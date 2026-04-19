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

# Priorities 0, 1, 2 are user-initiated (official contest, private contest,
# default). Priorities 3, 4 are admin-initiated rejudges. User-tier entries
# are subject to the per-user "at most one judging at a time" cap; admin-tier
# entries bypass the cap.
USER_TIER_THRESHOLD = 3


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
        # sub_id -> (user_id, is_user_tier). Populated on every dispatch,
        # cleared on every completion.
        self.submission_users = {}
        # user_ids with a currently-judging user-tier submission. A user
        # appears here at most once.
        self.running_users = set()
        self.validate_map = {}
        self.lock = RLock()

    @staticmethod
    def _is_user_tier(priority, user_id):
        return priority < USER_TIER_THRESHOLD and user_id is not None

    def _mark_dispatched(self, sub_id, user_id, is_user_tier):
        self.submission_users[sub_id] = (user_id, is_user_tier)
        if is_user_tier:
            self.running_users.add(user_id)

    def _mark_finished(self, sub_id):
        info = self.submission_users.pop(sub_id, None)
        if info is None:
            return
        user_id, is_user_tier = info
        if is_user_tier:
            self.running_users.discard(user_id)

    def _handle_free_judge(self, judge):
        with self.lock:
            current_tier = 0
            node = self.queue.first
            while node is not None:
                next_node = node.next  # save before potential removal
                val = node.value

                if isinstance(val, PriorityMarker):
                    current_tier = val.priority + 1
                    node = next_node
                    continue

                if isinstance(val, ValidateItem):
                    # Validation entries bypass per-user fairness.
                    if val.problem_id in judge.problems:
                        self.validate_map[val.validate_id] = judge
                        logger.info(
                            "Dispatched queued validation %s: %s",
                            val.validate_id,
                            judge.name,
                        )
                        try:
                            judge.submit_validate(val.validate_id, val.problem_id)
                        except Exception:
                            logger.exception(
                                "Failed to dispatch validation %s (%s) to %s",
                                val.validate_id,
                                val.problem_id,
                                judge.name,
                            )
                            self.judges.discard(judge)
                            return
                        self.queue.remove(node)
                        del self.node_map[val.validate_id]
                        return
                    node = next_node
                    continue

                (
                    id,
                    problem,
                    language,
                    source,
                    judge_id_v,
                    user_id,
                    is_user_tier,
                ) = val

                cap_fires = is_user_tier and user_id in self.running_users

                if not cap_fires:
                    if judge.can_judge(problem, language, judge_id_v):
                        self.submission_map[id] = judge
                        self._mark_dispatched(id, user_id, is_user_tier)
                        logger.info(
                            "Dispatched queued submission %d: %s", id, judge.name
                        )
                        try:
                            judge.submit(id, problem, language, source)
                        except VanishedSubmission:
                            del self.submission_map[id]
                            self._mark_finished(id)
                        except Exception:
                            logger.exception(
                                "Failed to dispatch %d (%s, %s) to %s",
                                id,
                                problem,
                                language,
                                judge.name,
                            )
                            del self.submission_map[id]
                            self._mark_finished(id)
                            self.judges.discard(judge)
                            return
                        self.queue.remove(node)
                        del self.node_map[id]
                        return
                    node = next_node
                    continue

                # Cap fires: user-tier submission whose user is already judging.
                if current_tier < self.priorities - 1:
                    # Push down one tier. Re-insert at the tail of the next
                    # tier (just before the marker for that tier).
                    self.queue.remove(node)
                    new_node = self.queue.insert(val, self.priority[current_tier + 1])
                    self.node_map[id] = new_node
                # else: already at the lowest tier; skip in place.
                node = next_node

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
                self._mark_finished(sub)
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
            self._mark_finished(submission)
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

    def judge(self, id, problem, language, source, judge_id, priority, user_id=None):
        with self.lock:
            if id in self.submission_map or id in self.node_map:
                # Already judging, don't queue again. This can happen during batch rejudges, rejudges should be
                # idempotent.
                return

            is_user_tier = self._is_user_tier(priority, user_id)

            if is_user_tier and user_id in self.running_users:
                # User already has a submission judging; push this one down
                # one tier at arrival and do not dispatch to a free judge.
                effective_priority = min(priority + 1, self.priorities - 1)
                self.node_map[id] = self.queue.insert(
                    (id, problem, language, source, judge_id, user_id, is_user_tier),
                    self.priority[effective_priority],
                )
                logger.info(
                    "Queued submission %d at tier %d (user %d already judging)",
                    id,
                    effective_priority,
                    user_id,
                )
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
                self._mark_dispatched(id, user_id, is_user_tier)
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
                    self._mark_finished(id)
                    self.judges.discard(judge)
                    return self.judge(
                        id, problem, language, source, judge_id, priority, user_id
                    )
            else:
                self.node_map[id] = self.queue.insert(
                    (id, problem, language, source, judge_id, user_id, is_user_tier),
                    self.priority[priority],
                )
                logger.info("Queued submission: %d", id)
