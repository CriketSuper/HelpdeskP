from django.conf import settings


def organization_context(request):
    return {
        "organization_name": getattr(settings, "ORGANIZATION_NAME", "Helpdesk"),
    }
