"""Smoke tests to verify Django project configuration.

These tests validate that the project boots correctly and
core infrastructure (settings, URL routing, models) works.
"""

from django.test import SimpleTestCase, TestCase


class TestDjangoSetup(SimpleTestCase):
    """Verify that Django itself is configured correctly."""

    def test_settings_module_loads(self) -> None:
        """Settings module should load without errors."""
        from django.conf import settings

        self.assertTrue(settings.configured)

    def test_root_urlconf_resolves(self) -> None:
        """Root URL configuration should be importable."""
        from importlib import import_module

        from django.conf import settings

        urlconf = import_module(settings.ROOT_URLCONF)
        self.assertTrue(hasattr(urlconf, "urlpatterns"))


class TestModelImports(TestCase):
    """Ensure all models can be imported and are registered."""

    def test_core_models_importable(self) -> None:
        """All core models should be importable from core.models."""
        from audit.models import AuditLog
        from campaigns.models import Campaign
        from clients.models import Client
        from core.models import User
        from donations.models import Donation, DonationBatch
        from donors.models import Donor
        from invoices.models import Invoice

        self.assertIsNotNone(User)
        self.assertIsNotNone(Client)
        self.assertIsNotNone(Campaign)
        self.assertIsNotNone(Donor)
        self.assertIsNotNone(DonationBatch)
        self.assertIsNotNone(Donation)
        self.assertIsNotNone(Invoice)
        self.assertIsNotNone(AuditLog)
