import json
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from asgiref.sync import sync_to_async

from judge.management.commands.generate_magazine_posts import Command, PracticeProblem
from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Organization,
    Problem,
    ProblemGroup,
    ProblemType,
    Profile,
)


class MagazineGenerationPublicContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="magazine",
            defaults={"full_name": "Magazine"},
        )
        cls.user = User.objects.create_user("magazine_user")
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )

    def _problem(self, code, *, is_public=True, is_organization_private=False):
        return Problem.objects.create(
            code=code,
            name="Problem %s" % code,
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10.0,
            is_public=is_public,
            is_organization_private=is_organization_private,
            description=("Problem statement for %s. " % code) * 20,
            user_count=20,
        )

    def _problem_type(self, name, full_name):
        problem_type, _ = ProblemType.objects.get_or_create(
            name=name, defaults={"full_name": full_name}
        )
        return problem_type

    def _contest(self, key, **kwargs):
        now = timezone.now()
        defaults = {
            "name": "Contest %s" % key,
            "start_time": now - timedelta(hours=3),
            "end_time": now - timedelta(hours=1),
            "is_visible": True,
            "is_private": False,
            "is_organization_private": False,
            "is_in_course": False,
        }
        defaults.update(kwargs)
        return Contest.objects.create(key=key, **defaults)

    def _community(self, slug, name, about=""):
        return Organization.objects.create(
            name=name,
            slug=slug,
            short_name=slug[:20],
            about=about,
            registrant=self.profile,
            is_community=True,
        )

    def _agent_selection_responses(self, query, *codes):
        return [
            json.dumps(
                {
                    "tool_call": {
                        "name": "search_public_problems",
                        "arguments": {"query": query},
                    }
                }
            ),
            *(
                json.dumps(
                    {
                        "tool_call": {
                            "name": "get_problem_statement",
                            "arguments": {"code": code},
                        }
                    }
                )
                for code in codes
            ),
            json.dumps(
                {
                    "codes": list(codes),
                    "evidence": {
                        code: "The statement requires the requested transition and query."
                        for code in codes
                    },
                }
            ),
        ]

    def _agent_problem_tools(self, codes):
        def tools(org, expected_difficulties, read_codes):
            @sync_to_async
            def search_public_problems(query, difficulty=None):
                return json.dumps([{"code": code} for code in codes])

            @sync_to_async
            def get_problem_statement(code):
                read_codes.add(code)
                return "Public problem statement"

            search_public_problems.__name__ = "search_public_problems"
            get_problem_statement.__name__ = "get_problem_statement"
            return [search_public_problems, get_problem_statement]

        return tools

    def test_public_contest_queryset_excludes_scoped_contests(self):
        public_contest = self._contest("public")
        self._contest("hidden", is_visible=False)
        self._contest("private-users", is_private=True)
        self._contest("private-org", is_organization_private=True)
        self._contest("course-contest", is_in_course=True)

        contests = list(Command()._base_public_contest_queryset())

        self.assertEqual(contests, [public_contest])

    def test_contest_discussion_org_mixed_plan_is_contest_only(self):
        org = self._community(
            "thao-luan-ky-thi",
            "Thảo luận kỳ thi",
            "Nơi thảo luận các kỳ thi.",
        )

        plan = Command()._mixed_post_plan(3, None, org)

        self.assertEqual(plan, ["contest", "contest", "contest"])

    def test_contest_prompt_uses_only_public_problems(self):
        contest = self._contest("contest")
        public_problem = self._problem("public-problem")
        private_problem = self._problem("private-problem", is_public=False)
        org_private_problem = self._problem(
            "org-private-problem", is_organization_private=True
        )
        ContestProblem.objects.create(
            contest=contest, problem=public_problem, points=100, order=1
        )
        ContestProblem.objects.create(
            contest=contest, problem=private_problem, points=100, order=2
        )
        ContestProblem.objects.create(
            contest=contest, problem=org_private_problem, points=100, order=3
        )
        command = Command()

        with patch.object(
            command, "_call_with_validation", return_value="generated content"
        ) as call_with_validation:
            command._generate_contest_post(None, contest)

        prompt = call_with_validation.call_args.args[1]
        self.assertIn("public-problem", prompt)
        self.assertNotIn("private-problem", prompt)
        self.assertNotIn("org-private-problem", prompt)

    @override_settings(USE_ML=True)
    @patch("judge.management.commands.generate_magazine_posts.search_problems")
    def test_knowledge_share_topic_prompt_includes_public_practice_problems(
        self, search_problems
    ):
        org = self._community(
            "tai-lieu-hoc-tap",
            "Tài liệu học tập",
            "Chia sẻ kiến thức và tài liệu học tập.",
        )
        public_problem = self._problem("prefix-public")
        second_public_problem = self._problem("count-public")
        private_problem = self._problem("private-hit", is_public=False)
        search_problems.return_value = [
            {
                "code": public_problem.code,
                "name": public_problem.name,
                "url": "/problem/%s" % public_problem.code,
                "points": public_problem.points,
                "types": [],
                "score": 0.9,
            },
            {
                "code": private_problem.code,
                "name": private_problem.name,
                "url": "/problem/%s" % private_problem.code,
                "points": private_problem.points,
                "types": [],
                "score": 0.8,
            },
            {
                "code": second_public_problem.code,
                "name": second_public_problem.name,
                "url": "/problem/%s" % second_public_problem.code,
                "points": second_public_problem.points,
                "types": [],
                "score": 0.7,
            },
        ]
        command = Command()
        service = MagicMock()
        service.call_llm_with_history.side_effect = self._agent_selection_responses(
            "mảng cộng dồn truy vấn tổng đoạn", "prefix-public", "count-public"
        )
        guide = command._topic_example_guide("Mảng cộng dồn", org)

        with patch.object(
            command,
            "_public_problem_tool_executables",
            self._agent_problem_tools(["prefix-public", "private-hit", "count-public"]),
        ):
            practice_problems = command._topic_practice_problems(
                service, "Mảng cộng dồn", org, guide
            )
        prompt = command._topic_prompt("Mảng cộng dồn", org, guide, practice_problems)

        search_problems.assert_not_called()
        self.assertEqual(
            [problem.code for problem in practice_problems],
            [public_problem.code, second_public_problem.code],
        )
        self.assertIn("PRACTICE_PROBLEMS_JSON", prompt)
        self.assertIn("/problem/prefix-public", prompt)
        self.assertIn("/problem/count-public", prompt)
        self.assertNotIn("/problem/private-hit", prompt)

    @override_settings(USE_ML=True)
    @patch("judge.management.commands.generate_magazine_posts.search_problems")
    def test_technical_topic_in_voi_org_includes_practice_problems(
        self, search_problems
    ):
        org = self._community(
            "hoc-sinh-gioi-quoc-gia-voi",
            "Học sinh giỏi Quốc gia (VOI)",
            "Ôn luyện thuật toán nâng cao.",
        )
        ds_only_problem = self._problem("range-min")
        ds_only_problem.types.add(
            self._problem_type("segtree", "segtree-general (cây phân đoạn)")
        )
        problem = self._problem("dp-segtree")
        problem.types.add(
            self._problem_type("dp", "dp-general (quy hoạch động cơ bản)"),
            self._problem_type("fenwick", "fenwick-tree (BIT)"),
        )
        second_problem = self._problem("dp-fenwick")
        second_problem.types.add(
            self._problem_type("dp-2", "dp-general (quy hoạch động cơ bản)")
        )
        command = Command()
        service = MagicMock()
        service.call_llm_with_history.side_effect = self._agent_selection_responses(
            "quy hoạch động tối ưu bằng fenwick segment tree",
            "dp-segtree",
            "dp-fenwick",
        )
        topic = "Khi nào nên dùng cấu trúc dữ liệu để tối ưu quy hoạch động?"
        guide = command._topic_example_guide(topic, org)

        with patch.object(
            command,
            "_public_problem_tool_executables",
            self._agent_problem_tools(["range-min", "dp-segtree", "dp-fenwick"]),
        ):
            practice_problems = command._topic_practice_problems(
                service, topic, org, guide
            )

        search_problems.assert_not_called()
        self.assertEqual(
            [problem.code for problem in practice_problems],
            ["dp-segtree", "dp-fenwick"],
        )

    @override_settings(USE_ML=True)
    @patch("judge.management.commands.generate_magazine_posts.search_problems")
    def test_problem_candidate_fallback_uses_static_queries(self, search_problems):
        org = self._community(
            "tin-hoc-thpt",
            "Tin học THPT",
            "Ôn luyện thuật toán cấp trung học phổ thông.",
        )
        problem = self._problem("agent-query-hit")
        search_problems.return_value = [
            {
                "code": problem.code,
                "name": problem.name,
                "url": "/problem/%s" % problem.code,
                "points": problem.points,
                "types": [],
                "score": 0.9,
            },
        ]
        command = Command()
        service = MagicMock()

        command._semantic_problem_candidates(None, "easy", set(), org)

        search_problems.assert_has_calls(
            [
                call("bài lập trình cơ bản vòng lặp mảng xâu đếm sắp xếp", limit=25),
                call(
                    "beginner programming loops arrays strings counting sorting",
                    limit=25,
                ),
            ]
        )
        service.call_llm.assert_not_called()

    @override_settings(USE_ML=True)
    @patch("judge.management.commands.generate_magazine_posts.search_problems")
    def test_contest_discussion_topic_skips_practice_search(self, search_problems):
        org = self._community(
            "thao-luan-ky-thi",
            "Thảo luận kỳ thi",
            "Nơi thảo luận các kỳ thi.",
        )
        command = Command()
        service = MagicMock()
        guide = command._topic_example_guide("Mảng cộng dồn", org)

        practice_problems = command._topic_practice_problems(
            service, "Mảng cộng dồn", org, guide
        )

        self.assertEqual(practice_problems, [])
        service.call_llm.assert_not_called()
        search_problems.assert_not_called()

    def test_topic_validation_requires_supplied_practice_problem_link(self):
        command = Command()
        guide = command._topic_example_guide("Mảng cộng dồn", None)
        practice_problems = [
            PracticeProblem(
                code="prefix-public",
                name="Prefix Public",
                url="/problem/prefix-public",
                points=800,
                types=[],
            )
        ]

        errors = command._validate_topic_post(
            "**Tóm tắt:** Một bài viết thử.\n\nVí dụ nhỏ có mảng cộng dồn.",
            "Mảng cộng dồn",
            guide,
            practice_problems,
        )

        self.assertIn(
            "Bài topic cần gợi ý ít nhất một bài áp dụng từ PRACTICE_PROBLEMS_JSON",
            errors,
        )
