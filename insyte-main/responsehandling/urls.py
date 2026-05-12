"""
URL configuration for responsehandling project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import FileResponse, Http404, HttpRequest, HttpResponseBase
from django.urls import include, path, re_path
from django.utils.http import urlencode
from django.views.generic import RedirectView

from core.health import health_check, health_live
from core.metrics import metrics_view
from core.storage_helpers import media_storage_exists, open_media_storage_file
from responsehandling.permissions import user_has_access


def protected_media_serve(request: HttpRequest, path: str) -> HttpResponseBase:
    """Serve media files only to authorised internal users in production."""
    import posixpath

    if not request.user.is_authenticated:
        login_query = urlencode({REDIRECT_FIELD_NAME: request.get_full_path()})
        from django.shortcuts import redirect

        return redirect(f"{settings.LOGIN_URL}?{login_query}")

    if not user_has_access(request.user):
        raise Http404

    # Sanitise path to prevent directory traversal.
    clean_path = posixpath.normpath(path).lstrip("/")
    if ".." in clean_path:
        raise Http404

    if not media_storage_exists(clean_path):
        raise Http404

    return FileResponse(open_media_storage_file(clean_path))


urlpatterns = [
    # Liveness: DB only — use for Docker/proxy health checks (see docker-compose)
    path("health/live/", health_live, name="health_live"),
    # Readiness: DB + cache + Celery + optional checks (monitoring)
    path("health/", health_check, name="health_check"),
    # Prometheus metrics (internal IP allowlist; see core.metrics.metrics_view)
    path("metrics", metrics_view, name="prometheus_metrics"),
    # Django Admin
    path("django-admin/", admin.site.urls),
    # Root redirect to login
    path(
        "",
        RedirectView.as_view(url="/auth/login/", permanent=False, query_string=True),
        name="home",
    ),
    # App URLs with namespaces
    path("auth/", include("auth_app.urls")),  # Authentication routes
    path(
        "admin/", include("custom_admin.urls")
    ),  # Admin routes (staff only) - includes REST API at /admin/api/
    path("client/", include("client_portal.urls")),  # Client portal (client users only)
    path("", include("core.urls")),  # User-facing routes
]

# Serve static and media files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # In production: serve media via authenticated Django view.
    # For better performance, configure Traefik/nginx or Cloudflare R2 to serve
    # media files directly and remove this block.

    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            protected_media_serve,
            name="protected_media",
        ),
    ]
