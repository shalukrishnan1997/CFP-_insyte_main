"""Django Form classes for the custom admin interface.

All forms are re-exported from this ``__init__`` so that existing code
can use ``from custom_admin.forms import CampaignCreateForm`` etc.

Forms provide:
    - Server-side validation with proper error messages
    - CSRF token binding
    - Type-safe field parsing (dates, decimals, booleans)
    - Password strength validation via Django validators
"""

from custom_admin.forms.auth_forms import ChangePasswordForm, UserProfileForm
from custom_admin.forms.banking_forms import (
    PayingInSlipCreateForm,
    PayingInSlipEditForm,
    SlipProcessingForm,
    SlipStatusForm,
)
from custom_admin.forms.campaign_forms import CampaignCreateForm, CampaignEditForm
from custom_admin.forms.client_forms import (
    ClientForm,
    PaymentGatewayConfigForm,
    PortalUserCreateForm,
    PortalUserResetPasswordForm,
)
from custom_admin.forms.invoice_forms import (
    InvoiceChangeStatusForm,
    InvoiceCreateForm,
    InvoiceMarkPaidForm,
)
from custom_admin.forms.letter_forms import LetterTemplateUploadForm
from custom_admin.forms.qa_forms import (
    QAActionForm,
    QABatchApproveForm,
    QABatchRejectForm,
    QABatchStatusForm,
)
from custom_admin.forms.service_forms import (
    ServiceCategoryCreateForm,
    ServiceItemCreateForm,
)
from custom_admin.forms.user_forms import GroupForm, UserCreateForm, UserEditForm

__all__ = [
    # campaign
    "CampaignCreateForm",
    "CampaignEditForm",
    # auth
    "ChangePasswordForm",
    # client
    "ClientForm",
    # user
    "GroupForm",
    # invoice
    "InvoiceChangeStatusForm",
    "InvoiceCreateForm",
    "InvoiceMarkPaidForm",
    # letter
    "LetterTemplateUploadForm",
    # banking
    "PayingInSlipCreateForm",
    "PayingInSlipEditForm",
    "PaymentGatewayConfigForm",
    "PortalUserCreateForm",
    "PortalUserResetPasswordForm",
    # qa
    "QAActionForm",
    "QABatchApproveForm",
    "QABatchRejectForm",
    "QABatchStatusForm",
    # service
    "ServiceCategoryCreateForm",
    "ServiceItemCreateForm",
    "SlipProcessingForm",
    "SlipStatusForm",
    "UserCreateForm",
    "UserEditForm",
    "UserProfileForm",
]
