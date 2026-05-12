"""Core models for the donation management system.

This package defines the core Django models including User, Client, Campaign,
Donor, Donation, and related models for managing charity donation campaigns.

All models are re-exported from this ``__init__`` so that existing imports like
``from core.models import Campaign`` continue to work without changes.
"""

from core.models.base import CreateAndUpdateTimestampModel
from core.models.user import User

__all__ = [
    "CreateAndUpdateTimestampModel",
    "User",
]
