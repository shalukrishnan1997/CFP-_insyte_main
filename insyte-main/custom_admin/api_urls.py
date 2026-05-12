"""
API URL Configuration - Minimal REST API for Django templates
Only includes the 3 viewsets actually used by templates:
- DataFileUploadViewSet: File upload and progress tracking (donation_client_campaigns.html)
- CampaignDataFileViewSet: Link files to campaigns (donation_client_campaigns.html)
- LetterBatchViewSet: Letter batch status notifications (admin_layout.html)

Note: Other endpoints use function-based views in custom_admin/urls.py:
- /admin/api/donors/search/ → api_views.donor_search
- /admin/api/campaigns/ → api_views.campaigns_by_client_api
- /admin/api/service-items/ → views.service_items_api
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api_views import (
    CampaignDataFileViewSet,
    DataFileUploadViewSet,
    LetterBatchViewSet,
)

# Create router and register ONLY viewsets used by Django templates
router = DefaultRouter()
router.register(
    r"campaign-data-files", CampaignDataFileViewSet, basename="campaign-data-file"
)
router.register(
    r"data-file-uploads", DataFileUploadViewSet, basename="data-file-upload"
)
router.register(r"letter-batches", LetterBatchViewSet, basename="letter-batch")

urlpatterns = [
    # All API endpoints (Session authentication only - no JWT needed)
    path("", include(router.urls)),
]
