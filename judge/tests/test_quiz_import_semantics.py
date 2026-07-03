from django.test import SimpleTestCase

from ai_features.quiz_import_service import (
    QUIZ_IMPORT_SYSTEM_PROMPT,
    normalize_quiz_question_payload,
)


class QuizImportPromptSemanticsTest(SimpleTestCase):
    """The import prompt must define the MEANING of correct_answers per type.

    These are prompt-invariant checks: they don't run the LLM, they assert the
    instruction text carries the anchors that prevent the composite-SA bug and
    its MA/MC siblings. They guard against future edits silently dropping them.
    """

    def test_prompt_defines_sa_or_semantics_and_composite_example(self):
        p = QUIZ_IMPORT_SYSTEM_PROMPT
        self.assertIn("logical OR", p)
        self.assertIn("ALTERNATIVE", p)
        # The concrete RIGHT/WRONG composite example must be present.
        self.assertIn('["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"]', p)
        self.assertIn("RIGHT:", p)
        self.assertIn("WRONG:", p)
        # Honor in-question answer-format instructions.
        self.assertIn("write in the format", p)

    def test_prompt_defines_ma_and_mc_meaning(self):
        p = QUIZ_IMPORT_SYSTEM_PROMPT
        # MA is AND / complete set.
        self.assertIn("COMPLETE set", p)
        self.assertIn("logical AND", p)
        # MC single id must exist among the listed choices.
        self.assertIn("one of the ids", p)

    def test_prompt_requires_sa_answer_format_and_example(self):
        p = QUIZ_IMPORT_SYSTEM_PROMPT
        # SA questions must embed a required format instruction + example.
        self.assertIn("REQUIRED ANSWER FORMAT", p)
        self.assertIn("ví dụ", p)  # the example marker in the sample instruction
        # The example must NOT be the real answer (anti-spoiler guidance).
        self.assertIn("spoil", p)
        self.assertIn("invented values", p)
        # Grading is normalized exact (whitespace/case ignored, order matters).
        self.assertIn("NORMALIZED EXACT", p)


class QuizImportTitleRulesTest(SimpleTestCase):
    """TITLE RULES must instruct non-spoiler, thematic titles."""

    def test_prompt_forbids_spoiler_titles(self):
        p = QUIZ_IMPORT_SYSTEM_PROMPT
        self.assertIn("MUST NOT", p)
        self.assertIn("solution", p)
        self.assertIn("approach", p)
        # Thematic/neutral guidance present.
        self.assertIn("THEMATIC", p)
        # The old spoiler-inducing instruction is gone.
        self.assertNotIn("brief, descriptive title", p)


class NormalizeCompositeSATest(SimpleTestCase):
    """Lock the write-path guarantee: normalize does NOT split a composite SA
    answer on commas — one entry stays one entry (defaults exact/insensitive)."""

    def test_normalize_keeps_single_composite_sa_answer(self):
        choices, correct = normalize_quiz_question_payload(
            "SA",
            None,
            {"answers": ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"]},
        )
        self.assertEqual(correct["answers"], ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"])
        self.assertEqual(correct["type"], "exact")
        self.assertFalse(correct["case_sensitive"])
        self.assertEqual(choices, [])
