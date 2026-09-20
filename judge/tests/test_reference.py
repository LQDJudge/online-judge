from unittest.mock import patch

from django.test import SimpleTestCase

from judge.jinja2.reference import reference


class ReferenceFilterTest(SimpleTestCase):
    def test_plain_html_skips_tree_parsing(self):
        html = "<p>Ordinary chat message</p>"

        with patch("judge.jinja2.reference.lxml_tree.fromstring") as fromstring:
            result = reference(html)

        self.assertEqual(result, html)
        fromstring.assert_not_called()
