from decimal import Decimal

from django import forms

from plans.services.pdfinfo import inspect_pdf


class SendPlansForm(forms.Form):
    from_email = forms.EmailField(label="From", help_text="The email address this is sent from")
    to_email = forms.EmailField(label="To", help_text="Your client's email address")
    client_name = forms.CharField(label="Client name", required=False, max_length=200)
    title = forms.CharField(label="Project name", required=False, max_length=200)
    price = forms.DecimalField(label="Price (USD)", min_value=Decimal("0.50"), max_digits=10, decimal_places=2)
    pdf = forms.FileField(label="Plan PDF")

    def clean_pdf(self):
        f = self.cleaned_data["pdf"]
        try:
            inspect_pdf(f)
        except ValueError as exc:
            raise forms.ValidationError(str(exc))
        return f
