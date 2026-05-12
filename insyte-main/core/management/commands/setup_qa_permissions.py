"""Management command to create QA permission group."""

from typing import Any

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from donations.models import Donation, DonationBatch


class Command(BaseCommand):
    """Create QA permission group with specific permissions."""

    help = "Creates QA group with permissions to modify QA status, edit donations, and create donations"

    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the command."""
        self.stdout.write("Creating QA permission group...")

        # Get or create QA group
        qa_group, created = Group.objects.get_or_create(name="QA")

        if created:
            self.stdout.write(self.style.SUCCESS("✓ Created QA group"))
        else:
            self.stdout.write(self.style.WARNING("⚠ QA group already exists"))

        # Clear existing permissions
        qa_group.permissions.clear()

        # Get content types
        donation_ct = ContentType.objects.get_for_model(Donation)
        batch_ct = ContentType.objects.get_for_model(DonationBatch)

        # Define required permissions
        required_permissions = [
            # Donation permissions
            ("view_donation", donation_ct),
            ("add_donation", donation_ct),
            ("change_donation", donation_ct),
            # DonationBatch permissions (QA status management)
            ("view_donationbatch", batch_ct),
            ("change_donationbatch", batch_ct),
        ]

        added_count = 0
        for codename, content_type in required_permissions:
            try:
                permission = Permission.objects.get(
                    codename=codename, content_type=content_type
                )
                qa_group.permissions.add(permission)
                added_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ Added permission: {content_type.app_label}.{codename}"
                    )
                )
            except Permission.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ Permission not found: {content_type.app_label}.{codename}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ Successfully configured QA group with {added_count} permissions"
            )
        )
        self.stdout.write("\nQA users can now:")
        self.stdout.write("  • View and review donation batches")
        self.stdout.write("  • Change batch QA status (approve/reject)")
        self.stdout.write("  • View, edit, and create donations")
        self.stdout.write("\nTo assign users to QA group:")
        self.stdout.write("  1. Go to Admin > Users")
        self.stdout.write('  2. Edit user and add to "QA" group')
