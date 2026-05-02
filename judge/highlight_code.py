from functools import lru_cache

from django.utils.html import escape, mark_safe
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

__all__ = ["highlight_code"]


@lru_cache(maxsize=128)
def _get_lexer(language):
    if not language:
        return TextLexer()
    try:
        return get_lexer_by_name(language)
    except ClassNotFound:
        return TextLexer()


@lru_cache(maxsize=4)
def _get_formatter(linenos):
    return HtmlFormatter(
        linenos="inline" if linenos else False,
        cssclass="highlight",
        wrapcode=True,
        nobackground=True,
    )


_HIGHLIGHT_OPEN = '<div class="highlight">'
_WRAPPER_OPEN = '<div class="md-typeset content-description">'
_WRAPPER_CLOSE = "</div>"


def highlight_code(code, language, linenos=True, title=None):
    body = highlight(code, _get_lexer(language), _get_formatter(bool(linenos)))
    if title and body.startswith(_HIGHLIGHT_OPEN):
        body = (
            _HIGHLIGHT_OPEN
            + '<span class="filename">'
            + escape(str(title))
            + "</span>"
            + body[len(_HIGHLIGHT_OPEN) :]
        )
    return mark_safe(_WRAPPER_OPEN + body + _WRAPPER_CLOSE)
