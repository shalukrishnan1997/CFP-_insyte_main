from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from core.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Custom User admin with additional fields."""

    list_display = [
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "is_active",
        "date_joined",
    ]
    list_filter = ["is_staff", "is_active", "is_superuser", "date_joined"]
    search_fields = ["username", "email", "first_name", "last_name"]
    date_hierarchy = "date_joined"
