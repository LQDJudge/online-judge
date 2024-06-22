from django.contrib.admin import widgets as admin_widgets
from django.forms.utils import flatatt
from django.template.loader import get_template
from django.utils.encoding import force_text
from django.utils.html import conditional_escape
from django.conf import settings

from judge.widgets.mixins import CompressorWidgetMixin

__all__ = [
    "PagedownWidget",
    "AdminPagedownWidget",
    "KatexPagedownWidget",
    "KatexAdminPagedownWidget",
    "HeavyPreviewPageDownWidget",
    "HeavyPreviewAdminPageDownWidget",
]

try:
    from pagedown.widgets import PagedownWidget as OldPagedownWidget
except ImportError:
    PagedownWidget = None
    AdminPagedownWidget = None
    KatexPagedownWidget = None
    KatexAdminPagedownWidget = None
    HeavyPreviewPageDownWidget = None
    HeavyPreviewAdminPageDownWidget = None
else:

    class PagedownWidget(CompressorWidgetMixin, OldPagedownWidget):
        # The goal here is to compress all the pagedown JS into one file.
        # We do not want any further compress down the chain, because
        # 1. we'll creating multiple large JS files to download.
        # 2. this is not a problem here because all the pagedown JS files will be used together.
        compress_js = True

        def __init__(self, *args, **kwargs):
            super(PagedownWidget, self).__init__(*args, **kwargs)

        class Media:
            extend = False
            js = [
                "pagedown/Markdown.Converter.js",
                "pagedown-extra/pagedown/Markdown.Converter.js",
                "pagedown/Markdown.Sanitizer.js",
                "pagedown/Markdown.Editor.js",
                "pagedown-extra/Markdown.Extra.js",
                "pagedown_init.js",
            ]

    class AdminPagedownWidget(PagedownWidget, admin_widgets.AdminTextareaWidget):
        class Media:
            css = {
                "all": [
                    "pagedown_widget.css",
                    "content-description.css",
                    "admin/css/pagedown.css",
                    "pagedown.css",
                    "https://fonts.googleapis.com/css2?family=Fira+Code&family=Noto+Sans&display=swap",
                ]
            }
            js = ["admin/js/pagedown.js"]

    class KatexPagedownWidget(PagedownWidget):
        class Media:
            css = {
                "all": ["https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css"]
            }
            js = [
                "katex_config.js",
                "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js",
                "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js",
                "pagedown_math.js",
            ]

    class KatexAdminPagedownWidget(AdminPagedownWidget, KatexPagedownWidget):
        pass

    class HeavyPreviewPageDownWidget(PagedownWidget):
        def __init__(self, *args, **kwargs):
            self.template = "pagedown.html"
            self.id = kwargs.pop("id", None)
            self.preview_url = kwargs.pop("preview")
            self.preview_timeout = kwargs.pop("preview_timeout", None)
            self.hide_preview_button = kwargs.pop("hide_preview_button", False)
            super(HeavyPreviewPageDownWidget, self).__init__(*args, **kwargs)

        def render(self, name, value, attrs=None, renderer=None):
            if value is None:
                value = ""
            final_attrs = self.build_attrs(attrs, {"name": name})
            if "class" not in final_attrs:
                final_attrs["class"] = ""
            final_attrs["class"] += " wmd-input"
            if self.id:
                final_attrs["id"] = self.id
            return get_template(self.template).render(
                self.get_template_context(final_attrs, value)
            )

        def get_template_context(self, attrs, value):
            return {
                "image_upload_enabled": getattr(
                    settings, "PAGEDOWN_IMAGE_UPLOAD_ENABLED", False
                ),
                "attrs": flatatt(attrs),
                "body": conditional_escape(force_text(value)),
                "postfix": attrs["id"],
                "show_preview": True,
                "preview_url": self.preview_url,
                "preview_timeout": self.preview_timeout,
                "extra_classes": "dmmd-no-button" if self.hide_preview_button else None,
            }

        class Media:
            js = ["dmmd-preview.js"]

    class HeavyPreviewAdminPageDownWidget(
        KatexPagedownWidget, AdminPagedownWidget, HeavyPreviewPageDownWidget
    ):
        class Media:
            css = {
                "all": [
                    "table.css",
                    "ranks.css",
                    "dmmd-preview.css",
                ]
            }
