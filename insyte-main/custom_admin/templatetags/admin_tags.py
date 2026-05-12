"""
Custom template tags for admin templates.
"""

from typing import Any

from django import template
from django.urls import reverse

register = template.Library()


@register.simple_tag(takes_context=True)
def admin_url(context: dict[str, Any], url_name: str, *args: Any, **kwargs: Any) -> str:
    """
    Generate URL using the dynamic namespace based on user role.
    Usage: {% admin_url 'admin_dashboard' %}
    Usage with args: {% admin_url 'campaign_detail' campaign.id %}
    """
    namespace = context.get("url_namespace", "custom_admin")
    full_url_name = f"{namespace}:{url_name}"
    return reverse(full_url_name, args=args, kwargs=kwargs)


@register.filter
def has_perm(user: object, permission: str) -> bool:
    """
    Check if user has a specific permission.
    Usage: {% if request.user|has_perm:'custom_admin.add_campaign' %}
    """
    if user.is_staff:
        return True
    return user.has_perm(permission)


@register.filter
def in_list(value: str, arg: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """
    Check if value is in the list.
    Usage: {% if 'add_campaign'|in_list:user_permissions %}
    """
    if not arg:
        return False

    return "all" in arg or value in arg


@register.filter
def not_in_list(value: str, arg: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """
    Check if value is NOT in the list.
    Usage: {% if 'delete_campaign'|not_in_list:user_permissions %}
    """
    return not in_list(value, arg)


@register.filter
def dictget(dictionary: dict[str, Any] | Any, key: str) -> Any | None:
    """
    Get a value from a dictionary using a dynamic key.
    Usage: {{ payment_type_labels|dictget:method|default:method }}
    """
    if not isinstance(dictionary, dict):
        return None
    return dictionary.get(key, None)


@register.filter
def replace(value: Any, arg: str) -> str:
    """
    Replace occurrences in a string.
    Usage: {{ issue|title|replace:"_," }}
    The arg should be in format "old_string,new_string"
    """
    if not isinstance(value, str):
        value = str(value)

    if "," in arg:
        old, new = arg.split(",", 1)
        return value.replace(old, new)
    return value
