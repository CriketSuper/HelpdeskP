from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from .models import UserProfile, admin_group_name, executor_group_name


@receiver(post_migrate)
def ensure_default_roles(sender, **kwargs):
    if sender.name != "desk":
        return

    admin_group, _ = Group.objects.get_or_create(name=admin_group_name)
    Group.objects.get_or_create(name=executor_group_name)

    user_model = get_user_model()

    if not user_model.objects.exists():
        user = user_model.objects.create_superuser(
            username=settings.DEFAULT_ADMIN_USERNAME,
            email=settings.DEFAULT_ADMIN_EMAIL,
            password=settings.DEFAULT_ADMIN_PASSWORD,
        )
    else:
        user = user_model.objects.filter(username=settings.DEFAULT_ADMIN_USERNAME).first()
        if user is None:
            return

    user.groups.add(admin_group)
    UserProfile.objects.get_or_create(
        user=user,
        defaults={"verbose_name": settings.DEFAULT_ADMIN_VERBOSE_NAME},
    )
