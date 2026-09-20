from unittest.mock import patch

from django.test import SimpleTestCase

from judge.jinja2.reference import reference
from judge.utils.opengraph import generate_opengraph


class ReferenceFilterTest(SimpleTestCase):
    def test_plain_html_skips_reference_walk_but_returns_tree_wrapper(self):
        html = "<p>Ordinary chat message</p>"

        result = reference(html)

        self.assertEqual(result.tree.text_content(), "Ordinary chat message")

    @patch("judge.utils.opengraph.cache")
    def test_plain_markdown_remains_compatible_with_opengraph(self, cache):
        cache.get.return_value = None

        description, image = generate_opengraph(
            "reference-opengraph-test",
            "Ordinary contest description",
        )

        self.assertEqual(description, "Ordinary contest description")
        self.assertIsNone(image)
