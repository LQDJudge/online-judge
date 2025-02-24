from django.forms.widgets import ClearableFileInput
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.conf import settings
from django import forms

from urllib.parse import urljoin


class FileEditWidget(ClearableFileInput):
    extra_template_name = "widgets/file_edit.html"

    @property
    def media(self):
        js = [
            urljoin(settings.ACE_URL, "ace.js"),
            "django_ace/widget.js",
            "file_edit/widget.js",
        ]

        css = {
            "screen": ["django_ace/widget.css"],
        }
        return forms.Media(js=js, css=css)

    def __init__(self, *args, default_file_name="new_file.txt", **kwargs):
        super().__init__(*args, **kwargs)
        self.default_file_name = default_file_name

    def render(self, name, value, attrs=None, renderer=None):
        # Generate a unique element ID based on the field name
        element_id = attrs["id"] if attrs and "id" in attrs else f"id_{name}"

        # Initialize file content and file name
        file_content = ""
        file_name = (
            self.default_file_name
        )  # Use default file name if no file is uploaded
        if value and hasattr(value, "url"):
            try:
                # Read the file content
                with value.open("r") as f:
                    file_content = f.read()
                # Get the file name
                file_name = value.name.split("/")[
                    -1
                ]  # Extract the file name from the path
            except Exception as e:
                file_content = f"Error loading file: {str(e)}"

        # Render the widget template with the context
        context = {
            "file_input_html": super().render(name, value, attrs, renderer),
            "element_id": element_id,  # Unique ID for each widget
            "file_content": file_content,
            "file_name": file_name,
            "default_file_name": self.default_file_name,
        }
        return mark_safe(render_to_string(self.extra_template_name, context))
