"""
Tests for JudgeList per-user judge-dispatch fairness.

Covers arrival-time push-down, walker push-down, completion-path bookkeeping,
and admin-tier (rejudge) exemption from the cap.
"""

from unittest import TestCase

from judge.bridge.judge_list import JudgeList


class FakeJudge:
    """Minimal judge stub for JudgeList testing."""

    def __init__(self, name="j", problems=("p1",), languages=("PY3",)):
        self.name = name
        self.problems = set(problems)
        self.languages = set(languages)
        self.working = False
        self.load = 0
        self._working_data = {}
        self._validating = None
        self.submitted = []

    def can_judge(self, problem, language, judge_id):
        if judge_id and judge_id != self.name:
            return False
        return problem in self.problems and language in self.languages

    def submit(self, id, problem, language, source):
        self.working = True
        self.submitted.append((id, problem, language))

    def submit_validate(self, validate_id, problem_id):
        self.working = True
        self._validating = validate_id

    def get_current_submission(self):
        return self.submitted[-1][0] if self.submitted else None

    def abort(self):
        pass

    def disconnect(self, force=False):
        pass

    def send(self, data):
        pass


def queued_ids_by_tier(jl):
    """Return a dict of tier -> list of submission ids, ordered head-to-tail."""
    result = {i: [] for i in range(jl.priorities)}
    current_tier = 0
    node = jl.queue.first
    while node is not None:
        val = node.value
        if hasattr(val, "priority"):  # PriorityMarker
            current_tier = val.priority + 1
        elif isinstance(val, tuple) and len(val) == 7:
            result[current_tier].append(val[0])
        node = node.next
    return result


