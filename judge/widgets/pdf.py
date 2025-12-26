from django import forms


class PDFWidget(forms.ClearableFileInput):
    template_name = "widgets/pdf.html"
