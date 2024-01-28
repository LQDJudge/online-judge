from django.utils.html import escape, mark_safe
from judge.markdown import markdown

__all__ = ["highlight_code"]


def highlight_code(code, language, linenos=True, title=None):
    linenos_option = 'linenums="1"' if linenos else ""
    title_option = f'title="{title}"' if title else ""
    options = f"{{.{language} {linenos_option} {title_option}}}"

    value = f"```{options}\n{code}\n```\n"
    return mark_safe(markdown(value))