class JudgeListFairnessTests(TestCase):
    def setUp(self):
        self.jl = JudgeList()

    def _make_judge(self, name="j", busy=False):
        j = FakeJudge(name=name)
        j.working = busy
        self.jl.judges.add(j)
        return j

    # Arrival-time behavior

    def test_user_tier_no_running_dispatches(self):
        j = self._make_judge()
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.assertIs(self.jl.submission_map.get(1), j)
        self.assertIn(10, self.jl.running_users)
        self.assertEqual(self.jl.submission_users[1], (10, True))

    def test_second_user_submission_queued_even_with_free_judge(self):
        """Cap: user with a running submission cannot grab a second free judge."""
        j1 = self._make_judge(name="j1")
        j2 = self._make_judge(name="j2")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # j2 is still free; second submission from same user must queue.
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        # Sub 1 went to one of the two judges; the other should remain idle.
        self.assertIn(1, self.jl.submission_map)
        self.assertNotIn(2, self.jl.submission_map)
        self.assertIn(2, self.jl.node_map)
        # Pushed down from pri 0 to pri 1 at arrival.
        self.assertIn(2, queued_ids_by_tier(self.jl)[1])
        busy_count = sum(1 for j in (j1, j2) if j.working)
        self.assertEqual(busy_count, 1)

    def test_arrival_pushdown_caps_at_tier_4(self):
        self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # Second submission already at pri 4 — can't push beyond.
        self.jl.judge(2, "p1", "PY3", "src", None, 4, user_id=10)
        self.assertIn(2, queued_ids_by_tier(self.jl)[4])

    def test_different_users_both_dispatch(self):
        j1 = self._make_judge(name="j1")
        j2 = self._make_judge(name="j2")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=20)
        self.assertIn(1, self.jl.submission_map)
        self.assertIn(2, self.jl.submission_map)
        self.assertNotEqual(self.jl.submission_map[1], self.jl.submission_map[2])
        self.assertEqual(self.jl.running_users, {10, 20})

    # Admin rejudge exemption

    def test_admin_rejudge_dispatches_even_if_user_running(self):
        j1 = self._make_judge(name="j1")
        j2 = self._make_judge(name="j2")
        # User 10 has a live submission judging.
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # Admin batch-rejudges an old submission from user 10. Must dispatch freely.
        self.jl.judge(99, "p1", "PY3", "src", None, 4, user_id=10)
        self.assertIn(99, self.jl.submission_map)
        self.assertNotEqual(self.jl.submission_map[1], self.jl.submission_map[99])
        self.assertEqual(self.jl.submission_users[99], (10, False))
        # Admin dispatch should not touch running_users.
        self.assertEqual(self.jl.running_users, {10})

    def test_admin_rejudge_queues_but_does_not_cap(self):
        # No free judges.
        j1 = self._make_judge(name="j1", busy=True)
        self.jl.judge(99, "p1", "PY3", "src", None, 3, user_id=10)
        self.assertIn(99, self.jl.node_map)
        # Must be queued at pri 3 (not pushed), since admin entries aren't capped.
        self.assertIn(99, queued_ids_by_tier(self.jl)[3])

    # Completion paths

    def test_on_judge_free_releases_running_user(self):
        j = self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.assertIn(10, self.jl.running_users)
        self.jl.on_judge_free(j, 1)
        self.assertNotIn(10, self.jl.running_users)
        self.assertNotIn(1, self.jl.submission_users)

    def test_remove_releases_running_user(self):
        j = self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.remove(j)
        self.assertNotIn(10, self.jl.running_users)

    def test_abort_queued_does_not_touch_running_users(self):
        # Sub 1 is dispatched (user 10 running). Sub 2 is queued (pushed to pri 1).
        self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.abort(2)
        # User 10 still has sub 1 running.
        self.assertIn(10, self.jl.running_users)
        self.assertNotIn(2, self.jl.node_map)

    def test_admin_completion_does_not_decrement_running_users(self):
        """Completing an admin rejudge for user X must leave running_users untouched."""
        j1 = self._make_judge(name="j1")
        j2 = self._make_judge(name="j2")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)  # user-tier
        self.jl.judge(99, "p1", "PY3", "src", None, 4, user_id=10)  # admin
        self.jl.on_judge_free(j2, 99)
        # User 10 still has sub 1 running.
        self.assertIn(10, self.jl.running_users)
        self.assertIn(1, self.jl.submission_users)

    # Walker behavior

    def test_legit_user_dispatched_ahead_of_spammer_queued(self):
        """When judge frees up, a legit user's queued submission wins over a
        spammer's pushed-down entries."""
        j1 = self._make_judge(name="j1")
        # Spammer user 10 has sub 1 running, sub 2 and sub 3 queued (pushed to pri 1).
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.judge(3, "p1", "PY3", "src", None, 0, user_id=10)
        # Legit user 20 submits; no free judge (j1 is busy), so it queues at pri 0.
        self.jl.judge(4, "p1", "PY3", "src", None, 0, user_id=20)
        # j1 finishes sub 1 → walker runs.
        self.jl.on_judge_free(j1, 1)
        # Sub 4 (user 20) should be dispatched next, not sub 2 or 3.
        self.assertEqual(self.jl.submission_map.get(4), j1)
        self.assertNotIn(2, self.jl.submission_map)
        self.assertNotIn(3, self.jl.submission_map)

    def test_walker_pushdown_of_entry_that_arrived_before_user_ran(self):
        """Sub arrives when user has no running submission → queued at pri 0.
        Before walker picks it, the user starts running a different sub.
        Walker must push-down the queued sub instead of dispatching it."""
        j1 = self._make_judge(name="j1", busy=True)  # no free judge at start
        # Sub 1 from user 10: no free judge, user not running → queued at pri 0.
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # Free j1, then queue another: sub 1 dispatched.
        j1.working = False
        self.jl._handle_free_judge(j1)
        self.assertEqual(self.jl.submission_map.get(1), j1)
        # Now sub 1 is running. A second submission from user 10 arrives while
        # j1 is busy → pushed down to pri 1 at arrival.
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        self.assertIn(2, queued_ids_by_tier(self.jl)[1])

    def test_walker_pushdown_settles_at_tier_4(self):
        """Repeated walks with a single spammer alone: entries cascade to tier 4
        and stay there."""
        j1 = self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # Second sub arrives → pushed to pri 1 at arrival.
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        self.assertIn(2, queued_ids_by_tier(self.jl)[1])
        # j1 remains busy. Walker runs (no judge is free, but invoke it directly
        # by pretending another free judge exists).
        j2 = self._make_judge(name="j2")  # second judge, can handle same problem
        # Walker runs for j2; sub 2 is still capped (user 10 running), so pushed
        # down to pri 2, then 3, then 4 in a single walk.
        self.jl._handle_free_judge(j2)
        # j2 should not have dispatched anything.
        self.assertFalse(j2.working)
        # Sub 2 should have cascaded to tier 4.
        self.assertIn(2, queued_ids_by_tier(self.jl)[4])

    def test_walker_pushdown_cap_at_tier_4_skips_in_place(self):
        """Once a user-tier entry has cascaded to tier 4, subsequent walks must
        skip it in place (user still running) rather than re-pushing."""
        j1 = self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=10)
        # First walk cascades sub 2 to tier 4.
        j2 = self._make_judge(name="j2")
        self.jl._handle_free_judge(j2)
        self.assertIn(2, queued_ids_by_tier(self.jl)[4])
        # Second walk with a fresh free judge: sub 2 should stay at tier 4.
        j3 = self._make_judge(name="j3")
        self.jl._handle_free_judge(j3)
        self.assertIn(2, queued_ids_by_tier(self.jl)[4])
        self.assertFalse(j3.working)

    # user_id = None (backward compat)

    def test_none_user_id_bypasses_cap(self):
        """Submissions without a user_id are treated as non-cap-eligible."""
        j1 = self._make_judge(name="j1")
        j2 = self._make_judge(name="j2")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=None)
        self.jl.judge(2, "p1", "PY3", "src", None, 0, user_id=None)
        # Both dispatched to different judges (no cap).
        self.assertIn(1, self.jl.submission_map)
        self.assertIn(2, self.jl.submission_map)
        self.assertNotEqual(self.jl.submission_map[1], self.jl.submission_map[2])
        self.assertEqual(self.jl.running_users, set())

    # Re-dispatch idempotence

    def test_duplicate_submission_noop(self):
        j1 = self._make_judge(name="j1")
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        # Second call with same id should be a no-op.
        self.jl.judge(1, "p1", "PY3", "src", None, 0, user_id=10)
        self.assertEqual(len(j1.submitted), 1)
