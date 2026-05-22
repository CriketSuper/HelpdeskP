from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import AuthEvent, Document, Ticket, UserProfile

User = get_user_model()


def _get_profile_name(user):
    if user is None:
        return None

    try:
        return user.profile.verbose_name
    except UserProfile.DoesNotExist:
        return user.get_username()


class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "get_created_by_verbose_name",
        "get_technician_verbose_name",
        "criticalness",
        "published",
        "progress",
        "status",
    )
    list_display_links = ("id", "title")
    search_fields = ("id", "title")

    def get_created_by_verbose_name(self, obj):
        return _get_profile_name(obj.created_by)

    get_created_by_verbose_name.short_description = "Автор"

    def get_technician_verbose_name(self, obj):
        return _get_profile_name(obj.technician)

    get_technician_verbose_name.short_description = "Исполнитель"


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name = "Дополнительная информация"
    verbose_name_plural = "Дополнительная информация"
    fieldsets = (
        (
            None,
            {
                "fields": ("verbose_name", "position", "receive_email_notifications"),
            },
        ),
    )


class CustomUserAdmin(UserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)
admin.site.register(Ticket, TicketAdmin)
admin.site.register(Document)


@admin.register(AuthEvent)
class AuthEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_type", "username", "user", "ip_address")
    list_filter = ("event_type", "created_at")
    search_fields = ("username", "user__username", "user__profile__verbose_name", "ip_address")
    readonly_fields = ("created_at", "event_type", "username", "user", "ip_address", "user_agent", "metadata")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
