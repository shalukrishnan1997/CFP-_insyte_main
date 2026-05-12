"""Notification views for in-app notifications."""

import asyncio

from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from notifications.models import Notification
from responsehandling.permissions import is_authenticated_and_is_staff


@is_authenticated_and_is_staff
def notification_list(request: HttpRequest) -> HttpResponse:
    """Display list of notifications for current user.

    Args:
        request: HTTP request

    Returns:
        Rendered notification list page or JSON response if format=json
    """
    # Get filter
    filter_type = request.GET.get("type", "all")
    unread_flag = request.GET.get("unread")

    # Base queryset
    notifications = Notification.objects.filter(user=request.user)

    # Apply filters
    if filter_type == "unread" or (
        unread_flag and unread_flag.lower() in {"true", "1", "yes"}
    ):
        notifications = notifications.filter(is_read=False)
    elif filter_type == "read":
        notifications = notifications.filter(is_read=True)
    elif filter_type != "all":
        notifications = notifications.filter(notification_type=filter_type)

    # Get counts for filters using a single query with conditional aggregation
    from django.db.models import Count
    from django.db.models import Q as DQ

    user_notifs = Notification.objects.filter(user=request.user)
    counts = user_notifs.aggregate(
        all=Count("id"),
        unread=Count("id", filter=DQ(is_read=False)),
        read=Count("id", filter=DQ(is_read=True)),
        success=Count("id", filter=DQ(notification_type=Notification.TYPE_SUCCESS)),
        warning=Count("id", filter=DQ(notification_type=Notification.TYPE_WARNING)),
        error=Count("id", filter=DQ(notification_type=Notification.TYPE_ERROR)),
        info=Count("id", filter=DQ(notification_type=Notification.TYPE_INFO)),
    )

    notifications = notifications.order_by("-created_at")[
        :50
    ]  # Limit to 50 most recent

    # Return JSON if requested
    if request.GET.get("format") == "json":
        notif_list = []
        for notif in notifications:
            notif_list.append(
                {
                    "id": str(notif.id),
                    "title": notif.title,
                    "message": notif.message,
                    "notification_type": notif.notification_type,
                    "is_read": notif.is_read,
                    "link": notif.link or "",
                    "created_at": notif.created_at.isoformat(),
                    "time": notif.created_at.strftime("%Y-%m-%d %H:%M"),
                }
            )

        return JsonResponse(
            {
                "notifications": notif_list,
                "unread_count": counts["unread"],
                "total_count": counts["all"],
            }
        )

    context = {
        "active": "notifications",
        "notifications": notifications,
        "filter_type": filter_type,
        "counts": counts,
        "page_title": "Notifications",
    }

    return render(request, "admin/notifications/list.html", context)


@is_authenticated_and_is_staff
@require_http_methods(["POST"])
def notification_mark_read(request: HttpRequest, notification_id: str) -> JsonResponse:
    """Mark a notification as read.

    Args:
        request: HTTP POST request
        notification_id: UUID of notification

    Returns:
        JSON response with success status
    """
    notification = get_object_or_404(
        Notification, id=notification_id, user=request.user
    )

    if not notification.is_read:
        from django.utils import timezone

        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])

    return JsonResponse({"success": True, "message": "Notification marked as read"})


@is_authenticated_and_is_staff
@require_http_methods(["POST"])
def notification_mark_all_read(request: HttpRequest) -> JsonResponse:
    """Mark all notifications as read for current user.

    Args:
        request: HTTP POST request

    Returns:
        JSON response with count of marked notifications
    """
    from django.utils import timezone

    count = Notification.objects.filter(user=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"{count} notifications marked as read",
            "count": count,
        }
    )


@is_authenticated_and_is_staff
def notification_count_api(request: HttpRequest) -> JsonResponse:
    """API endpoint to get unread notification count.

    Used by frontend to show notification badge.

    Args:
        request: HTTP request

    Returns:
        JSON response with unread count
    """
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()

    return JsonResponse({"unread_count": unread_count})


@is_authenticated_and_is_staff  # pyright: ignore[reportArgumentType]
def notification_stream_api(request: HttpRequest) -> StreamingHttpResponse:
    """SSE endpoint for real-time notification updates.

    MAINTENANCE WARNING: This implementation uses a synchronous iterator which
    blocks a web server worker thread for the duration of the connection.
    In environments like Gunicorn (WSGI), this can quickly exhaust the worker pool
    causing site-wide 504 errors or extreme latency.

    This should ONLY be used in an ASGI context with an asynchronous worker
    or disabled for performance stability.
    """
    import time

    from asgiref.sync import async_to_sync

    class NotificationStreamIterator:
        def __init__(self, user: object) -> None:
            self.user = user
            self.last_count = -1

        async def _get_count(self):
            return await Notification.objects.filter(
                user=self.user, is_read=False
            ).acount()

        def __iter__(self):
            """Synchronous iterator for WSGI servers."""
            iterations = 0
            MAX_ITERATIONS = 20  # Safety exit after ~5 minutes (20 * 15s) so a wedged/abandoned tab can't hog a gunicorn thread.

            while iterations < MAX_ITERATIONS:
                try:
                    unread_count = async_to_sync(self._get_count)()
                    if unread_count != self.last_count:
                        yield f"data: {unread_count}\n\n"
                        self.last_count = unread_count
                    else:
                        yield ": heartbeat\n\n"
                    time.sleep(15)
                    iterations += 1
                except Exception:
                    time.sleep(5)
                    iterations += 1

        async def __aiter__(self):
            """Asynchronous iterator for ASGI servers."""
            iterations = 0
            MAX_ITERATIONS = 400  # More lenient for async (~100 mins)

            while iterations < MAX_ITERATIONS:
                try:
                    unread_count = await self._get_count()
                    if unread_count != self.last_count:
                        yield f"data: {unread_count}\n\n"
                        self.last_count = unread_count
                    else:
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(15)
                    iterations += 1
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(5)
                    iterations += 1

    response = StreamingHttpResponse(
        NotificationStreamIterator(request.user), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # Disable buffering for Nginx
    return response
