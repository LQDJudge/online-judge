from .. import registry
import markdown as _markdown
import bleach
from django.utils.html import escape
from bs4 import BeautifulSoup
from pymdownx import superfences


EXTENSIONS = [
    "pymdownx.magiclink",
    "pymdownx.betterem",
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
    "nl2br",
    "mdx_breakless_lists",
]

EXTENSION_CONFIGS = {
    "pymdownx.superfences": {
        "custom_fences": [
            {
                "name": "sample",
                "class": "no-border",
                "format": superfences.fence_code_format,
            }
        ]
    },
}

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS + [
    "img",
    "center",
    "iframe",
    "div",
    "span",
    "table",
    "tr",
    "td",
    "th",
    "tr",
    "pre",
    "code",
    "p",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "thead",
    "tbody",
    "sup",
    "dl",
    "dt",
    "dd",
    "br",
    "details",
    "summary",
]

ALLOWED_ATTRS = ["src", "width", "height", "href", "class", "open"]


@registry.filter
def markdown(value, lazy_load=False):
    extensions = EXTENSIONS
    html = _markdown.markdown(
        value, extensions=extensions, extension_configs=EXTENSION_CONFIGS
    )
    html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)
    if not html:
        html = escape(value)
    if lazy_load:
        soup = BeautifulSoup(html, features="lxml")
        for img in soup.findAll("img"):
            if img.get("src"):
                img["data-src"] = img["src"]
                img["src"] = ""
        html = str(soup)
    return '<div class="md-typeset">%s</div>' % html
