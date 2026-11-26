"""Renderer-backed regressions for generation and assessment guidance."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from bs4 import BeautifulSoup

from ai_features.markdown_improver import MarkdownImprover
from ai_features.markdown_output import clean_markdown_response
from ai_features.solution_generator import SolutionGenerator
from judge.auto_review.phases import _call, build_stage_input
from judge.auto_review.review_guidelines import SECTION_GUIDANCE, TECHNICAL_REVIEW_RULES
from judge.chatbot.tools import _get_checker_template
from judge.markdown import _render_markdown
from llm_service.prompt_guidelines import (
    MARKDOWN_FORMATTING_RULES,
    PROBLEM_STATEMENT_FORMAT_RULES,
    STATEMENT_REVIEW_RULES,
    get_markdown_rules_for_prompt,
)


class GeneratedMarkdownTests(SimpleTestCase):
    def test_preserves_final_sample_fence_and_content(self):
        text = "#### Example\n\n```sample\n1  2\n3\n```"
        self.assertEqual(clean_markdown_response(text), text)
        self.assertIn("1  2\n3", _render_markdown(clean_markdown_response(text)))

    def test_unclosed_sample_retains_significant_spaces(self):
        self.assertEqual(
            clean_markdown_response("```sample\nvalue  "), "```sample\nvalue  \n```"
        )
        self.assertEqual(clean_markdown_response(" \n "), "")

    def test_explicit_wrapper_preserves_inner_fences(self):
        text = "#### Example\n\n```sample\n1\n```"
        self.assertEqual(
            clean_markdown_response("````markdown\n" + text + "\n````"), text
        )
        self.assertEqual(
            clean_markdown_response("```markdown\n" + text + "\n```"), text
        )

    def test_closes_nested_fence_at_its_own_indentation(self):
        text = '!!! question "Test 1"\n    ???+ "Input"\n        ```sample\n        1'
        cleaned = clean_markdown_response(text)
        self.assertTrue(cleaned.endswith("\n        ```"))
        self.assertIn("1", _render_markdown(cleaned))
        self.assertEqual(clean_markdown_response(cleaned), cleaned)

    def test_tildes_and_backticks_inside_code_are_preserved(self):
        for text in (
            '~~~python\nprint("```")\n~~~',
            "````text\n```\n````",
            "Use ` ``` ` as text.",
        ):
            with self.subTest(text=text):
                self.assertEqual(clean_markdown_response(text), text)

    def test_improver_uses_template_without_invented_scoring(self):
        with patch("ai_features.markdown_improver.LLMService") as service:
            service.return_value.call_llm_with_files.return_value = (
                "#### Output\n```sample\n42\n```"
            )
            result = MarkdownImprover(api_key="offline").improve_markdown("Original")
        self.assertTrue(result["success"])
        self.assertTrue(result["improved_markdown"].endswith("42\n```"))
        prompt = service.return_value.call_llm_with_files.call_args.args[2]
        self.assertIn(PROBLEM_STATEMENT_FORMAT_RULES, prompt)
        self.assertNotIn("mod = 10^9", prompt)
        self.assertNotIn("Use \\cdot for multiplication", prompt)

    def test_editorial_generation_keeps_final_nested_code_fence(self):
        text = '## Implementation\n\n??? note "C++"\n    ```cpp\n    int main() {}\n    ```'
        with patch("ai_features.solution_generator.LLMService") as service:
            service.return_value.call_llm_with_files.return_value = text
            result = SolutionGenerator(api_key="offline").generate_solution("Original")
        self.assertTrue(result["success"])
        self.assertEqual(result["solution_content"], text)
        self.assertIn(
            "int main()",
            BeautifulSoup(
                _render_markdown(result["solution_content"]), "html.parser"
            ).get_text(),
        )


class ReviewGuidanceTests(SimpleTestCase):
    def fixture(self):
        return SimpleNamespace(
            problem_id=1,
            _review_context={
                "problem_id": 1,
                "statement": "Text\n$$a=1$$\n$$b=2$$",
                "config": {},
                "data_config": {"checker": "standard"},
                "artifacts": {},
                "test_data_instructions": {"text": "Click Fill testcases and Apply!"},
            },
            coordinator_stages=Mock(filter=Mock(return_value=[])),
        )

    def test_supported_adjacent_math_and_plain_heading_render(self):
        html = _render_markdown("### Input\nText\n$$a=1$$\n$$b=2$$")
        self.assertIn("<h3", html)
        self.assertEqual(html.count('class="arithmatex"'), 2)
        self.assertIn(
            "adjacent display blocks do not require blank lines",
            MARKDOWN_FORMATTING_RULES,
        )

    def test_real_math_and_list_rendering_requirements_remain(self):
        self.assertIn('class="arithmatex"', _render_markdown("$x+y$"))
        self.assertNotIn('class="arithmatex"', _render_markdown("$ x+y $"))
        nested = _render_markdown("- Parent\n    - Child")
        flat = _render_markdown("- Parent\n  - Child")
        self.assertEqual(nested.count("<ul>"), 2)
        self.assertEqual(flat.count("<ul>"), 1)

    def test_reviewer_receives_acceptance_rules_not_generation_template(self):
        payload = build_stage_input(self.fixture(), "statement_review")
        self.assertEqual(payload["statement_format"], STATEMENT_REVIEW_RULES)
        self.assertNotEqual(payload["statement_format"], PROBLEM_STATEMENT_FORMAT_RULES)
        self.assertEqual(SECTION_GUIDANCE["statement_review"], STATEMENT_REVIEW_RULES)

    def test_technical_reviewers_do_not_receive_ui_howto(self):
        for phase in (
            "solutions_review",
            "validator_review",
            "checker_review",
            "test_review",
        ):
            with self.subTest(phase=phase):
                self.assertNotIn(
                    "test_data_instructions", build_stage_input(self.fixture(), phase)
                )
        self.assertIn(
            "Trailing whitespace is not the same as excess tokens",
            SECTION_GUIDANCE["validator_review"],
        )

    def test_model_call_receives_technical_guidance_and_rendering_rules(self):
        run = self.fixture()
        run.pk = 1
        run.triggered_by_id = None
        run.coordinator_stages.get.return_value = SimpleNamespace(
            pk=2, lease_token=None, status="R", attempts=1
        )
        result = {
            "assessment": "pass",
            "feedback": "Valid",
            "findings": [],
            "evidence": {},
        }
        config = Mock(api_key="offline")
        config.get_bot_name_for_review.return_value = "offline"
        with patch("judge.auto_review.phases.get_config", return_value=config), patch(
            "judge.auto_review.phases.LLMService"
        ) as service:
            service.return_value.call_llm.return_value = json.dumps(result)
            _call(run, "validator_review", {"kind": "problem"})
        prompt = json.loads(service.return_value.call_llm.call_args.args[0])
        self.assertEqual(prompt["review_rules"], TECHNICAL_REVIEW_RULES)
        self.assertEqual(
            prompt["section_guidance"], SECTION_GUIDANCE["validator_review"]
        )
        self.assertEqual(prompt["markdown_rules"], MARKDOWN_FORMATTING_RULES)

    def test_shared_rule_numbering_for_existing_consumers(self):
        rules = get_markdown_rules_for_prompt(9)
        for number in range(9, 13):
            self.assertIn(f"\n{number}.", rules)
        self.assertNotIn("\n1.", rules)

    def test_checker_template_uses_site_testlib_fraction_protocol(self):
        template = _get_checker_template(None)
        self.assertIn("quitp(0.5", template)
        self.assertNotIn("quitf(_pc", template)
