import copy
from types import SimpleNamespace

from django.test import SimpleTestCase

from judge.review.snapshots import (
    contest_review_content,
    digest_json,
    reviewed_content_hash,
)


class ContestContentIdentityTests(SimpleTestCase):
    def snapshot(self):
        return {
            "kind": "contest",
            "config": {
                "key": "original",
                "name": "Original title",
                "description": "Contest rules\n",
                "start_time": "2026-10-01T09:00:00+00:00",
                "end_time": "2026-10-01T11:00:00+00:00",
                "format_name": "default",
                "format_config": {},
                "time_limit": 3600,
                "freeze_after": 600,
                "is_visible": False,
                "is_rated": False,
                "summary": "Preview",
            },
            "roles": {"authors": [1], "curators": [], "testers": [2]},
            "items": [
                {"id": 10, "contest_id": 1, "problem_id": 1, "order": 0, "points": 100},
                {"id": 11, "contest_id": 1, "problem_id": 2, "order": 1, "points": 200},
            ],
            "children": [
                {
                    "problem_id": 1,
                    "content_hash": "p1",
                    "review_run_id": 1,
                    "name": "One",
                },
                {
                    "problem_id": 2,
                    "content_hash": "p2",
                    "review_run_id": 2,
                    "owned": True,
                },
            ],
            "quiz_content": {
                "quizzes": [
                    {
                        "id": 4,
                        "code": "quiz",
                        "title": "Quiz title",
                        "description": "Rules",
                        "time_limit": 30,
                    }
                ],
                "assignments": [
                    {"id": 5, "quiz_id": 4, "question_id": 6, "points": 2, "order": 1}
                ],
                "questions": [
                    {
                        "id": 6,
                        "title": "Management label",
                        "content": "2+2?",
                        "correct_answers": {"answers": "4"},
                    }
                ],
            },
            "exposure": {"material_concern": False},
        }

    def test_metadata_and_absolute_rescheduling_do_not_change_identity(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["config"].update(
            key="newslug",
            name="Renamed",
            summary="New preview",
            og_image="cover.png",
            logo_override_image="logo.png",
            is_visible=True,
            is_locked=True,
            is_private=True,
            is_organization_private=True,
            is_in_course=True,
            is_rated=True,
            user_count=100,
            rating_floor=500,
            rating_ceiling=3000,
            rate_all=True,
            has_access_code=True,
            future_admin_field="not opted in",
            start_time="2026-11-01T10:00:00+01:00",
            end_time="2026-11-01T12:00:00+01:00",
            description="Contest rules\r\n",
        )
        after["roles"] = {
            "authors": [3],
            "curators": [4],
            "testers": [5],
            "organizations": [6],
        }
        after["exposure"] = {"material_concern": True}
        for index, item in enumerate(after["items"]):
            item.update(id=100 + index, contest_id=90, order=100 + index)
        after["children"].reverse()
        for child in after["children"]:
            child.update(review_run_id=100, name="Renamed", code="new", owned=False)
        self.assertEqual(contest_review_content(before), contest_review_content(after))

    def test_editor_round_trip_microseconds_do_not_change_duration_identity(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["config"]["end_time"] = "2026-10-01T11:00:00.000011+00:00"
        self.assertEqual(contest_review_content(before), contest_review_content(after))
        after["config"]["end_time"] = "2026-10-01T11:00:01+00:00"
        self.assertNotEqual(
            contest_review_content(before), contest_review_content(after)
        )

    def test_rules_timing_and_judging_settings_are_review_relevant(self):
        before = self.snapshot()
        for key, value in {
            "description": "Different rule",
            "end_time": "2026-10-01T12:00:00+00:00",
            "time_limit": 1800,
            "freeze_after": 900,
            "format_name": "icpc",
            "format_config": {"penalty": 20},
            "points_precision": 3,
            "run_pretests_only": True,
            "scoreboard_visibility": "P",
            "public_scoreboard": True,
            "use_clarifications": False,
            "hide_problem_tags": True,
            "problem_label_script": "return 'X'",
            "rate_limit": 2,
        }.items():
            with self.subTest(key=key):
                after = copy.deepcopy(before)
                after["config"][key] = value
                self.assertNotEqual(
                    contest_review_content(before), contest_review_content(after)
                )

    def test_invalid_or_unparseable_timing_cannot_match_valid_timing(self):
        before = self.snapshot()
        for value in (
            "invalid",
            None,
            "2026-10-01T08:00:00+00:00",
            "2026-10-01T11:00:00",
        ):
            after = copy.deepcopy(before)
            after["config"]["end_time"] = value
            self.assertNotEqual(
                contest_review_content(before), contest_review_content(after)
            )

    def test_item_order_content_and_scoring_changes_are_detected(self):
        before = self.snapshot()
        mutations = [
            lambda s: s["items"].reverse(),
            lambda s: s["items"].pop(),
            lambda s: s["children"][0].update(content_hash="new package"),
        ]
        for key, value in {
            "problem_id": 3,
            "quiz_id": 4,
            "points": 101,
            "initial_ac_score": 5,
            "partial": True,
            "is_pretested": True,
            "show_testcases": True,
            "max_submissions": 10,
            "hidden_subtasks": "1",
            "is_result_hidden": True,
        }.items():
            mutations.append(lambda s, k=key, v=value: s["items"][0].update({k: v}))
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                after = copy.deepcopy(before)
                mutate(after)
                self.assertNotEqual(
                    contest_review_content(before), contest_review_content(after)
                )

    def test_quiz_display_metadata_is_ignored_but_content_is_not(self):
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["quiz_content"]["quizzes"][0].update(
            code="renamed",
            title="Renamed",
            is_public=True,
            is_locked=True,
            created_at="now",
        )
        after["quiz_content"]["assignments"][0].update(id=99, order=50)
        after["quiz_content"]["questions"][0]["title"] = "New management label"
        self.assertEqual(contest_review_content(before), contest_review_content(after))
        for section, key, value in (
            ("quizzes", "time_limit", 60),
            ("quizzes", "description", "New rules"),
            ("assignments", "points", 3),
            ("questions", "content", "3+3?"),
            ("questions", "correct_answers", {"answers": "6"}),
        ):
            with self.subTest(section=section, key=key):
                changed = copy.deepcopy(after)
                changed["quiz_content"][section][0][key] = value
                self.assertNotEqual(
                    contest_review_content(before), contest_review_content(changed)
                )

    def test_legacy_snapshot_is_compared_without_rewriting_history(self):
        snapshot = self.snapshot()
        frozen = copy.deepcopy(snapshot)
        run = SimpleNamespace(snapshot=snapshot, content_hash="old broad hash")
        self.assertEqual(
            reviewed_content_hash(run), digest_json(contest_review_content(snapshot))
        )
        self.assertEqual(snapshot, frozen)
        self.assertEqual(run.content_hash, "old broad hash")
        minimal = SimpleNamespace(
            snapshot={"kind": "contest"}, content_hash="opaque legacy hash"
        )
        self.assertEqual(reviewed_content_hash(minimal), "opaque legacy hash")
