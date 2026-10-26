import json

from django.test import SimpleTestCase

from judge.review.json_output import MARKER, decode_review_json


class ReviewJsonOutputTests(SimpleTestCase):
    def test_valid_json_is_preserved_including_code_and_escaped_quotes(self):
        value = {
            "summary": r"set $\{1,2\}$",
            "source": 'print("hello")\n',
            "input": "1\n",
        }
        self.assertEqual(decode_review_json(json.dumps(value)), (value, []))

    def test_only_literal_braces_in_narrative_are_restored(self):
        raw = r'{"summary":"Tập $\{1,2\}$", "comparisons":[{"substantive_differences":["Tập $\{a,b\}$"]}]}'
        value, repairs = decode_review_json(raw)
        self.assertEqual(value["summary"], r"Tập $\{1,2\}$")
        self.assertEqual(
            value["comparisons"][0]["substantive_differences"], [r"Tập $\{a,b\}$"]
        )
        self.assertEqual(
            repairs, ["$.summary", "$.comparisons[0].substantive_differences[0]"]
        )

    def test_executable_fields_keys_and_reference_ids_are_never_repaired(self):
        for field in (
            "source",
            "code",
            "input",
            "expected_output",
            "candidate_output",
            "evidence_refs",
            "relationship",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_review_json('{"%s":"bad \\{ text"}' % field)
        with self.assertRaises(ValueError):
            decode_review_json(r'{"bad\{key":"x"}')
        with self.assertRaises(ValueError):
            decode_review_json('{"summary":"bad \\{ ' + MARKER + '"}')

    def test_other_malformed_json_and_nonfinite_numbers_still_fail(self):
        for text in (
            r'{"summary":"bad \latex"}',
            r'{"summary":"x\{y"',
            '{"summary":"bad \\{", "n":NaN}',
            '{"summary": "a" + "b"}',
            '{"summary":"bad \\{", "input":"bad \\}"}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_review_json(text)

    def test_duplicate_fields_are_rejected_instead_of_overwriting_findings(self):
        for text in (
            '{"status":"finding","status":"no_issue"}',
            '{"summary":"bad \\{","status":"finding","status":"no_issue"}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_review_json(text)
