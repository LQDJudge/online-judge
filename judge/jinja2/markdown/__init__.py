from bs4 import BeautifulSoup

from judge.markdown import markdown as _markdown
from judge.review.presentation import review_list_indentation

from .. import registry


@registry.filter
def markdown(value, lazy_load=False):
    return _markdown(value, lazy_load)


@registry.filter
def review_markdown(value):
    return _markdown(review_list_indentation(value))


@registry.filter
def review_inline_markdown(value):
    """Use the usual sanitizer and math support without block tags in titles."""
    soup = BeautifulSoup(str(_markdown(value)), "html.parser")
    inline_tags = {"a", "br", "code", "del", "em", "s", "span", "strong", "sub", "sup"}
    for tag in list(soup.find_all(True)):
        if "arithmatex" in tag.get("class", []):
            tag.name = "span"
        elif tag.name not in inline_tags:
            tag.unwrap()
    return str(soup).strip()
