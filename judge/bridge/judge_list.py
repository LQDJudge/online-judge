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

    def _set_judge_submission(self, judge, id, problem, language, source):
        judge._working = id
        judge._working_data = {
            "problem": problem,
            "language": language,
            "source": source,
        }

    def _set_judge_validation(self, judge, validate_id, problem_id):
        judge._working = True
        judge._validating = validate_id
        judge._validating_problem = problem_id

    def _clear_judge_work(self, judge):
        judge._working = False
        judge._working_data = {}
        judge._validating = None
        judge._validating_problem = None
        if "working" in getattr(judge, "__dict__", {}):
            judge.working = False

    def _reserve_free_judge_work(self, judge):
        with self.lock:
            if judge not in self.judges or judge.working:
                return None

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
                        self._set_judge_validation(
                            judge, val.validate_id, val.problem_id
                        )
                        self.queue.remove(node)
                        del self.node_map[val.validate_id]
                        logger.info(
                            "Dispatched queued validation %s: %s",
                            val.validate_id,
                            judge.name,
                        )
                        return ("validation", val, current_tier)
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
                        self._set_judge_submission(judge, id, problem, language, source)
                        self.queue.remove(node)
                        del self.node_map[id]
                        logger.info(
                            "Dispatched queued submission %d: %s", id, judge.name
                        )
                        return ("submission", val, current_tier)
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

        return None

    def _fail_judge(self, judge, clear_work=True):
        with self.lock:
            self.judges.discard(judge)
            if clear_work:
                sub = judge.get_current_submission()
                if sub is not None and self.submission_map.get(sub) is judge:
                    del self.submission_map[sub]
                    self._mark_finished(sub)

                validate_id = judge._validating
                if (
                    validate_id is not None
                    and self.validate_map.get(validate_id) is judge
                ):
                    self.validate_map.pop(validate_id, None)

                self._clear_judge_work(judge)

        try:
            judge.disconnect(force=True)
        except Exception:
            logger.exception("Failed to disconnect failed judge %s", judge.name)

    def _requeue_submission(self, sub, working_data):
        if sub is None or not working_data:
            return
        self.judge(
            sub,
            working_data["problem"],
            working_data["language"],
            working_data["source"],
            None,
            0,
        )

    def _remove_judge_locked(self, judge):
        sub = judge.get_current_submission()
        working_data = {}
        if sub is not None and self.submission_map.get(sub) is judge:
            del self.submission_map[sub]
            self._mark_finished(sub)
            working_data = judge._working_data.copy()
        elif sub is not None:
            logger.warning(
                "Ignoring stale submission ownership for %s on %s",
                sub,
                judge.name,
            )
            sub = None

        validate_id = judge._validating
        if validate_id is not None and self.validate_map.get(validate_id) is judge:
            self.validate_map.pop(validate_id, None)
        elif validate_id is not None:
            logger.warning(
                "Ignoring stale validation ownership for %s on %s",
                validate_id,
                judge.name,
            )

        self.judges.discard(judge)
        return sub, working_data

    def _remove_and_requeue_judge(self, judge):
        with self.lock:
            sub, working_data = self._remove_judge_locked(judge)
            self._clear_judge_work(judge)
        self._requeue_submission(sub, working_data)

    def _release_submission(self, judge, id):
        with self.lock:
            if self.submission_map.get(id) is judge:
                del self.submission_map[id]
                self._mark_finished(id)
            self._clear_judge_work(judge)

    def _release_validation(self, judge, validate_id):
        with self.lock:
            if self.validate_map.get(validate_id) is judge:
                self.validate_map.pop(validate_id, None)
            self._clear_judge_work(judge)

    def _dispatch_reserved_submission(self, judge, item, priority):
        id, problem, language, source, judge_id, user_id, is_user_tier = item
        try:
            judge.submit(id, problem, language, source)
        except VanishedSubmission:
            self._release_submission(judge, id)
            return False
        except Exception:
            logger.exception(
                "Failed to dispatch %d (%s, %s) to %s",
                id,
                problem,
                language,
                judge.name,
            )
            self._release_submission(judge, id)
            self._fail_judge(judge)
            self.judge(id, problem, language, source, judge_id, priority, user_id)
            return False
        return True

    def _dispatch_reserved_validation(self, judge, item, priority):
        try:
            judge.submit_validate(item.validate_id, item.problem_id)
        except Exception:
            logger.exception(
                "Failed to dispatch validation %s (%s) to %s",
                item.validate_id,
                item.problem_id,
                judge.name,
            )
            self._release_validation(judge, item.validate_id)
            self._fail_judge(judge)
            self.validate(item.validate_id, item.problem_id)
            return False
        return True

    def _handle_free_judge(self, judge):
        while True:
            reserved = self._reserve_free_judge_work(judge)
            if reserved is None:
                return

            kind, item, priority = reserved
            if kind == "submission":
                if self._dispatch_reserved_submission(judge, item, priority):
                    return
            else:
                if self._dispatch_reserved_validation(judge, item, priority):
                    return

    def register(self, judge):
        # Disconnect all judges with the same name, see <https://github.com/DMOJ/online-judge/issues/828>
        stale_work = []
        with self.lock:
            stale_judges = list(
                current for current in self.judges if current.name == judge.name
            )
            for current in stale_judges:
                stale_work.append((current, *self._remove_judge_locked(current)))
                self._clear_judge_work(current)
            self.judges.add(judge)
        for current, sub, working_data in stale_work:
            try:
                current.disconnect(force=True)
            except Exception:
                logger.exception("Failed to disconnect stale judge %s", current.name)
            self._requeue_submission(sub, working_data)
        self._handle_free_judge(judge)

    def disconnect(self, judge_id, force=False):
        with self.lock:
            judges = [judge for judge in self.judges if judge.name == judge_id]
        for judge in judges:
            try:
                judge.disconnect(force=force)
            except Exception:
                logger.exception("Failed to disconnect judge %s", judge.name)
                self._remove_and_requeue_judge(judge)

    def update_problems(self, judge):
        self._handle_free_judge(judge)

    def broadcast_update_problems(self):
        """Tell all connected judges to rescan their problem list."""
        with self.lock:
            judges = list(self.judges)
        for judge in judges:
            try:
                judge.send({"name": "update-problems"})
            except Exception:
                logger.exception("Failed to send update-problems to %s", judge.name)
                self._fail_judge(judge, clear_work=False)

    def remove(self, judge):
        with self.lock:
            sub, working_data = self._remove_judge_locked(judge)
            return sub, working_data

    def __iter__(self):
        return iter(self.judges)

    def on_judge_free(self, judge, submission):
        with self.lock:
            logger.info("Judge available after grading %d: %s", submission, judge.name)
            if self.submission_map.get(submission) is judge:
                del self.submission_map[submission]
                self._mark_finished(submission)
            else:
                logger.warning(
                    "Ignoring stale completion for submission %d from %s",
                    submission,
                    judge.name,
                )
            self._clear_judge_work(judge)
        self._handle_free_judge(judge)

    def abort(self, submission):
        with self.lock:
            logger.info("Abort request: %d", submission)
            judge = self.submission_map.get(submission)
            if judge is None:
                try:
                    node = self.node_map[submission]
                except KeyError:
                    pass
                else:
                    self.queue.remove(node)
                    del self.node_map[submission]
                return False
        try:
            judge.abort()
        except Exception:
            logger.exception(
                "Failed to abort submission %d on %s", submission, judge.name
            )
            self._fail_judge(judge, clear_work=False)
        return True

    def validate(self, validate_id, problem_id):
        """Dispatch validation to an available judge or queue it."""
        while True:
            with self.lock:
                if validate_id in self.validate_map or validate_id in self.node_map:
                    return True

                candidates = [
                    judge
                    for judge in self.judges
                    if not judge.working and problem_id in judge.problems
                ]
                if not candidates:
                    # Queue at lowest priority
                    self.node_map[validate_id] = self.queue.insert(
                        ValidateItem(validate_id, problem_id),
                        self.priority[self.priorities - 1],
                    )
                    logger.info("Queued validation: %s", validate_id)
                    return True

                judge = min(candidates, key=attrgetter("load"))
                logger.info("Dispatched validation %s to: %s", validate_id, judge.name)
                item = ValidateItem(validate_id, problem_id)
                self.validate_map[validate_id] = judge
                self._set_judge_validation(judge, validate_id, problem_id)

            if self._dispatch_reserved_validation(judge, item, self.priorities - 1):
                return True

    def on_judge_free_validation(self, judge, validate_id):
        with self.lock:
            logger.info(
                "Judge available after validation %s: %s", validate_id, judge.name
            )
            if self.validate_map.get(validate_id) is judge:
                self.validate_map.pop(validate_id, None)
            else:
                logger.warning(
                    "Ignoring stale validation completion for %s from %s",
                    validate_id,
                    judge.name,
                )
            self._clear_judge_work(judge)
        self._handle_free_judge(judge)

    def check_priority(self, priority):
        return 0 <= priority < self.priorities

    @staticmethod
    def _source_length(source):
        if source is None:
            return 0
        return len(source)

    @staticmethod
    def _source_bytes(source):
        if source is None:
            return 0
        if isinstance(source, bytes):
            return len(source)
        return len(str(source).encode("utf-8"))

    @staticmethod
    def _judge_name(judge):
        return getattr(judge, "name", None)

    @staticmethod
    def _judge_current_submission(judge):
        try:
            return judge.get_current_submission()
        except Exception:
            return getattr(judge, "_working", None) or None

    def _submission_status_entry(self, id, judge, item=None):
        user_id = None
        is_user_tier = None
        if item is not None:
            id, problem, language, source, judge_id, user_id, is_user_tier = item
        else:
            working_data = getattr(judge, "_working_data", {})
            problem = working_data.get("problem")
            language = working_data.get("language")
            source = working_data.get("source")
            judge_id = None
            user_info = self.submission_users.get(id)
            if user_info is not None:
                user_id, is_user_tier = user_info

        return {
            "submission-id": id,
            "judge": self._judge_name(judge),
            "problem": problem,
            "language": language,
            "judge-id": judge_id,
            "user-id": user_id,
            "user-tier": is_user_tier,
            "source-length": self._source_length(source),
            "source-bytes": self._source_bytes(source),
        }

    def _validation_status_entry(self, validate_id, judge, item=None):
        problem_id = (
            item.problem_id
            if item is not None
            else getattr(judge, "_validating_problem", None)
        )
        return {
            "validate-id": validate_id,
            "judge": self._judge_name(judge),
            "problem-id": problem_id,
        }

    def _judge_status_entry(self, judge, include_problems):
        executors = getattr(judge, "executors", {})
        executor_keys = list(
            executors.keys() if hasattr(executors, "keys") else executors
        )
        problems = sorted(getattr(judge, "problems", ()))
        entry = {
            "name": self._judge_name(judge),
            "working": bool(getattr(judge, "working", False)),
            "current-submission": self._judge_current_submission(judge),
            "current-validation": getattr(judge, "_validating", None),
            "current-validation-problem": getattr(judge, "_validating_problem", None),
            "load": getattr(judge, "load", None),
            "latency": getattr(judge, "latency", None),
            "problem-count": len(problems),
            "executor-count": len(executor_keys),
            "executors": sorted(executor_keys),
            "address": getattr(judge, "judge_address", None),
            "client-address": getattr(judge, "client_address", None),
        }
        if include_problems:
            entry["problems"] = problems
        return entry

    def _queue_status_entries(self):
        entries = []
        current_tier = 0
        node = self.queue.first
        while node is not None:
            value = node.value
            if isinstance(value, PriorityMarker):
                current_tier = value.priority + 1
            elif isinstance(value, ValidateItem):
                entry = self._validation_status_entry(value.validate_id, None, value)
                entry.update({"type": "validation", "priority": current_tier})
                entries.append(entry)
            else:
                entry = self._submission_status_entry(value[0], None, value)
                entry.update({"type": "submission", "priority": current_tier})
                entries.append(entry)
            node = node.next
        return entries

    def judge(self, id, problem, language, source, judge_id, priority, user_id=None):
        while True:
            with self.lock:
                if id in self.submission_map or id in self.node_map:
                    # Already judging, don't queue again. This can happen during batch rejudges, rejudges should be
                    # idempotent.
                    return

                is_user_tier = self._is_user_tier(priority, user_id)
                item = (id, problem, language, source, judge_id, user_id, is_user_tier)

                if is_user_tier and user_id in self.running_users:
                    # User already has a submission judging; push this one down
                    # one tier at arrival and do not dispatch to a free judge.
                    effective_priority = min(priority + 1, self.priorities - 1)
                    self.node_map[id] = self.queue.insert(
                        item,
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
                    if not judge.working
                    and judge.can_judge(problem, language, judge_id)
                ]
                if judge_id:
                    logger.info(
                        "Specified judge %s is%savailable",
                        judge_id,
                        " " if candidates else " not ",
                    )
                else:
                    logger.info("Free judges: %d", len(candidates))

                if not candidates:
                    self.node_map[id] = self.queue.insert(
                        item,
                        self.priority[priority],
                    )
                    logger.info("Queued submission: %d", id)
                    return

                # Schedule the submission on the judge reporting least load.
                judge = min(candidates, key=attrgetter("load"))
                logger.info("Dispatched submission %d to: %s", id, judge.name)
                self.submission_map[id] = judge
                self._mark_dispatched(id, user_id, is_user_tier)
                self._set_judge_submission(judge, id, problem, language, source)

            if self._dispatch_reserved_submission(judge, item, priority):
                return

    def status(self, detail=False, include_problems=False):
        with self.lock:
            queued_submissions = 0
            queued_validations = 0
            node = self.queue.first
            while node is not None:
                value = node.value
                if isinstance(value, ValidateItem):
                    queued_validations += 1
                elif not isinstance(value, PriorityMarker):
                    queued_submissions += 1
                node = node.next

            status = {
                "judges": len(self.judges),
                "queued-submissions": queued_submissions,
                "active-submissions": len(self.submission_map),
                "queued-validations": queued_validations,
                "active-validations": len(self.validate_map),
            }
            if not detail:
                return status

            status.update(
                {
                    "judges-detail": [
                        self._judge_status_entry(judge, include_problems)
                        for judge in sorted(self.judges, key=attrgetter("name"))
                    ],
                    "queue": self._queue_status_entries(),
                    "active-submissions-detail": [
                        self._submission_status_entry(id, judge)
                        for id, judge in sorted(self.submission_map.items())
                    ],
                    "active-validations-detail": [
                        self._validation_status_entry(validate_id, judge)
                        for validate_id, judge in sorted(
                            self.validate_map.items(), key=lambda item: str(item[0])
                        )
                    ],
                    "running-users": sorted(self.running_users),
                    "node-map-size": len(self.node_map),
                    "submission-map-size": len(self.submission_map),
                    "validate-map-size": len(self.validate_map),
                }
            )
            return status
