import markdown as _markdown
import bleach
from django.utils.html import escape
from bs4 import BeautifulSoup
from pymdownx import superfences
from django.conf import settings
from urllib.parse import urlparse

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


def _wrap_img_iframe_with_lazy_load(soup):
    for img in soup.findAll("img"):
        if img.get("src"):
            img["loading"] = "lazy"
    for img in soup.findAll("iframe"):
        if img.get("src"):
            img["loading"] = "lazy"
    return soup


def _wrap_images_with_featherlight(soup):
    for img in soup.findAll("img"):
        if img.get("src"):
            link = soup.new_tag("a", href=img["src"], **{"data-featherlight": "image"})
            img.wrap(link)
    return soup


def _open_external_links_in_new_tab(soup):
    domain = settings.SITE_DOMAIN.lower()
    for a in soup.findAll("a", href=True):
        href = a["href"]
        if href.startswith("http://") or href.startswith("https://"):
            link_domain = urlparse(href).netloc.lower()
            if link_domain != domain:
                a["target"] = "_blank"
    return soup


def markdown(value, lazy_load=False):
    extensions = EXTENSIONS
    html = _markdown.markdown(
        value, extensions=extensions, extension_configs=EXTENSION_CONFIGS
    )

    html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)

    if not html:
        html = escape(value)

    soup = BeautifulSoup(html, features="html.parser")
    if lazy_load:
        soup = _wrap_img_iframe_with_lazy_load(soup)

    soup = _wrap_images_with_featherlight(soup)
    soup = _open_external_links_in_new_tab(soup)
    html = str(soup)

    return '<div class="md-typeset content-description">%s</div>' % html
