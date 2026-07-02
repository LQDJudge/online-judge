from django.test import SimpleTestCase

from judge.utils.quiz_grading import normalize_sa, sa_exact_match


class NormalizeSATest(SimpleTestCase):
    """normalize_sa touches only whitespace and case — never meaning."""

    def test_meaning_bearing_chars_stay_distinct(self):
        # The four must all normalize to different strings (critical for math/CP).
        forms = ["1,2", "1.2", "12", "1 2"]
        normed = [normalize_sa(f) for f in forms]
        self.assertEqual(len(set(normed)), 4, normed)

    def test_space_between_digits_is_kept(self):
        # "1 2" (two numbers) must NOT collapse to "12".
        self.assertNotEqual(normalize_sa("1 2"), normalize_sa("12"))

    def test_case_folded_by_default(self):
        self.assertEqual(normalize_sa("Paris"), normalize_sa("paris"))

    def test_case_preserved_when_case_sensitive(self):
        self.assertNotEqual(
            normalize_sa("Paris", case_sensitive=True),
            normalize_sa("paris", case_sensitive=True),
        )

    def test_whitespace_runs_and_ends_collapsed(self):
        self.assertEqual(normalize_sa("  a   b  "), normalize_sa("a b"))

    def test_spaces_adjacent_to_punctuation_removed(self):
        self.assertEqual(normalize_sa("5, 8, 13, 15"), normalize_sa("5,8,13,15"))
        self.assertEqual(normalize_sa("Chloe: 5"), normalize_sa("Chloe:5"))
        self.assertEqual(normalize_sa("( 1 , 2 )"), normalize_sa("(1,2)"))
        self.assertEqual(normalize_sa("a + b"), normalize_sa("a+b"))

    def test_newline_becomes_space_not_removed_between_digits(self):
        self.assertEqual(normalize_sa("5\n8"), normalize_sa("5 8"))
        self.assertNotEqual(normalize_sa("5\n8"), normalize_sa("58"))

    def test_punctuation_never_stripped(self):
        # trailing dot matters (e.g. "5." vs "5"); we do NOT strip it.
        self.assertNotEqual(normalize_sa("5."), normalize_sa("5"))


class SAExactMatchTest(SimpleTestCase):
    """sa_exact_match = normalized exact compare, OR over the answers list."""

    def test_accepts_format_variants_of_correct_answer(self):
        ans = ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"]
        for typed in [
            "Chloe: 5, Leo: 8, Emma: 13, Lily: 15",
            "Chloe:5,Leo:8,Emma:13,Lily:15",
            "chloe: 5 , leo: 8 , emma: 13 , lily: 15",
        ]:
            self.assertTrue(sa_exact_match(typed, ans), typed)

    def test_rejects_wrong_order_and_values(self):
        self.assertFalse(sa_exact_match("2,5", ["5,2"]))
        self.assertFalse(sa_exact_match("12", ["1 2"]))
        self.assertFalse(
            sa_exact_match(
                "Chloe: 5, Leo: 8, Emma: 13, Lily: 14",
                ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"],
            )
        )

    def test_case_sensitivity_flag(self):
        self.assertTrue(sa_exact_match("paris", ["Paris"]))
        self.assertFalse(sa_exact_match("paris", ["Paris"], case_sensitive=True))

    def test_non_string_answer_entries_do_not_crash(self):
        # Legacy/edge data may store numbers or None in the answers list;
        # grading must not raise (it would fail the whole grading task).
        self.assertTrue(sa_exact_match("5", [5]))
        self.assertTrue(sa_exact_match("5", [5], case_sensitive=True))
        self.assertFalse(sa_exact_match("6", [5]))
        self.assertFalse(sa_exact_match("x", [None]))
        self.assertEqual(normalize_sa(5), "5")
        self.assertEqual(normalize_sa(None), "")
