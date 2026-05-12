"""Abstract base models for the donation management system."""

from django.db import models


class CreateAndUpdateTimestampModel(models.Model):
    """Abstract base model that automatically tracks creation and update timestamps.

    Provides created_at and updated_at fields to all models that inherit from it.
    This is an abstract model and will not create a database table.

    Attributes:
        created_at: Timestamp when the record was created.
        updated_at: Timestamp when the record was last updated.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
