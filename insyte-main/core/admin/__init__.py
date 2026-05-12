"""Core admin package — User admin only.

Domain-model admins live in their owning app's ``admin.py`` and are
auto-discovered by Django when the app is in INSTALLED_APPS.
"""

from core.admin.user import *  # noqa: F403
