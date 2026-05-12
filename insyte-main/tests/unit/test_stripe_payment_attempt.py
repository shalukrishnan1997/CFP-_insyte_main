"""Tests for ``StripePaymentAttempt`` durable attempt logging.

Verifies that every Stripe attempt (success or failure) is recorded in
``core_stripepaymentattempt`` and that successes additionally produce a
``StripePayment`` row.

Note on rollback durability: in production (PostgreSQL), ``log_attempt`` opens
a fresh autocommit connection so the row survives an outer
``transaction.set_rollback(True)``. SQLite cannot exercise this contract under
test because shared-cache writers are serialized at the database level and a
second connection cannot write while the first holds an open write
transaction. The tests below therefore verify the simpler invariant that the
attempt row is created on every code path; the rollback-survival behavior is
a property of the connection strategy used by ``log_attempt``.
"""

from unittest.mock import MagicMock, patch

import pytest
import stripe

from tests.factories import (
    DonationFactory,
    StripeCustomerFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestStripePaymentAttemptLogging:
    """Every Stripe attempt must produce a ``StripePaymentAttempt`` row."""

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_stripe_error_logs_failed_attempt(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment, StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        mock_customer.return_value = StripeCustomerFactory()
        mock_pi_create.side_effect = stripe.CardError(
            message="Your card was declined.",
            param="number",
            code="card_declined",
        )

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is False
        attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.status == StripePaymentAttempt.STATUS_FAILED
        assert attempt.attempt_number == 1
        assert attempt.amount_cents == int(donation.amount * 100)
        assert attempt.currency == donation.currency
        assert "declined" in attempt.error_message.lower()
        assert attempt.error_code == "card_declined"
        assert not StripePayment.objects.filter(donation=donation).exists()

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_succeeded_creates_attempt_and_payment(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment, StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        mock_customer.return_value = StripeCustomerFactory()
        mock_pi = MagicMock()
        mock_pi.id = "pi_attempt_succeeded_1"
        mock_pi.status = "succeeded"
        mock_pi.latest_charge = "ch_attempt_1"
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is True
        attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
        assert len(attempts) == 1
        assert attempts[0].status == StripePaymentAttempt.STATUS_SUCCEEDED
        assert attempts[0].stripe_payment_intent_id == "pi_attempt_succeeded_1"

        payments = list(StripePayment.objects.filter(donation=donation))
        assert len(payments) == 1
        assert payments[0].status == StripePayment.STATUS_SUCCEEDED

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_attempt_numbers_increment_per_donation(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        mock_customer.return_value = StripeCustomerFactory()
        mock_pi_create.side_effect = stripe.CardError(
            message="Insufficient funds.",
            param="number",
            code="insufficient_funds",
        )

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        for _ in range(2):
            BatchPaymentService._process_single_donation(donation, user)

        attempts = list(
            StripePaymentAttempt.objects.filter(donation=donation).order_by(
                "attempt_number"
            )
        )
        assert [a.attempt_number for a in attempts] == [1, 2]
        assert all(a.status == StripePaymentAttempt.STATUS_FAILED for a in attempts)

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_requires_payment_method_logs_failed_attempt(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment, StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        mock_customer.return_value = StripeCustomerFactory()
        mock_pi = MagicMock()
        mock_pi.id = "pi_requires_pm_1"
        mock_pi.status = "requires_payment_method"
        mock_pi.last_payment_error = MagicMock(
            message="Card declined", code="generic_decline"
        )
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is False
        attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
        assert len(attempts) == 1
        assert attempts[0].status == StripePaymentAttempt.STATUS_FAILED
        assert attempts[0].stripe_payment_intent_id == "pi_requires_pm_1"
        assert attempts[0].error_code == "generic_decline"
        assert not StripePayment.objects.filter(donation=donation).exists()

    @patch("stripe.PaymentIntent.create")
    @patch("payments.services.StripePaymentService.get_or_create_customer")
    @patch("payments.services.StripePaymentService._get_api_key")
    def test_requires_action_logs_attempt(
        self,
        mock_api_key: MagicMock,
        mock_customer: MagicMock,
        mock_pi_create: MagicMock,
    ) -> None:
        """3DS / SCA path: attempt is logged AND a pending StripePayment is created.

        Unlike a hard ``requires_payment_method`` decline, ``requires_action``
        is a recoverable state — the donor just needs to complete the SCA
        challenge via the emailed Checkout link. The attempt row records the
        challenge; the StripePayment row holds the next-action URL used by
        the QA Checkout fallback flow. The PaymentIntent ``client_secret`` is
        deliberately *not* persisted (bearer token, would leak via audit /
        Sentry).
        """
        from payments.batch_payment import BatchPaymentService
        from payments.models import StripePayment, StripePaymentAttempt

        mock_api_key.return_value = "sk_test_key"
        mock_customer.return_value = StripeCustomerFactory()
        mock_pi = MagicMock()
        mock_pi.id = "pi_requires_action_1"
        mock_pi.status = "requires_action"
        mock_pi.next_action.use_stripe_sdk.stripe_js = (
            "https://js.stripe.com/v3/3ds/redirect"
        )
        mock_pi.to_dict.return_value = {}
        mock_pi_create.return_value = mock_pi

        donation = DonationFactory(
            payment_method="card",
            qa_status="approved",
            payment_status="pending",
        )
        user = UserFactory(is_staff=True)

        result = BatchPaymentService._process_single_donation(donation, user)

        assert result["success"] is True
        assert result["awaiting_authentication"] is True
        attempts = list(StripePaymentAttempt.objects.filter(donation=donation))
        assert len(attempts) == 1
        assert attempts[0].status == StripePaymentAttempt.STATUS_REQUIRES_ACTION
        # StripePayment row holds the recovery handle for the QA Checkout
        # fallback; donation is now in ``awaiting_authentication``. The
        # ``client_secret`` is intentionally *not* persisted.
        payment = StripePayment.objects.get(donation=donation)
        assert payment.requires_action_url.endswith("3ds/redirect")
        donation.refresh_from_db()
        assert donation.payment_status == "awaiting_authentication"


@pytest.mark.django_db()
class TestStripePaymentAttemptManager:
    """Direct tests for the ``StripePaymentAttempt`` manager methods."""

    def test_log_attempt_persists_fields(self) -> None:
        from payments.models import StripePaymentAttempt

        donation = DonationFactory()

        attempt = StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=2500,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_FAILED,
            attempt_number=1,
            stripe_payment_intent_id="pi_unit_1",
            error_code="card_declined",
            error_message="Card declined",
        )

        assert attempt is not None
        persisted = StripePaymentAttempt.objects.get(donation=donation)
        assert persisted.status == StripePaymentAttempt.STATUS_FAILED
        assert persisted.amount_cents == 2500
        assert persisted.attempt_number == 1
        assert persisted.stripe_payment_intent_id == "pi_unit_1"
        assert persisted.error_code == "card_declined"
        assert persisted.error_message == "Card declined"

    def test_log_attempt_uses_fresh_connection_on_postgres(self) -> None:
        """On non-SQLite vendors the row is written through a separate connection.

        This proves the rollback-survival strategy without requiring a real
        Postgres database in the test environment. Production verification
        (Postgres only): wrap ``log_attempt`` in ``transaction.atomic()``,
        force a rollback after the call, and assert the row persists when
        read through a fresh connection. Not feasible under SQLite test
        settings — verify manually on staging.

        Also covers the ``IntegrityError`` retry branch: a single collision
        on the first INSERT should be transparently retried with a fresh
        ``attempt_number`` and succeed.
        """
        from unittest.mock import MagicMock, patch

        from django.db import IntegrityError

        from payments.models import StripePaymentAttempt

        donation = DonationFactory()
        fake_default = MagicMock()
        fake_default.vendor = "postgresql"

        # First call raises IntegrityError, second succeeds. Two cursor()
        # context managers feed two execute calls.
        first_cursor = MagicMock()
        first_cursor.execute.side_effect = IntegrityError("duplicate key")
        second_cursor = MagicMock()

        fake_fresh_connection = MagicMock()
        cursor_mgr_1 = MagicMock()
        cursor_mgr_1.__enter__ = MagicMock(return_value=first_cursor)
        cursor_mgr_1.__exit__ = MagicMock(return_value=False)
        cursor_mgr_2 = MagicMock()
        cursor_mgr_2.__enter__ = MagicMock(return_value=second_cursor)
        cursor_mgr_2.__exit__ = MagicMock(return_value=False)
        fake_fresh_connection.cursor.side_effect = [cursor_mgr_1, cursor_mgr_2]

        with (
            patch("payments.models.default_connection", fake_default),
            patch("payments.models.connections") as mock_connections,
            patch.object(
                StripePaymentAttempt.objects,
                "next_attempt_number",
                return_value=2,
            ) as mock_next,
        ):
            mock_connections.create_connection.return_value = fake_fresh_connection
            attempt = StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=1000,
                currency="GBP",
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=1,
                error_message="simulated",
            )

        assert attempt is not None
        # Retried once: two create_connection calls (one per attempt) and
        # next_attempt_number queried once to recompute.
        assert mock_connections.create_connection.call_count == 2
        mock_next.assert_called_once_with(donation)
        # Final row uses the recomputed attempt_number.
        assert attempt.attempt_number == 2
        assert fake_fresh_connection.set_autocommit.call_count == 2
        assert fake_fresh_connection.cursor.call_count == 2
        assert fake_fresh_connection.close.call_count == 2

    def test_concurrent_attempt_numbers_dont_collide(self) -> None:
        """Two writers computing the same ``attempt_number`` must not collide.

        Simulates a race: the first INSERT raises ``IntegrityError`` from the
        ``UniqueConstraint(donation, attempt_number)``, the manager retries
        with the recomputed number, and the row persists.
        """
        from payments.models import StripePaymentAttempt

        donation = DonationFactory()
        # First writer succeeds with attempt_number=1.
        StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=1000,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_SUCCEEDED,
            attempt_number=1,
        )

        # Second writer tries again with the same attempt_number=1
        # (simulating a stale read of next_attempt_number). The retry path
        # should recompute to 2 and succeed.
        attempt = StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=1000,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_FAILED,
            attempt_number=1,
        )

        assert attempt is not None
        assert attempt.attempt_number == 2
        rows = list(
            StripePaymentAttempt.objects.filter(donation=donation).order_by(
                "attempt_number"
            )
        )
        assert [r.attempt_number for r in rows] == [1, 2]

    def test_log_attempt_reraises_on_database_error(self) -> None:
        """Infrastructure failures must propagate, not be swallowed."""
        from unittest.mock import patch

        import pytest as _pytest
        from django.db import OperationalError

        from payments.models import StripePaymentAttempt

        donation = DonationFactory()

        with (
            patch.object(
                StripePaymentAttempt.objects,
                "create",
                side_effect=OperationalError("connection lost"),
            ),
            _pytest.raises(OperationalError),
        ):
            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=1000,
                currency="GBP",
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=1,
            )

    def test_log_attempt_swallows_logical_error(self) -> None:
        """Logical errors (e.g. ``ValueError``) are logged but not raised."""
        from unittest.mock import patch

        from payments.models import StripePaymentAttempt

        donation = DonationFactory()

        with patch.object(
            StripePaymentAttempt.objects,
            "create",
            side_effect=ValueError("bad value"),
        ):
            result = StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=1000,
                currency="GBP",
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=1,
            )

        assert result is None

    def test_log_attempt_raises_after_exhausting_retries(self) -> None:
        """Repeated collisions exhaust the retry cap and propagate the error."""
        from unittest.mock import patch

        import pytest as _pytest
        from django.db import IntegrityError

        from payments.models import StripePaymentAttempt

        donation = DonationFactory()

        with (
            patch.object(
                StripePaymentAttempt.objects,
                "create",
                side_effect=IntegrityError("duplicate"),
            ),
            _pytest.raises(IntegrityError),
        ):
            StripePaymentAttempt.objects.log_attempt(
                donation=donation,
                amount_cents=1000,
                currency="GBP",
                status=StripePaymentAttempt.STATUS_FAILED,
                attempt_number=1,
            )

    def test_next_attempt_number_increments(self) -> None:
        from payments.models import StripePaymentAttempt

        donation = DonationFactory()

        assert StripePaymentAttempt.objects.next_attempt_number(donation) == 1

        StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=1000,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_FAILED,
            attempt_number=1,
        )
        assert StripePaymentAttempt.objects.next_attempt_number(donation) == 2

        StripePaymentAttempt.objects.log_attempt(
            donation=donation,
            amount_cents=1000,
            currency="GBP",
            status=StripePaymentAttempt.STATUS_SUCCEEDED,
            attempt_number=2,
        )
        assert StripePaymentAttempt.objects.next_attempt_number(donation) == 3
