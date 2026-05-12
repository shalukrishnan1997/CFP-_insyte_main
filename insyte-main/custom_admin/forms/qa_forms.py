"""QA review workflow forms.

Forms:
    QAActionForm: Approve/reject/flag individual donations.
    QABatchStatusForm: Update batch QA status.
    QABatchApproveForm: Approve an entire batch.
    QABatchRejectForm: Reject a batch (notes required).
    DonorContactStatusForm: Update a donor's contact status from QA.
"""

from typing import Any

from django import forms
from django.core.exceptions import ValidationError

from donations.models import Donation
from donors.models import Donor

QA_ACTION_CHOICES = [
    ("approve", "Approve"),
    ("reject", "Reject"),
    ("flag", "Flag for Review"),
    ("reset", "Reset to Pending"),
]


class QAActionForm(forms.Form):
    """Form for QA actions on individual donations."""

    action = forms.ChoiceField(
        choices=QA_ACTION_CHOICES,
        help_text="QA decision for this donation.",
    )
    qa_reject_reason = forms.ChoiceField(
        choices=[("", "— Select reason —"), *Donation.QA_REJECT_REASON_CHOICES],
        required=False,
        help_text="Required when rejecting. Drives the rejection letter wording.",
    )
    qa_notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
        help_text="Optional notes about this QA decision.",
    )

    def clean(self) -> dict[str, Any]:
        """Enforce reject-reason rules when the action is 'reject'."""
        cleaned = super().clean() or {}
        if cleaned.get("action") != "reject":
            return cleaned

        reason = cleaned.get("qa_reject_reason") or ""
        notes = (cleaned.get("qa_notes") or "").strip()
        if not reason:
            raise ValidationError(
                {"qa_reject_reason": "Select a reject reason before rejecting."}
            )
        if reason == Donation.QA_REJECT_REASON_OTHER and not notes:
            raise ValidationError(
                {"qa_notes": "Notes are required when the reject reason is 'Other'."}
            )
        return cleaned


class QABatchStatusForm(forms.Form):
    """Form for updating batch QA status."""

    BATCH_STATUS_CHOICES = [
        ("pending_qa", "Pending QA"),
        ("qa_in_progress", "QA In Progress"),
        ("qa_approved", "QA Approved"),
        ("qa_rejected", "QA Rejected"),
    ]

    status = forms.ChoiceField(
        choices=BATCH_STATUS_CHOICES,
        help_text="New batch status.",
    )
    review_notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
    )


class QABatchApproveForm(forms.Form):
    """Form for approving an entire batch."""

    batch_notes = forms.CharField(
        widget=forms.Textarea,
        required=False,
        help_text="Optional approval notes.",
    )


class QABatchRejectForm(forms.Form):
    """Form for rejecting a batch. Notes are mandatory."""

    batch_notes = forms.CharField(
        widget=forms.Textarea,
        help_text="Required: explain why the batch is being rejected.",
    )

    def clean_batch_notes(self) -> str:
        """Ensure rejection notes are provided."""
        notes = self.cleaned_data["batch_notes"].strip()
        if not notes:
            raise ValidationError("Notes are required when rejecting a batch.")
        return notes


class DonorContactStatusForm(forms.Form):
    """Update a donor's contact status from the QA review page.

    Deliberately independent of the donation-level approve/reject action:
    setting ``deceased`` or ``gone_away`` should not interfere with
    processing the current donation's payment.
    """

    contact_status = forms.ChoiceField(
        choices=Donor.CONTACT_STATUS_CHOICES,
        help_text="New contact status for this donor.",
    )
    contact_status_reason = forms.CharField(
        max_length=255,
        required=False,
        help_text="Optional reason/source for the status change.",
    )
