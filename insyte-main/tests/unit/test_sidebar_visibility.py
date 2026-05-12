"""Frontend RBAC gate tests.

Locks the four sidebar / button gates that previously hid UI from
group-based admins (PR #26):

- Audit History link in ``admin_layout.html``
- User Management link in ``admin_layout.html``
- Invoices link in ``admin_layout.html``
- Re-submit-to-QA button in ``admin/donation_batch_detail.html``

Each gate is exercised in two ways:

1. Structural: assert the literal gate string lives in the template file,
   so accidental edits that drop or weaken the gate fail loudly.
2. Behavioral: render the gate expression as a standalone Django template
   against six user shapes (staff, bare codename, app-prefixed codename,
   irrelevant perm, ``"all"`` sentinel, anonymous) and assert visibility
   matches the backend decorator on the corresponding view.
"""

from pathlib import Path

import pytest
from django.template import Context, Template

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_LAYOUT = REPO_ROOT / "templates" / "layouts" / "admin_layout.html"
DONATION_BATCH_DETAIL = REPO_ROOT / "templates" / "admin" / "donation_batch_detail.html"


GATES = {
    "audit_history": (
        "{% if is_admin_user or 'all' in user_permissions "
        "or 'view_auditlog' in user_permissions "
        "or 'audit.view_auditlog' in user_permissions %}"
    ),
    "user_management": (
        "{% if is_admin_user or 'all' in user_permissions "
        "or 'view_user' in user_permissions "
        "or 'core.view_user' in user_permissions %}"
    ),
    "invoices": (
        "{% if is_admin_user or 'all' in user_permissions "
        "or 'view_invoice' in user_permissions "
        "or 'invoices.view_invoice' in user_permissions %}"
    ),
    "donation_batch_resubmit": (
        "{% if is_admin_user or 'all' in user_permissions "
        "or 'change_donationbatch' in user_permissions "
        "or 'donations.change_donationbatch' in user_permissions "
        "or 'core.change_donationbatch' in user_permissions %}"
    ),
}


def _render(gate: str, *, is_admin_user: bool, user_permissions: list[str]) -> str:
    template = Template(gate + "VISIBLE{% endif %}")
    return template.render(
        Context({"is_admin_user": is_admin_user, "user_permissions": user_permissions})
    ).strip()


def _is_visible(gate: str, *, is_admin_user: bool, user_permissions: list[str]) -> bool:
    return (
        _render(gate, is_admin_user=is_admin_user, user_permissions=user_permissions)
        == "VISIBLE"
    )


class TestSidebarGateStructure:
    """Lock the four gate strings in the actual template files.

    A structural change that drops a perm from one of these gates would
    silently re-introduce the bug PR #26 fixed; this catches it.
    """

    def test_audit_gate_in_admin_layout(self) -> None:
        assert GATES["audit_history"] in ADMIN_LAYOUT.read_text()

    def test_user_management_gate_in_admin_layout(self) -> None:
        assert GATES["user_management"] in ADMIN_LAYOUT.read_text()

    def test_invoices_gate_in_admin_layout(self) -> None:
        assert GATES["invoices"] in ADMIN_LAYOUT.read_text()

    def test_resubmit_gate_in_donation_batch_detail(self) -> None:
        assert GATES["donation_batch_resubmit"] in DONATION_BATCH_DETAIL.read_text()


@pytest.mark.parametrize(
    "gate_name,bare_codename,app_prefixed",
    [
        ("audit_history", "view_auditlog", "audit.view_auditlog"),
        ("user_management", "view_user", "core.view_user"),
        ("invoices", "view_invoice", "invoices.view_invoice"),
        (
            "donation_batch_resubmit",
            "change_donationbatch",
            "donations.change_donationbatch",
        ),
    ],
)
class TestSidebarGateBehavior:
    """Each gate must mirror the backend decorator: visible for staff, for
    holders of the matching perm in either bare or app-prefixed form, and
    for the ``"all"`` sentinel; hidden for everyone else.
    """

    def test_staff_sees_link(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert _is_visible(GATES[gate_name], is_admin_user=True, user_permissions=[])

    def test_all_sentinel_sees_link(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert _is_visible(
            GATES[gate_name], is_admin_user=False, user_permissions=["all"]
        )

    def test_bare_codename_sees_link(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert _is_visible(
            GATES[gate_name], is_admin_user=False, user_permissions=[bare_codename]
        )

    def test_app_prefixed_perm_sees_link(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert _is_visible(
            GATES[gate_name], is_admin_user=False, user_permissions=[app_prefixed]
        )

    def test_irrelevant_perm_hidden(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert not _is_visible(
            GATES[gate_name],
            is_admin_user=False,
            user_permissions=["something_unrelated", "app.something_unrelated"],
        )

    def test_anonymous_hidden(
        self, gate_name: str, bare_codename: str, app_prefixed: str
    ) -> None:
        assert not _is_visible(
            GATES[gate_name], is_admin_user=False, user_permissions=[]
        )


class TestDonationBatchResubmitLegacyAppLabel:
    """The DonationBatch model moved from ``core`` to ``donations`` but the
    Permission row's app_label can lag behind. Both prefixes must work.
    """

    def test_legacy_core_prefix_still_grants_access(self) -> None:
        assert _is_visible(
            GATES["donation_batch_resubmit"],
            is_admin_user=False,
            user_permissions=["core.change_donationbatch"],
        )
