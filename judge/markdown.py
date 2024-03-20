import markdown as _markdown
import bleach
from django.utils.html import escape
from bs4 import BeautifulSoup
from pymdownx import superfences

from judge.markdown_extensions import YouTubeExtension, EmoticonExtension


EXTENSIONS = [
    "pymdownx.arithmatex",
    "pymdownx.magiclink",
    "pymdownx.betterem",
    "pymdownx.details",
    "pymdownx.emoji",
    "pymdownx.inlinehilite",
    "pymdownx.superfences",
    "pymdownx.highlight",
    "pymdownx.tasklist",
    "markdown.extensions.footnotes",
    "markdown.extensions.attr_list",
    "markdown.extensions.def_list",
    "markdown.extensions.tables",
    "markdown.extensions.admonition",
    "nl2br",
    "mdx_breakless_lists",
    YouTubeExtension(),
    EmoticonExtension(),
]

EXTENSION_CONFIGS = {
    "pymdownx.arithmatex": {
        "generic": True,
    },
    "pymdownx.superfences": {
        "custom_fences": [
            {
                "name": "sample",
                "class": "no-border",
                "format": superfences.fence_code_format,
            }
        ],
    },
    "pymdownx.highlight": {
        "auto_title": True,
        "auto_title_map": {
            "Text Only": "",
        },
    },
}

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
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

ALLOWED_ATTRS = [
    "src",
    "width",
    "height",
    "href",
    "class",
    "open",
    "title",
    "frameborder",
    "allow",
    "allowfullscreen",
    "loading",
]


def markdown(value, lazy_load=False):
    extensions = EXTENSIONS
    html = _markdown.markdown(
        value, extensions=extensions, extension_configs=EXTENSION_CONFIGS
    )

    html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)

    if not html:
        html = escape(value)
    if lazy_load:
        soup = BeautifulSoup(html, features="html.parser")
        for img in soup.findAll("img"):
            if img.get("src"):
                img["loading"] = "lazy"
        for img in soup.findAll("iframe"):
            if img.get("src"):
                img["loading"] = "lazy"
        html = str(soup)
    return '<div class="md-typeset content-description">%s</div>' % html
