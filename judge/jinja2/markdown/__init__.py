from .. import registry
import markdown as _markdown
import bleach
from django.utils.html import escape


EXTENSIONS = [
    "pymdownx.magiclink",
    "pymdownx.details",
    "pymdownx.emoji",
    "pymdownx.inlinehilite",
    "pymdownx.superfences",
    "pymdownx.tasklist",
    "markdown.extensions.footnotes",
    "markdown.extensions.attr_list",
    "markdown.extensions.def_list",
    "markdown.extensions.tables",
    "markdown.extensions.admonition",
    "pymdownx.arithmatex",
]

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS + ["img", "center", "iframe"]

ALLOWED_ATTRS = ["src", "width", "height", "href"]


@registry.filter
def markdown(value, hard_wrap=False):
    extensions = EXTENSIONS
    if hard_wrap:
        extensions = EXTENSIONS + ["nl2br"]
    html = bleach.clean(value, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)
    html = _markdown.markdown(html, extensions=extensions)
    if not html:
        html = escape(value)
    return '<div class="md-typeset">%s</div>' % html
