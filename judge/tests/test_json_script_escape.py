import json

from django.test import SimpleTestCase

from judge.views.user import _json_for_script

LS = "\u2028"  # line separator
PS = "\u2029"  # paragraph separator


class JsonForScriptTest(SimpleTestCase):
    def test_script_breakout_neutralized(self):
        payload = _json_for_script([{"label": "</script><script>alert(1)</script>"}])
        # The raw closing tag must not survive — it would break out of <script>.
        self.assertNotIn("</script>", payload)
        self.assertNotIn("<script>", payload)
        self.assertIn("\\u003c", payload)

    def test_ampersand_and_angles_escaped(self):
        payload = _json_for_script({"x": "<b>&</b>"})
        for raw in ("<", ">", "&"):
            self.assertNotIn(raw, payload)

    def test_line_separators_escaped(self):
        # U+2028 / U+2029 are valid JSON but break JS string literals.
        payload = _json_for_script({"x": "a" + LS + "b" + PS + "c"})
        self.assertNotIn(LS, payload)
        self.assertNotIn(PS, payload)
        self.assertIn("\\u2028", payload)
        self.assertIn("\\u2029", payload)

    def test_round_trips_to_original(self):
        original = {"label": "</script>", "n": 5, "s": "a&b<c>"}
        self.assertEqual(json.loads(_json_for_script(original)), original)

    def test_plain_data_unaffected(self):
        self.assertEqual(json.loads(_json_for_script([1, 2, 3])), [1, 2, 3])
