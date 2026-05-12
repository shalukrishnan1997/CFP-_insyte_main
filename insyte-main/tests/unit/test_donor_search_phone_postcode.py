"""Unit tests for the phone+postcode extension to ``donor_search``."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from core.services.phone import looks_like_phone_query, normalize_phone
from custom_admin.api_views import _system_donor_search_query
from donors.models import SystemDonor
from tests.factories import (
    CampaignFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


class TestNormalizePhone:
    """The shared helper used by both the search API and the model save hook."""

    def test_strips_spaces_and_hyphens(self) -> None:
        assert normalize_phone("07700 900 123") == "07700900123"
        assert normalize_phone("07700-900-123") == "07700900123"

    def test_collapses_uk_plus_44(self) -> None:
        assert normalize_phone("+44 7700 900 123") == "07700900123"
        assert normalize_phone("+447700900123") == "07700900123"

    def test_collapses_uk_0044(self) -> None:
        assert normalize_phone("0044 7700 900 123") == "07700900123"

    def test_strips_parentheses_and_dots(self) -> None:
        assert normalize_phone("(0044) 7700.900.123") == "07700900123"

    def test_empty_returns_empty(self) -> None:
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""

    def test_non_uk_international_keeps_country_digits(self) -> None:
        # Non-UK international: digits-only including country code, no leading +.
        assert normalize_phone("+1 (415) 555-0100") == "14155550100"


class TestLooksLikePhoneQuery:
    """The threshold heuristic that branches into the phone-search path."""

    def test_six_or_more_digits_qualifies(self) -> None:
        assert looks_like_phone_query("07700 900 123") is True
        assert looks_like_phone_query("123456") is True

    def test_short_digit_strings_do_not_qualify(self) -> None:
        assert looks_like_phone_query("12345") is False
        assert looks_like_phone_query("URN12") is False

    def test_pure_text_does_not_qualify(self) -> None:
        assert looks_like_phone_query("Smith") is False


@pytest.mark.django_db()
class TestSystemDonorSearchQuery:
    """The Q builder routing phone-shaped queries to ``normalized_phone``."""

    def test_phone_query_hits_normalized_phone(self) -> None:
        client = CampaignFactory().client
        match = SystemDonorFactory(client=client, phone="07700 900 123")
        SystemDonorFactory(client=client, phone="01234567890")

        results = list(
            SystemDonor.objects.filter(_system_donor_search_query("07700900123"))
        )

        assert match in results
        # The 01234567890 donor must not appear — phone-shaped query must
        # only return phone matches, not text-search hits.
        assert len(results) == 1

    def test_text_query_searches_name_and_postcode(self) -> None:
        client = CampaignFactory().client
        # SystemDonorFactory defaults postcode to SW1A 1AA — override to keep
        # the postcode-search test isolated to the row we create with that postcode.
        SystemDonorFactory(
            client=client, first_name="Alice", last_name="Walker", postcode="M1 1AA"
        )
        SystemDonorFactory(
            client=client, postcode="SW1A 1AA", first_name="Bob", last_name="Clark"
        )

        by_name = list(SystemDonor.objects.filter(_system_donor_search_query("Walker")))
        by_postcode = list(
            SystemDonor.objects.filter(_system_donor_search_query("SW1A"))
        )

        assert len(by_name) == 1
        assert by_name[0].last_name == "Walker"
        assert len(by_postcode) == 1
        assert by_postcode[0].postcode == "SW1A 1AA"


@pytest.mark.django_db()
class TestDonorSearchEndpoint:
    """End-to-end smoke through the HTTP endpoint."""

    def _login(self) -> tuple[Client, object]:
        user = UserFactory(is_staff=True)
        client = Client()
        client.force_login(user)
        return client, user

    def test_phone_search_returns_systemdonor_first(self) -> None:
        http, _ = self._login()
        campaign = CampaignFactory()
        match = SystemDonorFactory(client=campaign.client, phone="07700 900 123")
        SystemDonorFactory(client=campaign.client, phone="01234567890")

        url = reverse("custom_admin:donor_search")
        resp = http.get(url, {"q": "07700900123", "campaign_id": str(campaign.id)})
        assert resp.status_code == 200
        body = json.loads(resp.content)
        urns = [d["urn"] for d in body["donors"]]
        # Both SystemDonors share the same client; only the phone match should appear.
        assert match.external_urn in urns
        assert len(body["donors"]) == 1

    def test_postcode_search_returns_match(self) -> None:
        http, _ = self._login()
        campaign = CampaignFactory()
        SystemDonorFactory(client=campaign.client, postcode="SW1A 1AA")

        url = reverse("custom_admin:donor_search")
        resp = http.get(url, {"q": "SW1A", "campaign_id": str(campaign.id)})

        assert resp.status_code == 200
        body = json.loads(resp.content)
        assert len(body["donors"]) >= 1

    def test_dedupe_drops_house_file_donor_with_matching_systemdonor(self) -> None:
        """SystemDonor wins when the same URN appears in both tables."""
        http, _ = self._login()
        campaign = CampaignFactory()
        DonorFactory(client=campaign.client, urn="DUPE-001")
        SystemDonorFactory(client=campaign.client, external_urn="DUPE-001")

        url = reverse("custom_admin:donor_search")
        resp = http.get(url, {"q": "DUPE-001", "campaign_id": str(campaign.id)})

        body = json.loads(resp.content)
        sources = [d["source"] for d in body["donors"]]
        # DUPE-001 should appear once, from SystemDonor.
        assert sources.count("system_donor") == 1
        assert "house_file" not in sources

    def test_short_query_returns_empty(self) -> None:
        http, _ = self._login()
        url = reverse("custom_admin:donor_search")
        resp = http.get(url, {"q": "A"})
        body = json.loads(resp.content)
        assert body == {"donors": []}

    def test_serializers_include_phone_intake_prefill_keys(self) -> None:
        """Phone-intake's non-financial editor prefills donor fields from the
        donor-search response. These keys must be in every serializer."""
        http, _ = self._login()
        campaign = CampaignFactory()
        # Match query "Pre" against last_name across all three donor types.
        SystemDonorFactory(
            client=campaign.client, last_name="Prefill", external_urn="PRE-SYS"
        )
        DonorFactory(client=campaign.client, last_name="Prefill", urn="PRE-HF")

        url = reverse("custom_admin:donor_search")
        resp = http.get(url, {"q": "Prefill", "campaign_id": str(campaign.id)})
        assert resp.status_code == 200

        donors = json.loads(resp.content)["donors"]
        assert donors, "expected at least one match"
        required_keys = {
            "date_of_birth",
            "age",
            "gift_aid_date",
            "contact_status",
            "contact_status_reason",
            "contact_status_changed_at",
            "address_last_verified_at",
        }
        for record in donors:
            missing = required_keys - record.keys()
            assert not missing, (
                f"Serializer for source={record.get('source')} is missing "
                f"keys: {missing}"
            )
