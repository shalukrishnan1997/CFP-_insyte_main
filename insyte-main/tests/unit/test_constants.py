"""Unit tests for core.constants module."""

from django.test import SimpleTestCase

from core.constants import CURRENCY_CODE, CURRENCY_SYMBOL


class TestCurrencyConstants(SimpleTestCase):
    """Verify currency constants are defined correctly."""

    def test_currency_code_is_gbp(self) -> None:
        self.assertEqual(CURRENCY_CODE, "GBP")

    def test_currency_symbol_is_pound(self) -> None:
        self.assertEqual(CURRENCY_SYMBOL, "£")
