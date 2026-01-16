import markdown as _markdown
import bleach
from django.utils.html import escape
from bs4 import BeautifulSoup
from pymdownx import superfences, arithmatex
from django.conf import settings
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
    "markdown.extensions.toc",
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
            },
            {
                "name": "math",
                "class": "arithmatex",
                "format": arithmatex.arithmatex_fenced_format(which="generic"),
            },
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
    "video",
    "source",
]

ALLOWED_ATTRS = [
    "src",
    "width",
    "height",
    "href",
    "class",
    "id",
    "open",
    "title",
    "frameborder",
    "allow",
    "allowfullscreen",
    "loading",
    "controls",
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
            link = soup.new_tag(
                "a",
                href=img["src"],
                **{
                    "data-featherlight": "image",
                    "data-featherlight-variant": "image-widget-lightbox",
                }
            )
            img.wrap(link)
    return soup


def _open_external_links_in_new_tab(soup):
    domain = settings.SITE_DOMAIN.lower()
    for a in soup.findAll("a", href=True):
        href = a["href"]
        if href.startswith("http://") or href.startswith("https://"):
            try:
                link_domain = urlparse(href).netloc.lower()
                if link_domain != domain:
                    a["target"] = "_blank"
            except Exception:
                continue
    return soup


def _sanitize_iframe_autoplay(soup):
    """Remove autoplay parameters from iframe src URLs and attributes to prevent autoplay"""
    for iframe in soup.findAll("iframe"):
        try:
            # 1. Sanitize src URL parameters
            src = iframe.get("src")
            if src:
                # Parse the URL
                parsed = urlparse(src)

                # Get query parameters
                query_params = parse_qs(parsed.query)

                # Remove autoplay parameters (set to 0 if present)
                autoplay_params = ["autoplay", "auto_play", "auto-play"]
                modified = False

                for param in autoplay_params:
                    if param in query_params:
                        # Set autoplay to 0 instead of removing to be explicit
                        query_params[param] = ["0"]
                        modified = True

                # If we modified parameters, rebuild the URL
                if modified:
                    new_query = urlencode(query_params, doseq=True)
                    new_parsed = parsed._replace(query=new_query)
                    iframe["src"] = urlunparse(new_parsed)

            # 2. Remove/sanitize allow attribute that might permit autoplay
            allow_attr = iframe.get("allow")
            if allow_attr:
                # Remove autoplay from allow attribute
                allow_values = [val.strip() for val in allow_attr.split(";")]
                allow_values = [
                    val for val in allow_values if not val.startswith("autoplay")
                ]

                if allow_values:
                    iframe["allow"] = "; ".join(allow_values)
                else:
                    # Remove empty allow attribute
                    del iframe["allow"]

        except Exception:
            # If URL parsing fails, continue with next iframe
            continue

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
    soup = _sanitize_iframe_autoplay(soup)
    html = str(soup)

    return '<div class="md-typeset content-description">%s</div>' % html
