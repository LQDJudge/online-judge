import json
import re

from django.test import SimpleTestCase
from django.utils.html import json_script

LS = "\u2028"  # line separator
PS = "\u2029"  # paragraph separator


def _json_script_content(data):
    payload = json_script(data, "payload")
    match = re.search(
        r'<script id="payload" type="application/json">(.*)</script>', payload
    )
    if not match:
        raise AssertionError("json_script did not render the expected script tag")
    return payload, match.group(1)


class JsonScriptTest(SimpleTestCase):
    def test_script_breakout_neutralized(self):
        payload, content = _json_script_content(
            [{"label": "</script><script>alert(1)</script>"}]
        )
        # The raw closing tag must not survive — it would break out of <script>.
        self.assertNotIn("</script>", content)
        self.assertNotIn("<script>", content)
        self.assertIn("\\u003C", payload)

    def test_ampersand_and_angles_escaped(self):
        _, content = _json_script_content({"x": "<b>&</b>"})
        for raw in ("<", ">", "&"):
            self.assertNotIn(raw, content)

    def test_line_separators_escaped(self):
        # U+2028 / U+2029 are valid JSON but break JS string literals.
        _, content = _json_script_content({"x": "a" + LS + "b" + PS + "c"})
        self.assertNotIn(LS, content)
        self.assertNotIn(PS, content)
        self.assertIn("\\u2028", content)
        self.assertIn("\\u2029", content)

    def test_round_trips_to_original(self):
        original = {"label": "</script>", "n": 5, "s": "a&b<c>"}
        _, content = _json_script_content(original)
        self.assertEqual(json.loads(content), original)

    def test_plain_data_unaffected(self):
        _, content = _json_script_content([1, 2, 3])
        self.assertEqual(json.loads(content), [1, 2, 3])
