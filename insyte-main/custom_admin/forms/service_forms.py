"""Service category/item management forms.

Forms:
    ServiceCategoryCreateForm: Create new service categories.
    ServiceItemCreateForm: Create new service items within categories.
"""

from django import forms


class ServiceCategoryCreateForm(forms.Form):
    """Form for creating service categories."""

    name = forms.CharField(
        max_length=255,
        strip=True,
        help_text="Category name (e.g., 'Processing Fees').",
    )
    description = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )


class ServiceItemCreateForm(forms.Form):
    """Form for creating service items within a category."""

    category_id = forms.UUIDField(
        help_text="Parent category for this service item.",
    )
    description = forms.CharField(
        max_length=500,
        strip=True,
        help_text="Service item description.",
    )
    unit_price = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        initial=0,
        help_text="Price per unit (£).",
    )
    pricing_unit = forms.CharField(
        max_length=50,
        required=False,
        initial="each",
        strip=True,
        help_text="Unit of measure (e.g., 'each', 'per page', 'per batch').",
    )
    notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )
    is_default = forms.BooleanField(
        required=False,
        help_text="Auto-include in new invoices.",
    )
