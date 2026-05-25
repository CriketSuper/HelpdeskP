from django.conf import settings

from .models import admin_group_name, director_group_name, executor_group_name, technical_staff_group_name


def organization_context(request):
    can_view_statistics = False
    can_view_system_status = False
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        can_view_statistics = user.is_superuser or user.groups.filter(
            name__in=(admin_group_name, executor_group_name, director_group_name)
        ).exists()
        can_view_system_status = user.groups.filter(name=technical_staff_group_name).exists()

    return {
        "organization_name": getattr(settings, "ORGANIZATION_NAME", "Helpdesk"),
        "can_view_statistics": can_view_statistics,
        "can_view_system_status": can_view_system_status,
    }
