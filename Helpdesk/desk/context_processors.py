from django.conf import settings


def organization_context(request):
    can_view_statistics = False
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        can_view_statistics = user.is_superuser or user.groups.filter(
            name__in=("Админ", "Исполнитель", "Директор")
        ).exists()

    return {
        "organization_name": getattr(settings, "ORGANIZATION_NAME", "Helpdesk"),
        "can_view_statistics": can_view_statistics,
    }
