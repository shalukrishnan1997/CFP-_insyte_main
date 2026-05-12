import pytest
from django.test import Client as DjangoClient
from django.urls import reverse

from payments.models import PaymentGatewayConfig
from tests.factories import ClientFactory


@pytest.mark.django_db
class TestClientPaymentConfig:
    def test_page_renders_stripe_only(self, authenticated_client: DjangoClient) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.get(url)

        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Stripe" in content
        assert "SagePay (Opayo)" not in content
        assert "WorldPay" not in content

    def test_post_stripe_saves_config(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        payload = {
            "provider": "stripe",
            "is_active": "on",
            "stripe_secret_key": "sk_test_secret",
            "stripe_publishable_key": "pk_test_publishable",
            "stripe_webhook_secret": "whsec_test",
        }
        response = authenticated_client.post(url, payload)

        assert response.status_code == 302
        config = PaymentGatewayConfig.objects.get(client=client_obj, provider="stripe")
        assert config.is_active is True
        assert config.secret_key_encrypted == "sk_test_secret"
        assert config.publishable_key_encrypted == "pk_test_publishable"
        assert config.webhook_secret_encrypted == "whsec_test"

    def test_post_empty_secret_and_webhook_preserves_existing(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )
        authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "sk_test_keep_me",
                "stripe_publishable_key": "pk_test_v1",
                "stripe_webhook_secret": "whsec_keep_me",
            },
        )
        authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "",
                "stripe_publishable_key": "pk_test_v2",
                "stripe_webhook_secret": "",
            },
        )
        config = PaymentGatewayConfig.objects.get(client=client_obj, provider="stripe")
        assert config.secret_key_encrypted == "sk_test_keep_me"
        assert config.publishable_key_encrypted == "pk_test_v2"
        assert config.webhook_secret_encrypted == "whsec_keep_me"

    def test_post_non_stripe_provider_is_blocked(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.post(
            url,
            {
                "provider": "worldpay",
                "is_active": "on",
                "worldpay_merchant": "merchant",
            },
        )

        assert response.status_code == 302
        assert (
            PaymentGatewayConfig.objects.filter(
                client=client_obj,
                provider="worldpay",
            ).exists()
            is False
        )

    def test_post_rejects_wrong_prefix_publishable_key(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "sk_test_ok",
                "stripe_publishable_key": "sk_test_pasted_into_wrong_slot",
                "stripe_webhook_secret": "whsec_ok",
            },
        )

        assert response.status_code == 302
        assert not PaymentGatewayConfig.objects.filter(
            client=client_obj, provider="stripe"
        ).exists()

    def test_post_rejects_wrong_prefix_secret_key(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "pk_test_pasted_into_wrong_slot",
                "stripe_publishable_key": "pk_test_ok",
                "stripe_webhook_secret": "whsec_ok",
            },
        )

        assert response.status_code == 302
        assert not PaymentGatewayConfig.objects.filter(
            client=client_obj, provider="stripe"
        ).exists()

    def test_post_rejects_wrong_prefix_webhook_secret(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "sk_test_ok",
                "stripe_publishable_key": "pk_test_ok",
                "stripe_webhook_secret": "not_a_whsec_value",
            },
        )

        assert response.status_code == 302
        assert not PaymentGatewayConfig.objects.filter(
            client=client_obj, provider="stripe"
        ).exists()

    def test_post_accepts_restricted_secret_key_prefix(
        self,
        authenticated_client: DjangoClient,
    ) -> None:
        client_obj = ClientFactory()
        url = reverse(
            "custom_admin:client_payment_config", kwargs={"client_id": client_obj.id}
        )

        response = authenticated_client.post(
            url,
            {
                "provider": "stripe",
                "is_active": "on",
                "stripe_secret_key": "rk_test_restricted",
                "stripe_publishable_key": "pk_test_ok",
                "stripe_webhook_secret": "whsec_ok",
            },
        )

        assert response.status_code == 302
        config = PaymentGatewayConfig.objects.get(client=client_obj, provider="stripe")
        assert config.secret_key_encrypted == "rk_test_restricted"
