import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from donors.admin_views import _parse_upload, replace_house_file_rows
from tests.factories import (
    ClientFactory,
    DonationFactory,
    DonorFactory,
    SystemDonorFactory,
)


def test_parse_upload_accepts_pipe_delimited_csv() -> None:
    upload = SimpleUploadedFile(
        "house_file.csv",
        b"urn|first_name|last_name|postcode\nURN001|John|Smith|LS1 4AB\n",
        content_type="text/csv",
    )

    rows = _parse_upload(upload)

    assert rows == [
        {
            "urn": "URN001",
            "first_name": "John",
            "last_name": "Smith",
            "postcode": "LS1 4AB",
        }
    ]


def test_parse_upload_rejects_comma_delimited_csv() -> None:
    upload = SimpleUploadedFile(
        "house_file.csv",
        b"urn,first_name,last_name,postcode\nURN001,John,Smith,LS1 4AB\n",
        content_type="text/csv",
    )

    with pytest.raises(
        ValueError,
        match=r"Invalid delimiter. Only pipe-delimited CSV files are supported.",
    ):
        _parse_upload(upload)


def test_parse_upload_rejects_excel_files() -> None:
    upload = SimpleUploadedFile(
        "house_file.xlsx",
        b"fake-excel-content",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    with pytest.raises(
        ValueError,
        match=r"Unsupported file format. Please upload a pipe-delimited .csv file.",
    ):
        _parse_upload(upload)


@pytest.mark.django_db()
def test_replace_house_file_rows_is_scoped_to_selected_client() -> None:
    client_a = ClientFactory(name="Client A")
    client_b = ClientFactory(name="Client B")
    DonorFactory(
        client=client_a,
        urn="OLD-A",
        first_name="Old",
        last_name="House",
        postcode="LS1 9ZZ",
    )
    donor_b = DonorFactory(
        client=client_b,
        urn="OLD-B",
        first_name="Other",
        last_name="Client",
        postcode="LS1 4AB",
    )

    result = replace_house_file_rows(
        rows=[
            {
                "urn": "URN-CLIENT-A",
                "first_name": "John",
                "last_name": "Smith",
                "postcode": "LS1 4AB",
            }
        ],
        client=client_a,
    )

    donor_b.refresh_from_db()

    assert result["replaced_count"] == 1
    assert not type(donor_b).objects.filter(client=client_a, urn="OLD-A").exists()
    assert type(donor_b).objects.filter(client=client_a, urn="URN-CLIENT-A").exists()
    assert donor_b.urn == "OLD-B"


@pytest.mark.django_db()
def test_replace_house_file_rows_allows_same_urn_in_different_clients() -> None:
    client_a = ClientFactory(name="Client A")
    client_b = ClientFactory(name="Client B")
    DonorFactory(
        client=client_b,
        urn="URN-SHARED",
        first_name="Alice",
        last_name="Brown",
        postcode="SW1A 1AA",
    )
    old_client_a_donor = DonorFactory(
        client=client_a,
        urn="OLD-A",
        first_name="John",
        last_name="Smith",
        postcode="LS1 4AB",
    )

    result = replace_house_file_rows(
        rows=[
            {
                "urn": "URN-SHARED",
                "first_name": "John",
                "last_name": "Smith",
                "postcode": "LS1 4AB",
            }
        ],
        client=client_a,
    )

    assert result["replaced_count"] == 1
    assert (
        not type(old_client_a_donor).objects.filter(pk=old_client_a_donor.pk).exists()
    )
    assert (
        type(old_client_a_donor)
        .objects.filter(client=client_a, urn="URN-SHARED")
        .exists()
    )


@pytest.mark.django_db()
def test_replace_house_file_rows_rejects_duplicate_urns_in_upload() -> None:
    client = ClientFactory(name="Client A")

    with pytest.raises(ValueError, match="Duplicate URN 'URN001'"):
        replace_house_file_rows(
            rows=[
                {
                    "urn": "URN001",
                    "first_name": "John",
                    "last_name": "Smith",
                    "postcode": "LS1 4AB",
                },
                {
                    "urn": "URN001",
                    "first_name": "Jane",
                    "last_name": "Smith",
                    "postcode": "LS1 4AC",
                },
            ],
            client=client,
        )


@pytest.mark.django_db()
def test_replace_house_file_rows_preserves_internal_donor_history() -> None:
    client = ClientFactory(name="Client A")
    source_donor = DonorFactory(
        client=client,
        urn="OLD-URN",
        first_name="John",
        last_name="Smith",
        postcode="LS1 4AB",
    )
    system_donor = SystemDonorFactory(client=client, external_urn="OLD-URN")
    donation = DonationFactory(
        campaign__client=client,
        donor=source_donor,
        system_donor=system_donor,
    )

    replace_house_file_rows(
        rows=[
            {
                "urn": "NEW-URN",
                "first_name": "Jane",
                "last_name": "Doe",
                "postcode": "LS1 4AC",
            }
        ],
        client=client,
    )

    donation.refresh_from_db()
    assert donation.system_donor == system_donor
    assert donation.donor is None
