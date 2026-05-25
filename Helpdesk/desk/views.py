import hashlib
import logging
import mimetypes
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LogoutView
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Max, Min, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.generic.edit import CreateView

from .document_exports import (
    TICKET_TEMPLATE_PATH,
    build_ticket_docx_filename,
    build_ticket_pdf_filename,
    render_ticket_docx,
    render_ticket_pdf,
)
from .forms import (
    LoginForm,
    PasswordUpdateForm,
    ProfileSettingsForm,
    TicketEditForm,
    TicketForm,
)
from .models import (
    AuthEvent,
    Document,
    Notification,
    Ticket,
    UserProfile,
    admin_group_name,
    director_group_name,
    executor_group_name,
    technical_staff_group_name,
    get_assignable_technicians,
    get_default_technician_user,
)
from .rich_text import (
    rich_text_has_text,
    rich_text_to_audit_text,
    rich_text_to_plain_text,
    sanitize_rich_text,
)

User = get_user_model()
logger = logging.getLogger(__name__)

def _current_timestamp():
    return timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")


def _get_user_display_name(user):
    if user is None:
        return ""

    try:
        return user.profile.verbose_name
    except UserProfile.DoesNotExist:
        return user.get_username()



def _get_user_profile(user):
    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={"verbose_name": user.get_username()},
    )
    return profile



def _get_notification_email(user):
    if user is None:
        return None

    profile = _get_user_profile(user)
    if not profile.receive_email_notifications:
        return None

    return user.email or None


def _notifications_are_enabled():
    if not getattr(settings, "EMAIL_NOTIFICATIONS_ENABLED", False):
        return False

    if (
        settings.EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"
        and not settings.EMAIL_HOST
    ):
        logger.warning("Email notifications are enabled, but EMAIL_HOST is empty.")
        return False

    return True


def _get_notification_recipients(*users):
    recipients = []
    seen = set()

    for user in users:
        email = _get_notification_email(user)
        if not email:
            continue

        normalized_email = email.strip().lower()
        if normalized_email in seen:
            continue

        seen.add(normalized_email)
        recipients.append(email)

    return recipients


def _unique_users(*users, exclude_user=None, exclude_users=None):
    result = []
    seen = set()
    excluded_ids = set()

    if exclude_user is not None:
        excluded_ids.add(exclude_user.pk)
    for user in exclude_users or []:
        if user is not None:
            excluded_ids.add(user.pk)

    for user in users:
        if user is None:
            continue
        if user.pk in excluded_ids:
            continue
        if user.pk in seen:
            continue

        seen.add(user.pk)
        result.append(user)

    return result


def _add_ticket_participants(ticket, *users):
    valid_users = _unique_users(*users)
    if ticket.pk and valid_users:
        ticket.participants.add(*valid_users)


def _notify(subject, message, recipients):
    if not recipients or not _notifications_are_enabled():
        return False

    prefixed_subject = f'{getattr(settings, "EMAIL_SUBJECT_PREFIX", "")}{subject}'
    try:
        send_mail(
            prefixed_subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send email notification to %s", ", ".join(recipients))
        return False

    return True


def _notify_users(subject, message, *users):
    recipients = _get_notification_recipients(*users)
    return _notify(subject, message, recipients)


def _format_ticket_datetime(value):
    if not value:
        return "не указан"

    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _get_ticket_notification_link(ticket):
    ticket_path = reverse("ticket", kwargs={"ticket_id": ticket.pk})
    base_url = getattr(settings, "NOTIFICATION_BASE_URL", "").strip().rstrip("/")
    return f"{base_url}{ticket_path}" if base_url else ticket_path


def _build_ticket_email_body(ticket, event_summary, *, actor=None, details=None):
    detail_lines = []
    for detail in details or []:
        cleaned_detail = (detail or "").strip()
        if cleaned_detail:
            detail_lines.append(cleaned_detail)

    co_executor_names = [
        _get_user_display_name(user)
        for user in ticket.additional_executors.all()
    ]

    lines = [
        f"{getattr(settings, 'ORGANIZATION_NAME', 'Helpdesk')}",
        "",
        f"Заявка №{ticket.pk}",
        f"Тема: {ticket.title}",
        f"Автор: {_get_user_display_name(ticket.created_by)}",
        f"Основной исполнитель: {_get_user_display_name(ticket.technician) or 'не назначен'}",
        f"Соисполнители: {', '.join(co_executor_names) if co_executor_names else 'не назначены'}",
        f"Статус: {ticket.status}",
        f"Этап: {ticket.progress}",
        f"Дата создания: {_format_ticket_datetime(ticket.created_at)}",
        f"Дата изменения: {_format_ticket_datetime(ticket.published)}",
        f"Срок выполнения: {_format_ticket_datetime(ticket.deadline)}",
        "",
        f"Событие: {event_summary}",
    ]

    if actor:
        lines.append(f"Инициатор: {actor}")

    if detail_lines:
        lines.append("")
        lines.append("Детали:")
        lines.extend(f"- {detail}" for detail in detail_lines)

    lines.extend(
        [
            "",
            f"Ссылка на заявку: {_get_ticket_notification_link(ticket)}",
        ]
    )
    return "\n".join(lines)


def _create_in_app_notifications(
    title,
    body,
    *users,
    ticket=None,
    exclude_user=None,
    level=Notification.Levels.INFO,
):
    recipients = _unique_users(*users, exclude_user=exclude_user)
    if not recipients:
        return

    link = ""
    if ticket is not None:
        link = reverse("ticket", kwargs={"ticket_id": ticket.pk})

    Notification.objects.bulk_create(
        [
            Notification(
                user=user,
                title=title,
                body=body,
                level=level,
                link=link,
            )
            for user in recipients
        ]
    )


def _notify_ticket_users(
    title,
    body,
    *users,
    ticket=None,
    exclude_user=None,
    exclude_users=None,
    level=Notification.Levels.INFO,
    email_body=None,
):
    recipients = _unique_users(*users, exclude_user=exclude_user, exclude_users=exclude_users)
    _create_in_app_notifications(
        title,
        body,
        *recipients,
        ticket=ticket,
        level=level,
    )
    _notify_users(title, email_body or body, *recipients)


def _get_notification_collapse_key(notification):
    if notification.link and notification.link.startswith("/desk/"):
        return f"ticket:{notification.link}"
    return f"notification:{notification.pk}"


def _collapse_unread_notifications(notifications):
    grouped_notifications = {}

    for notification in notifications:
        group_key = _get_notification_collapse_key(notification)
        if group_key not in grouped_notifications:
            grouped_notifications[group_key] = {
                "notification": notification,
                "count": 1,
                "ids": [notification.pk],
            }
            continue

        grouped_notifications[group_key]["notification"] = notification
        grouped_notifications[group_key]["count"] += 1
        grouped_notifications[group_key]["ids"].append(notification.pk)

    collapsed_groups = sorted(
        grouped_notifications.values(),
        key=lambda item: (item["notification"].created_at, item["notification"].pk),
    )

    payload = []
    read_ids = []
    for group in collapsed_groups:
        notification = group["notification"]
        read_ids.extend(group["ids"])
        extra_updates = group["count"] - 1
        body = notification.body
        if extra_updates > 0:
            suffix = f"Еще {extra_updates} изменений по этой заявке."
            body = f"{body}\n\n{suffix}" if body else suffix

        payload.append(
            {
                "id": notification.pk,
                "title": notification.title,
                "body": body,
                "level": notification.level,
                "link": notification.link,
                "created_at": timezone.localtime(notification.created_at).strftime("%d.%m.%Y %H:%M"),
                "collapsed_count": group["count"],
            }
        )

    return payload, read_ids


def _validate_uploaded_documents(uploaded_documents):
    max_size = getattr(settings, "MAX_UPLOAD_SIZE_BYTES", 200 * 1024 * 1024)
    max_size_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 200)

    for uploaded_document in uploaded_documents:
        if uploaded_document.size > max_size:
            return (
                f'Файл "{uploaded_document.name}" превышает допустимый размер '
                f"({max_size_mb} МБ)."
            )

    return None


def _get_safe_redirect_target(request):
    redirect_to = (
        request.POST.get("next")
        or request.GET.get("next")
        or request.META.get("HTTP_REFERER")
    )
    if redirect_to and url_has_allowed_host_and_scheme(
        redirect_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect_to
    return reverse("profiles")


def _get_client_ip(request):
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _create_auth_event(event_type, *, request=None, user=None, username="", metadata=None):
    AuthEvent.objects.create(
        user=user,
        username=(username or "").strip(),
        event_type=event_type,
        ip_address=_get_client_ip(request) if request is not None else None,
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:512] if request is not None else ""),
        metadata=metadata or {},
    )


def _build_login_rate_limit_keys(request, submitted_identity):
    ip_address = (_get_client_ip(request) or "unknown").lower()
    identity = (submitted_identity or "").strip().lower() or "anonymous"
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "ip_failures": f"login-rate-limit:ip:{ip_address}:failures",
        "ip_blocked": f"login-rate-limit:ip:{ip_address}:blocked",
        "identity_failures": f"login-rate-limit:identity:{identity_hash}:failures",
        "identity_blocked": f"login-rate-limit:identity:{identity_hash}:blocked",
    }


def _is_login_rate_limited(request, submitted_identity):
    if not getattr(settings, "LOGIN_RATE_LIMIT_ENABLED", False):
        return False

    keys = _build_login_rate_limit_keys(request, submitted_identity)
    return bool(cache.get(keys["ip_blocked"]) or cache.get(keys["identity_blocked"]))


def _register_login_failure(request, submitted_identity, *, username="", reason="invalid_credentials"):
    keys = _build_login_rate_limit_keys(request, submitted_identity)
    max_attempts = max(1, int(getattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 5)))
    window_seconds = max(60, int(getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300)))
    block_seconds = max(60, int(getattr(settings, "LOGIN_RATE_LIMIT_BLOCK_SECONDS", 900)))

    blocked = False
    for failure_key, blocked_key in (
        (keys["ip_failures"], keys["ip_blocked"]),
        (keys["identity_failures"], keys["identity_blocked"]),
    ):
        attempts = int(cache.get(failure_key, 0)) + 1
        cache.set(failure_key, attempts, timeout=window_seconds)
        if attempts >= max_attempts:
            cache.set(blocked_key, True, timeout=block_seconds)
            cache.delete(failure_key)
            blocked = True

    _create_auth_event(
        AuthEvent.EventTypes.LOGIN_FAILURE,
        request=request,
        username=username or submitted_identity,
        metadata={"reason": reason, "blocked": blocked},
    )
    return blocked


def _reset_login_rate_limit(request, submitted_identity):
    keys = _build_login_rate_limit_keys(request, submitted_identity)
    cache.delete_many(
        [
            keys["ip_failures"],
            keys["ip_blocked"],
            keys["identity_failures"],
            keys["identity_blocked"],
        ]
    )


def _is_admin(user):
    return user.is_superuser or user.groups.filter(name=admin_group_name).exists()


def _is_executor(user):
    return user.groups.filter(name=executor_group_name).exists()


def _is_workflow_executor(user):
    return user.groups.filter(
        Q(name=executor_group_name) | Q(name=director_group_name)
    ).exists()


def _can_view_ticket(user, ticket):
    return (
        _is_admin(user)
        or ticket.created_by == user
        or ticket.technician == user
        or ticket.additional_executors.filter(pk=user.pk).exists()
        or ticket.participants.filter(pk=user.pk).exists()
    )


def _can_manage_ticket_workflow(user, ticket):
    return _is_admin(user) or (
        _is_workflow_executor(user)
        and (
            ticket.technician == user
            or ticket.additional_executors.filter(pk=user.pk).exists()
        )
    )


def _can_change_ticket_status(user, ticket):
    return _is_admin(user) or ticket.created_by == user


def _can_edit_ticket_details(user, ticket):
    return _is_admin(user) or ticket.created_by == user


def _can_manage_ticket_documents(user, ticket):
    return _is_admin(user) or ticket.created_by == user or ticket.technician == user


def _can_view_statistics(user):
    return _is_admin(user) or _is_workflow_executor(user)


def _can_view_system_status(user):
    return user.groups.filter(name=technical_staff_group_name).exists()


def _format_bytes(size_in_bytes):
    value = float(size_in_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{int(size_in_bytes)} B"


def _get_directory_metrics(path):
    total_size = 0
    file_count = 0
    for root, _, files in os.walk(path):
        for filename in files:
            file_path = os.path.join(root, filename)
            try:
                total_size += os.path.getsize(file_path)
                file_count += 1
            except OSError:
                continue
    return file_count, total_size


def _get_database_status():
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except OperationalError as error:
        return {
            "name": "База данных",
            "status": "error",
            "summary": "Нет подключения",
            "details": str(error),
        }

    return {
        "name": "База данных",
        "status": "ok",
        "summary": "Подключение установлено",
        "details": connection.settings_dict.get("ENGINE", ""),
    }


def _get_email_status():
    if not getattr(settings, "EMAIL_NOTIFICATIONS_ENABLED", False):
        return {
            "name": "Почта",
            "status": "info",
            "summary": "Уведомления отключены",
            "details": "EMAIL_NOTIFICATIONS_ENABLED=false",
        }

    if not settings.EMAIL_HOST:
        return {
            "name": "Почта",
            "status": "warning",
            "summary": "SMTP не настроен",
            "details": "EMAIL_HOST пустой",
        }

    security_mode = "SSL" if settings.EMAIL_USE_SSL else ("TLS" if settings.EMAIL_USE_TLS else "без шифрования")
    return {
        "name": "Почта",
        "status": "ok",
        "summary": f"{settings.EMAIL_HOST}:{settings.EMAIL_PORT}",
        "details": f"Режим: {security_mode}",
    }


def _get_path_status(name, path):
    current_path = Path(path)
    if not current_path.exists():
        return {
            "name": name,
            "status": "error",
            "summary": "Каталог не найден",
            "details": str(current_path),
        }

    file_count, total_size = _get_directory_metrics(current_path)
    free_space = shutil.disk_usage(current_path).free
    return {
        "name": name,
        "status": "ok",
        "summary": f"{file_count} файлов · {_format_bytes(total_size)}",
        "details": f"{current_path} · свободно {_format_bytes(free_space)}",
    }


def _get_backup_status():
    backup_root_raw = getattr(settings, "SYSTEM_STATUS_BACKUP_ROOT", "").strip()
    if not backup_root_raw:
        return {
            "name": "Резервные копии",
            "status": "info",
            "summary": "Каталог бэкапов не настроен",
            "details": "SYSTEM_STATUS_BACKUP_ROOT не задан",
        }

    backup_root = Path(backup_root_raw)
    if not backup_root.exists():
        return {
            "name": "Резервные копии",
            "status": "warning",
            "summary": "Каталог бэкапов не найден",
            "details": str(backup_root),
        }

    candidates = [item for item in backup_root.iterdir() if item.is_dir()]
    if not candidates:
        return {
            "name": "Резервные копии",
            "status": "warning",
            "summary": "Бэкапы не найдены",
            "details": str(backup_root),
        }

    latest_backup = max(candidates, key=lambda item: item.stat().st_mtime)
    latest_modified = timezone.localtime(
        datetime.fromtimestamp(latest_backup.stat().st_mtime, tz=timezone.get_current_timezone())
    ).strftime("%d.%m.%Y %H:%M")
    return {
        "name": "Резервные копии",
        "status": "ok",
        "summary": f"Последний бэкап: {latest_backup.name}",
        "details": f"{latest_modified} · {latest_backup}",
    }


def _build_system_status_snapshot():
    checks = [
        _get_database_status(),
        _get_email_status(),
        _get_path_status("Media", settings.MEDIA_ROOT),
        _get_path_status("Логи", settings.LOG_DIR if hasattr(settings, "LOG_DIR") else Path(settings.BASE_DIR) / "logs"),
        _get_backup_status(),
    ]
    overall_status = "ok"
    if any(check["status"] == "error" for check in checks):
        overall_status = "error"
    elif any(check["status"] == "warning" for check in checks):
        overall_status = "warning"

    return {
        "generated_at": timezone.localtime().strftime("%d.%m.%Y %H:%M:%S"),
        "overall_status": overall_status,
        "checks": checks,
    }


def _reset_ticket_attention_notifications(ticket):
    ticket.attention_warning_notified_at = None
    ticket.attention_danger_notified_at = None


def _get_ticket_attention_state(ticket, now=None):
    now = now or timezone.now()
    warning_window_hours = max(
        1,
        int(getattr(settings, "TICKET_ATTENTION_WARNING_HOURS", 24)),
    )
    warning_deadline = now + timedelta(hours=warning_window_hours)

    if ticket.status == Ticket.Status.CLOSED:
        return {
            "level": "success",
            "row_class": "table-success",
            "summary": "Закрытая заявка",
            "details": [],
        }

    level = None
    details = []

    if ticket.deadline:
        if ticket.deadline <= now:
            level = "danger"
            details.append("Срок выполнения просрочен")
        elif ticket.deadline <= warning_deadline:
            level = "warning"
            details.append("Срок выполнения скоро истекает")

    if ticket.criticalness == Ticket.Kinds.CRITICAL:
        level = "danger"
        details.append("Критичность: критичная")
    elif ticket.criticalness == Ticket.Kinds.HIGH and level != "danger":
        level = "warning"
        details.append("Критичность: высокая")

    if level == "danger":
        summary = "Заявка требует срочного внимания"
        row_class = "table-danger"
    elif level == "warning":
        summary = "Заявка требует внимания"
        row_class = "table-warning"
    else:
        summary = ""
        row_class = ""

    return {
        "level": level or "default",
        "row_class": row_class,
        "summary": summary,
        "details": details,
    }


def _maybe_send_ticket_attention_notification(ticket, now=None):
    attention = _get_ticket_attention_state(ticket, now=now)
    level = attention["level"]

    if level not in {"warning", "danger"}:
        return False

    notified_at_field = (
        "attention_danger_notified_at"
        if level == "danger"
        else "attention_warning_notified_at"
    )
    if getattr(ticket, notified_at_field):
        return False

    recipients = []
    if ticket.technician is not None:
        recipients.append(ticket.technician)
    recipients.extend(ticket.additional_executors.all())
    recipients = _unique_users(*recipients)
    if not recipients:
        return False

    title = (
        f"Заявка №{ticket.pk} требует срочного внимания"
        if level == "danger"
        else f"Заявка №{ticket.pk} требует внимания"
    )
    body = (
        f'По заявке №{ticket.pk} "{ticket.title}" требуется обратить внимание на '
        f'{", ".join(attention["details"]).lower()}.'
    )
    email_body = _build_ticket_email_body(
        ticket,
        attention["summary"],
        details=attention["details"],
    )

    _notify_ticket_users(
        title,
        body,
        *recipients,
        ticket=ticket,
        level=(
            "danger"
            if level == "danger"
            else Notification.Levels.WARNING
        ),
        email_body=email_body,
    )

    setattr(ticket, notified_at_field, now or timezone.now())
    ticket.save(update_fields=[notified_at_field])
    return True


def _append_system_message(ticket, message, event_type=None, event_meta=None):
    chat = list(ticket.chat or [])
    entry = {
        "author": "Уведомление системы",
        "message": message,
        "datetime": _current_timestamp(),
    }
    if event_type:
        entry["event_type"] = event_type
    if event_meta:
        entry["event_meta"] = event_meta
    chat.append(entry)
    ticket.chat = chat


def _build_document_payload(document):
    document_name = os.path.basename(document.file.name)
    return {
        "document_id": document.pk,
        "document_name": document_name,
        "document_deleted": False,
    }


def _append_document_message(ticket, user, documents):
    chat = list(ticket.chat or [])
    document_payload = [_build_document_payload(document) for document in documents]
    chat.append(
        {
            "author": _get_user_display_name(user),
            "message": "прикрепил новые документы" if len(document_payload) > 1 else "прикрепил новый документ",
            "datetime": _current_timestamp(),
            "documents": document_payload,
        }
    )
    ticket.chat = chat


def _build_movement_events(ticket):
    created_time = ticket.published.strftime("%d.%m.%Y %H:%M") if ticket.published else ""
    events = [
        {
            "title": "Создан",
            "subtitle": ticket.title,
            "time": created_time,
            "tooltip": "\n".join(
                part
                for part in [
                    f"Время: {created_time}" if created_time else "",
                    f"Автор: {_get_user_display_name(ticket.created_by)}" if ticket.created_by else "",
                ]
                if part
            ),
        }
    ]

    for entry in ticket.chat or []:
        if not isinstance(entry, dict):
            continue

        event_type = entry.get("event_type")
        event_meta = entry.get("event_meta") or {}
        event_time = entry.get("datetime", "")

        if event_type == "assignment":
            previous_assignee = event_meta.get("previous_assignee", "")
            assignee = event_meta.get("assignee", "")
            actor = event_meta.get("actor", "")
            events.append(
                {
                    "title": "Передана",
                    "subtitle": assignee,
                    "time": event_time,
                    "tooltip": "\n".join(
                        part
                        for part in [
                            f"Время: {event_time}" if event_time else "",
                            f"Кем передана: {actor}" if actor else "",
                            f"От кого: {previous_assignee}" if previous_assignee else "",
                            f"Кому: {assignee}" if assignee else "",
                        ]
                        if part
                    ),
                }
            )
        elif event_type == "progress":
            progress = event_meta.get("progress", "")
            actor = event_meta.get("actor", "")
            events.append(
                {
                    "title": "Изменён статус",
                    "subtitle": progress,
                    "time": event_time,
                    "tooltip": "\n".join(
                        part
                        for part in [
                            f"Время: {event_time}" if event_time else "",
                            f"Новый статус: {progress}" if progress else "",
                            f"Кем изменён: {actor}" if actor else "",
                        ]
                        if part
                    ),
                }
            )
        elif event_type == "co_assignment":
            added_assignees = event_meta.get("added_assignees", "")
            removed_assignees = event_meta.get("removed_assignees", "")
            actor = event_meta.get("actor", "")
            subtitle_parts = [part for part in [added_assignees, removed_assignees] if part]
            events.append(
                {
                    "title": "Изменены соисполнители",
                    "subtitle": " / ".join(subtitle_parts),
                    "time": event_time,
                    "tooltip": "\n".join(
                        part
                        for part in [
                            f"Время: {event_time}" if event_time else "",
                            f"Кем изменены: {actor}" if actor else "",
                            f"Добавлены: {added_assignees}" if added_assignees else "",
                            f"Сняты: {removed_assignees}" if removed_assignees else "",
                        ]
                        if part
                    ),
                }
            )
        elif event_type == "closed":
            actor = event_meta.get("actor", "")
            events.append(
                {
                    "title": "Закрыта",
                    "subtitle": "",
                    "time": event_time,
                    "tooltip": "\n".join(
                        part
                        for part in [
                            f"Время: {event_time}" if event_time else "",
                            f"Кем закрыта: {actor}" if actor else "",
                        ]
                        if part
                    ),
                }
            )
        elif event_type == "reopened":
            actor = event_meta.get("actor", "")
            events.append(
                {
                    "title": "Открыта повторно",
                    "subtitle": "",
                    "time": event_time,
                    "tooltip": "\n".join(
                        part
                        for part in [
                            f"Время: {event_time}" if event_time else "",
                            f"Кем открыта: {actor}" if actor else "",
                        ]
                        if part
                    ),
                }
            )

    return events


class TicketCreateView(LoginRequiredMixin, CreateView):
    template_name = "desk/create.html"
    form_class = TicketForm
    success_url = reverse_lazy("index")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        technician_users = get_assignable_technicians()
        default_technician = get_default_technician_user()
        selected_technician_id = (
            context["form"]["technician"].value()
            or context["form"].initial.get("technician")
            or (default_technician.pk if default_technician else "")
        )
        selected_technician = None
        if selected_technician_id:
            try:
                selected_technician = technician_users.get(pk=selected_technician_id)
            except (User.DoesNotExist, ValueError, TypeError):
                selected_technician = None

        context["max_upload_size_mb"] = getattr(settings, "MAX_UPLOAD_SIZE_MB", 200)
        context["technician_users"] = technician_users
        context["selected_technician_id"] = selected_technician.pk if selected_technician else ""
        context["selected_technician_name"] = _get_user_display_name(selected_technician)
        context["create_submission_token"] = uuid.uuid4().hex
        return context

    def form_valid(self, form):
        documents = self.request.FILES.getlist("documents")
        validation_error = _validate_uploaded_documents(documents)
        if validation_error:
            form.add_error(None, validation_error)
            messages.error(self.request, validation_error)
            return self.form_invalid(form)

        submission_token = (self.request.POST.get("submission_token") or "").strip() or uuid.uuid4().hex
        form.instance.created_by = self.request.user
        form.instance.submission_token = submission_token

        try:
            with transaction.atomic():
                response = super().form_valid(form)
        except IntegrityError:
            existing_ticket = Ticket.objects.filter(
                submission_token=submission_token,
                created_by=self.request.user,
            ).first()
            if existing_ticket is None:
                raise

            messages.info(self.request, f"Заявка №{existing_ticket.pk} уже была создана.")
            return redirect("ticket", ticket_id=existing_ticket.pk)

        for document in documents:
            if document:
                Document.objects.create(ticket=form.instance, file=document)

        assigned_technician = form.instance.technician or get_default_technician_user()
        _add_ticket_participants(form.instance, self.request.user, assigned_technician)
        _notify_ticket_users(
            "Вам назначена новая заявка",
            f'Поступила заявка с темой "{form.cleaned_data["title"]}".',
            assigned_technician,
            ticket=form.instance,
            exclude_user=self.request.user,
            level=Notification.Levels.SUCCESS,
            email_body=_build_ticket_email_body(
                form.instance,
                "Вам назначена новая заявка",
                actor=_get_user_display_name(self.request.user),
                details=[
                    f'Тема заявки: "{form.cleaned_data["title"]}"',
                    f"Критичность: {form.instance.criticalness}",
                    f"Содержание: {rich_text_to_plain_text(form.instance.content)}",
                ],
            ),
        )
        _maybe_send_ticket_attention_notification(form.instance)
        return response


class MyLogoutView(LogoutView):
    next_page = reverse_lazy("login")

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _create_auth_event(
                AuthEvent.EventTypes.LOGOUT,
                request=request,
                user=request.user,
                username=_get_user_display_name(request.user),
            )
        return super().post(request, *args, **kwargs)


@never_cache
def Login_View(request):
    error_message = None
    if request.method == "POST":
        submitted_identity = (
            request.POST.get("user_profile")
            or request.POST.get("username_display")
            or ""
        )
        if _is_login_rate_limited(request, submitted_identity):
            form = LoginForm(request.POST)
            error_message = "Слишком много попыток входа. Повторите позже."
            _create_auth_event(
                AuthEvent.EventTypes.LOGIN_FAILURE,
                request=request,
                username=submitted_identity,
                metadata={"reason": "rate_limited", "blocked": True},
            )
            return render(request, "desk/login.html", {"form": form, "error_message": error_message})

        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["resolved_username"]
            password = form.cleaned_data["password"]
            user = authenticate(request, username=username, password=password)
            if user is not None:
                _reset_login_rate_limit(request, submitted_identity)
                login(request, user)
                _create_auth_event(
                    AuthEvent.EventTypes.LOGIN_SUCCESS,
                    request=request,
                    user=user,
                    username=username,
                )
                return redirect("index")
            _register_login_failure(
                request,
                submitted_identity,
                username=username,
            )
            error_message = "Неверный логин или пароль"
        else:
            _register_login_failure(
                request,
                submitted_identity,
                username=request.POST.get("username_display") or request.POST.get("user_profile") or "",
                reason="invalid_form",
            )
    else:
        form = LoginForm()
    return render(request, "desk/login.html", {"form": form, "error_message": error_message})


@login_required
def profile_settings(request):
    profile = _get_user_profile(request.user)

    if request.method == "POST":
        if "save-profile" in request.POST:
            profile_settings_form = ProfileSettingsForm(
                request.POST,
                instance=profile,
                user=request.user,
            )
            password_update_form = PasswordUpdateForm(request.user)
            if profile_settings_form.is_valid():
                profile_settings_form.save()
                messages.success(request, "Профиль обновлён.")
                return redirect(_get_safe_redirect_target(request))
        elif "change-password" in request.POST:
            profile_settings_form = ProfileSettingsForm(instance=profile, user=request.user)
            password_update_form = PasswordUpdateForm(request.user, request.POST)
            if password_update_form.is_valid():
                user = password_update_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Пароль обновлён.")
                return redirect(_get_safe_redirect_target(request))
        else:
            profile_settings_form = ProfileSettingsForm(instance=profile, user=request.user)
            password_update_form = PasswordUpdateForm(request.user)
    else:
        profile_settings_form = ProfileSettingsForm(instance=profile, user=request.user)
        password_update_form = PasswordUpdateForm(request.user)

    return render(
        request,
        "desk/profile_settings.html",
        {
            "profile_settings_form": profile_settings_form,
            "password_update_form": password_update_form,
        },
    )


@never_cache
@login_required
def index(request):
    if _is_admin(request.user):
        tickets = Ticket.objects.all()
    elif _is_workflow_executor(request.user):
        tickets = Ticket.objects.filter(
            Q(created_by=request.user)
            | Q(technician=request.user)
            | Q(additional_executors=request.user)
            | Q(participants=request.user)
        ).distinct()
    else:
        tickets = Ticket.objects.filter(created_by=request.user)

    title = request.GET.get("title")
    created_by = request.GET.get("created_by")
    criticalness = request.GET.get("criticalness")
    technician = request.GET.get("technician")
    status = request.GET.get("status")
    progress = request.GET.get("progress")
    published_start = request.GET.get("published_start")
    published_end = request.GET.get("published_end")

    if title:
        tickets = tickets.filter(title__icontains=title)
    if created_by:
        tickets = tickets.filter(created_by__profile__verbose_name__icontains=created_by)
    if criticalness:
        try:
            tickets = tickets.filter(criticalness=Ticket.Kinds[criticalness])
        except KeyError:
            pass
    if technician:
        tickets = tickets.filter(technician__profile__verbose_name__icontains=technician)
    if status:
        if status == "any":
            tickets = tickets.filter(status__in=Ticket.Status.values)
        else:
            try:
                tickets = tickets.filter(status=Ticket.Status[status])
            except KeyError:
                pass
    if progress:
        try:
            tickets = tickets.filter(progress=Ticket.Progres[progress])
        except KeyError:
            pass
    if published_start:
        tickets = tickets.filter(published__gte=published_start)
    if published_end:
        tickets = tickets.filter(published__lte=published_end)
    if not status:
        tickets = tickets.filter(status=Ticket.Status.OPENED)

    ticket_list = list(
        tickets.select_related("created_by", "technician").prefetch_related("additional_executors")
    )
    current_time = timezone.now()
    for ticket in ticket_list:
        attention = _get_ticket_attention_state(ticket, now=current_time)
        ticket.attention_row_class = attention["row_class"]
        ticket.attention_hint = "; ".join(attention["details"]) or attention["summary"]
        _maybe_send_ticket_attention_notification(ticket, now=current_time)

    paginator = Paginator(ticket_list, 18)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    preserved_query = request.GET.copy()
    preserved_query.pop("page", None)
    pagination_query = preserved_query.urlencode()

    return render(
        request,
        "desk/index.html",
        {
            "tickets": page_obj.object_list,
            "page_obj": page_obj,
            "is_paginated": page_obj.paginator.num_pages > 1,
            "pagination_query": pagination_query,
            "technician_users": get_assignable_technicians(),
            "status_choices": [
                ("CLOSED", "Закрыта"),
                ("any", "Любой"),
            ],
            "progress_choices": [
                ("IMPOSSIBLE", "Невозможно выполнить"),
                ("ACCEPTED", "Принято к рассмотрению"),
                ("AGREED", "Согласовано"),
                ("TODO", "К исполнению"),
                ("INPROGRESS", "В стадии решения"),
                ("DECIDED", "Успешно решено"),
            ],
            "title": title,
            "created_by": created_by,
            "criticalness": criticalness,
            "technician": technician,
            "status": status,
            "progress": progress,
            "published_start": published_start,
            "published_end": published_end,
            "max_upload_size_mb": getattr(settings, "MAX_UPLOAD_SIZE_MB", 200),
        },
    )


def _get_statistics_queryset(user):
    if _is_admin(user):
        return Ticket.objects.all()
    return Ticket.objects.filter(
        Q(created_by=user)
        | Q(technician=user)
        | Q(additional_executors=user)
        | Q(participants=user)
    ).distinct()


def _build_choice_stats(queryset, field_name, choices, color_map):
    counter = Counter(queryset.values_list(field_name, flat=True))
    total = sum(counter.values()) or 1
    max_count = max(counter.values(), default=0)
    items = []
    for value, label in choices:
        count = counter.get(value, 0)
        items.append(
            {
                "value": value,
                "label": label,
                "count": count,
                "percent": round(count * 100 / total, 1) if count else 0,
                "fill_percent": max(10, round(count * 100 / max_count)) if max_count and count else 8,
                "color": color_map.get(value, "#6c757d"),
            }
        )
    return items


def _build_created_timeline_stats(queryset, start_date, end_date):
    days_span = max(1, (end_date - start_date).days + 1)
    timestamps = [
        timezone.localtime(value).date()
        for value in queryset.values_list("created_at", flat=True)
        if value and start_date <= timezone.localtime(value).date() <= end_date
    ]

    if days_span <= 31:
        mode = "day"
        buckets = [start_date + timedelta(days=offset) for offset in range(days_span)]
        counter = Counter(timestamps)

        def label_for(bucket):
            return bucket.strftime("%d.%m")

        def title_for(bucket):
            return bucket.strftime("%d.%m.%Y")

    elif days_span <= 180:
        mode = "week"
        week_starts = []
        current = start_date - timedelta(days=start_date.weekday())
        while current <= end_date:
            week_starts.append(current)
            current += timedelta(days=7)
        buckets = week_starts
        counter = Counter()
        for value in timestamps:
            bucket = value - timedelta(days=value.weekday())
            counter[bucket] += 1

        def label_for(bucket):
            return f"{bucket.strftime('%d.%m')}"

        def title_for(bucket):
            week_end = min(bucket + timedelta(days=6), end_date)
            return f"{bucket.strftime('%d.%m.%Y')} - {week_end.strftime('%d.%m.%Y')}"

    else:
        mode = "month"
        buckets = []
        current = start_date.replace(day=1)
        while current <= end_date:
            buckets.append(current)
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1, day=1)
            else:
                current = current.replace(month=current.month + 1, day=1)
        counter = Counter()
        for value in timestamps:
            counter[value.replace(day=1)] += 1

        def label_for(bucket):
            return bucket.strftime("%m.%Y")

        def title_for(bucket):
            return bucket.strftime("%m.%Y")

    max_count = max(counter.values(), default=0)
    items = []
    for bucket in buckets:
        count = counter.get(bucket, 0)
        items.append(
            {
                "bucket": bucket,
                "label": label_for(bucket),
                "title": title_for(bucket),
                "count": count,
                "height_percent": max(10, round(count * 100 / max_count)) if max_count and count else 8,
            }
        )
    return items, mode


def _build_status_donut(status_items):
    total = sum(item["count"] for item in status_items)
    if not total:
        return "conic-gradient(#e9ecef 0deg 360deg)"

    current = 0
    slices = []
    for item in status_items:
        if not item["count"]:
            continue
        angle = item["count"] * 360 / total
        slices.append(f"{item['color']} {current:.2f}deg {current + angle:.2f}deg")
        current += angle
    if current < 360:
        slices.append(f"#e9ecef {current:.2f}deg 360deg")
    return f"conic-gradient({', '.join(slices)})"


def _build_executor_statistics(queryset):
    technicians = list(get_assignable_technicians().select_related("profile"))
    items = []
    for technician in technicians:
        main_queryset = queryset.filter(technician=technician)
        co_queryset = queryset.filter(additional_executors=technician).exclude(technician=technician)
        visible_total = main_queryset.count() + co_queryset.count()
        if not visible_total:
            continue
        open_count = main_queryset.filter(status=Ticket.Status.OPENED).count() + co_queryset.filter(
            status=Ticket.Status.OPENED
        ).count()
        closed_count = main_queryset.filter(status=Ticket.Status.CLOSED).count() + co_queryset.filter(
            status=Ticket.Status.CLOSED
        ).count()
        overdue_count = (
            main_queryset.filter(status=Ticket.Status.OPENED, deadline__lt=timezone.now()).count()
            + co_queryset.filter(status=Ticket.Status.OPENED, deadline__lt=timezone.now()).count()
        )
        items.append(
            {
                "name": _get_user_display_name(technician),
                "main_count": main_queryset.count(),
                "co_count": co_queryset.count(),
                "total_count": visible_total,
                "open_count": open_count,
                "closed_count": closed_count,
                "overdue_count": overdue_count,
            }
        )
    return sorted(items, key=lambda item: (-item["total_count"], item["name"]))


def _parse_statistics_dates(request):
    date_format = "%Y-%m-%d"
    today = timezone.localdate()
    period = request.GET.get("period", "30")
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()

    date_from = None
    date_to = None
    if date_from_raw:
        try:
            date_from = timezone.datetime.strptime(date_from_raw, date_format).date()
        except ValueError:
            date_from = None
    if date_to_raw:
        try:
            date_to = timezone.datetime.strptime(date_to_raw, date_format).date()
        except ValueError:
            date_to = None

    if period != "all" and (date_from is None or date_to is None):
        try:
            period_days = int(period)
        except ValueError:
            period = "30"
            period_days = 30
        date_to = date_to or today
        date_from = date_from or (date_to - timedelta(days=period_days - 1))
    elif period == "all":
        if date_from is not None and date_to is None:
            date_to = today
        elif date_to is not None and date_from is None:
            date_from = date_to

    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    return period, date_from, date_to


@never_cache
@login_required
def statistics_report(request):
    if not _can_view_statistics(request.user):
        messages.error(request, "Статистика доступна только администраторам и исполнителям.")
        return redirect("index")

    base_queryset = _get_statistics_queryset(request.user)
    can_filter_statistics_technician = _is_admin(request.user)
    technician_users = (
        list(get_assignable_technicians().select_related("profile"))
        if can_filter_statistics_technician
        else []
    )
    technician_filter = (
        (request.GET.get("technician") or "").strip()
        if can_filter_statistics_technician
        else ""
    )
    period = request.GET.get("period", "30")
    period_options = [
        {"value": "7", "label": "7 дней"},
        {"value": "30", "label": "30 дней"},
        {"value": "90", "label": "90 дней"},
        {"value": "all", "label": "За всё время"},
    ]
    period, date_from, date_to = _parse_statistics_dates(request)

    filtered_queryset = base_queryset
    selected_technician = None
    if technician_filter:
        selected_technician = next(
            (user for user in technician_users if str(user.pk) == technician_filter),
            None,
        )
        if selected_technician is not None:
            filtered_queryset = filtered_queryset.filter(
                Q(technician=selected_technician) | Q(additional_executors=selected_technician)
            ).distinct()

    if date_from is None or date_to is None:
        bounds = filtered_queryset.aggregate(
            min_created=Min("created_at"),
            min_published=Min("published"),
            max_created=Max("created_at"),
            max_published=Max("published"),
        )
        start_candidates = [
            timezone.localdate(value)
            for value in (bounds["min_created"], bounds["min_published"])
            if value is not None
        ]
        end_candidates = [
            timezone.localdate(value)
            for value in (bounds["max_created"], bounds["max_published"])
            if value is not None
        ]
        fallback_start = min(start_candidates, default=timezone.localdate())
        fallback_end = max(end_candidates, default=timezone.localdate())
        date_from = date_from or fallback_start
        date_to = date_to or fallback_end

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    activity_queryset = filtered_queryset.filter(
        published__date__gte=date_from,
        published__date__lte=date_to,
    ).distinct()
    created_queryset = filtered_queryset.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    ).distinct()
    now = timezone.now()
    total_count = activity_queryset.count()
    created_count = created_queryset.count()
    open_count = activity_queryset.filter(status=Ticket.Status.OPENED).count()
    closed_count = activity_queryset.filter(status=Ticket.Status.CLOSED).count()
    overdue_count = activity_queryset.filter(status=Ticket.Status.OPENED, deadline__lt=now).count()
    critical_count = activity_queryset.filter(criticalness=Ticket.Kinds.CRITICAL).count()
    high_priority_count = activity_queryset.filter(
        Q(criticalness=Ticket.Kinds.HIGH) | Q(criticalness=Ticket.Kinds.CRITICAL)
    ).count()

    status_items = _build_choice_stats(
        activity_queryset,
        "status",
        Ticket.Status.choices,
        {
            Ticket.Status.OPENED: "#0d6efd",
            Ticket.Status.CLOSED: "#198754",
        },
    )
    progress_items = _build_choice_stats(
        activity_queryset,
        "progress",
        Ticket.Progres.choices,
        {
            Ticket.Progres.ACCEPTED: "#0d6efd",
            Ticket.Progres.AGREED: "#6f42c1",
            Ticket.Progres.TODO: "#fd7e14",
            Ticket.Progres.INPROGRESS: "#ffc107",
            Ticket.Progres.DECIDED: "#198754",
            Ticket.Progres.IMPOSSIBLE: "#dc3545",
        },
    )
    criticality_items = _build_choice_stats(
        activity_queryset,
        "criticalness",
        Ticket.Kinds.choices,
        {
            Ticket.Kinds.LOW: "#20c997",
            Ticket.Kinds.MEDIUM: "#0dcaf0",
            Ticket.Kinds.HIGH: "#ffc107",
            Ticket.Kinds.CRITICAL: "#dc3545",
        },
    )

    daily_items, timeline_mode = _build_created_timeline_stats(created_queryset, date_from, date_to)
    executor_items = _build_executor_statistics(activity_queryset) if _is_admin(request.user) else []
    manual_dates_requested = any(
        (request.GET.get(field_name) or "").strip()
        for field_name in ("date_from", "date_to")
    )
    display_period = period
    if manual_dates_requested:
        matched_period = None
        if date_to == timezone.localdate():
            range_days = (date_to - date_from).days + 1
            if range_days in {7, 30, 90}:
                matched_period = str(range_days)
        display_period = matched_period or "custom"

    context = {
        "period": display_period,
        "period_options": period_options,
        "stats_total_count": total_count,
        "stats_created_count": created_count,
        "stats_open_count": open_count,
        "stats_closed_count": closed_count,
        "stats_overdue_count": overdue_count,
        "stats_critical_count": critical_count,
        "stats_high_priority_count": high_priority_count,
        "status_items": status_items,
        "progress_items": progress_items,
        "criticality_items": criticality_items,
        "status_donut_style": _build_status_donut(status_items),
        "daily_items": daily_items,
        "timeline_mode": timeline_mode,
        "executor_items": executor_items,
        "statistics_scope_label": "Все заявки" if _is_admin(request.user) else "Доступные вам заявки",
        "statistics_period_label": f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}",
        "date_from": date_from.strftime("%Y-%m-%d"),
        "date_to": date_to.strftime("%Y-%m-%d"),
        "technician_users": technician_users,
        "can_filter_statistics_technician": can_filter_statistics_technician,
        "selected_statistics_technician": selected_technician,
    }
    return render(request, "desk/statistics.html", context)


@never_cache
def healthcheck(request):
    snapshot = _build_system_status_snapshot()
    http_status = 200 if snapshot["overall_status"] in {"ok", "warning"} else 503
    return JsonResponse(snapshot, status=http_status)


@never_cache
@login_required
def system_status(request):
    if not _can_view_system_status(request.user):
        messages.error(request, "Страница состояния системы доступна только техперсоналу.")
        return redirect("index")

    context = {
        "system_status": _build_system_status_snapshot(),
        "recent_auth_events": AuthEvent.objects.select_related("user").all()[:20],
        "login_rate_limit_attempts": getattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 5),
        "login_rate_limit_window_seconds": getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300),
        "login_rate_limit_block_seconds": getattr(settings, "LOGIN_RATE_LIMIT_BLOCK_SECONDS", 900),
    }
    return render(request, "desk/system_status.html", context)


@login_required
def ticket(request, **kwargs):
    current_ticket = get_object_or_404(Ticket, pk=kwargs.get("ticket_id"))
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Страница не найдена")

    can_manage_ticket_workflow = _can_manage_ticket_workflow(request.user, current_ticket)
    users = get_assignable_technicians()
    additional_executors = list(current_ticket.additional_executors.all())

    context = {
        "current_ticket": current_ticket,
        "can_assign_technician": can_manage_ticket_workflow,
        "can_edit_progress": can_manage_ticket_workflow,
        "can_manage_multiple_executors": can_manage_ticket_workflow and _is_workflow_executor(request.user),
        "can_edit_ticket_details": _can_edit_ticket_details(request.user, current_ticket),
        "can_change_ticket_status": _can_change_ticket_status(request.user, current_ticket),
        "can_manage_documents": _can_manage_ticket_documents(request.user, current_ticket),
        "movement_events": _build_movement_events(current_ticket),
        "users": users,
        "selected_additional_executor_ids": [user.pk for user in additional_executors],
        "max_upload_size_mb": getattr(settings, "MAX_UPLOAD_SIZE_MB", 200),
    }
    return render(request, "desk/ticket.html", context)


@login_required
def ticket_edit(request, ticket_id):
    current_ticket = get_object_or_404(Ticket, pk=ticket_id)
    if not _can_edit_ticket_details(request.user, current_ticket):
        raise Http404("Страница не найдена")

    if request.method == "POST":
        original_title = current_ticket.title
        original_content = current_ticket.content
        original_content_text = rich_text_to_audit_text(original_content)
        original_criticalness = current_ticket.criticalness
        original_deadline = current_ticket.deadline
        form = TicketEditForm(
            request.POST,
            instance=current_ticket,
            can_edit_deadline=_is_admin(request.user),
        )
        if form.is_valid():
            actor_name = _get_user_display_name(request.user)
            updated_ticket = form.save()

            change_messages = []
            if original_title != updated_ticket.title:
                change_messages.append(
                    f'Тема заявки изменена с "{original_title}" на "{updated_ticket.title}" пользователем {actor_name}'
                )
            if original_criticalness != updated_ticket.criticalness:
                change_messages.append(
                    f'Критичность заявки изменена с "{original_criticalness}" на "{updated_ticket.criticalness}" пользователем {actor_name}'
                )
            if original_content != updated_ticket.content:
                updated_content_text = rich_text_to_audit_text(updated_ticket.content)
                change_messages.append(
                    f'Содержание заявки изменено пользователем {actor_name} с "{original_content_text}" на "{updated_content_text}"'
                )
            if original_deadline != updated_ticket.deadline:
                previous_deadline = (
                    timezone.localtime(original_deadline).strftime("%d.%m.%Y %H:%M")
                    if original_deadline
                    else "не указан"
                )
                next_deadline = (
                    timezone.localtime(updated_ticket.deadline).strftime("%d.%m.%Y %H:%M")
                    if updated_ticket.deadline
                    else "не указан"
                )
                change_messages.append(
                    f'Срок выполнения изменён с "{previous_deadline}" на "{next_deadline}" пользователем {actor_name}'
                )

            if (
                original_criticalness != updated_ticket.criticalness
                or original_deadline != updated_ticket.deadline
            ):
                _reset_ticket_attention_notifications(updated_ticket)

            if change_messages:
                for message_text in change_messages:
                    _append_system_message(updated_ticket, message_text)
                updated_ticket.save()
                _add_ticket_participants(
                    updated_ticket,
                    request.user,
                    updated_ticket.created_by,
                    updated_ticket.technician,
                )
                _notify_ticket_users(
                    "Обновление по заявке",
                    f'По заявке №{updated_ticket.id} изменены основные данные.',
                    updated_ticket.created_by,
                    updated_ticket.technician,
                    *updated_ticket.participants.all(),
                    ticket=updated_ticket,
                    exclude_user=request.user,
                    level=Notification.Levels.INFO,
                    email_body=_build_ticket_email_body(
                        updated_ticket,
                        "Изменены основные данные заявки",
                        actor=actor_name,
                        details=change_messages,
                    ),
                )
                messages.success(request, "Заявка обновлена.")
                _maybe_send_ticket_attention_notification(updated_ticket)
            else:
                messages.info(request, "Изменений не найдено.")

            return redirect("ticket", ticket_id=updated_ticket.pk)
    else:
        form = TicketEditForm(instance=current_ticket, can_edit_deadline=_is_admin(request.user))

    return render(
        request,
        "desk/ticket_edit.html",
        {
            "form": form,
            "current_ticket": current_ticket,
        },
    )


@login_required
def download_document(request, document_id):
    document = get_object_or_404(Document.objects.select_related("ticket"), pk=document_id)
    if not _can_view_ticket(request.user, document.ticket):
        raise Http404("Страница не найдена")

    if not document.file:
        raise Http404("Файл не найден")

    try:
        file_handle = document.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("Файл не найден") from exc

    filename = os.path.basename(document.file.name)
    guessed_content_type, _ = mimetypes.guess_type(filename)
    is_pdf = (guessed_content_type == "application/pdf") or filename.lower().endswith(".pdf")

    response = FileResponse(
        file_handle,
        as_attachment=not is_pdf,
        filename=filename,
        content_type="application/pdf" if is_pdf else "application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def ticket_document_download(request, ticket_id):
    current_ticket = get_object_or_404(Ticket, pk=ticket_id)
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Page not found")

    if not TICKET_TEMPLATE_PATH.exists():
        raise Http404("Document template not found")

    response = HttpResponse(
        render_ticket_docx(current_ticket),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{build_ticket_docx_filename(current_ticket)}"'
    return response


@login_required
def ticket_document_print(request, ticket_id):
    current_ticket = get_object_or_404(Ticket, pk=ticket_id)
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Page not found")

    response = HttpResponse(
        render_ticket_pdf(current_ticket),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'inline; filename="{build_ticket_pdf_filename(current_ticket)}"'
    return response


@login_required
def notifications_feed(request):
    unread_notifications = list(
        Notification.objects.filter(user=request.user, is_read=False).order_by("created_at")[:20]
    )
    payload, read_ids = _collapse_unread_notifications(unread_notifications)

    if read_ids:
        Notification.objects.filter(pk__in=read_ids).update(is_read=True)

    return JsonResponse({"notifications": payload})


@login_required
def send_message(request, ticket_id):
    current_ticket = get_object_or_404(Ticket, pk=ticket_id)
    if request.method != "POST":
        return redirect("ticket", ticket_id=ticket_id)
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Страница не найдена")

    _add_ticket_participants(
        current_ticket,
        request.user,
        current_ticket.created_by,
        current_ticket.technician,
        *current_ticket.additional_executors.all(),
    )

    if "delete-document" in request.POST:
        if not _can_manage_ticket_documents(request.user, current_ticket):
            raise Http404("Страница не найдена")

        document = get_object_or_404(
            Document,
            pk=request.POST.get("document_id"),
            ticket=current_ticket,
        )
        document_name = os.path.basename(document.file.name)

        chat = list(current_ticket.chat or [])
        for entry in chat:
            if entry.get("document_id") == document.pk:
                entry["document_deleted"] = True
                entry["document_url"] = ""
            for nested_document in entry.get("documents", []):
                if nested_document.get("document_id") == document.pk:
                    nested_document["document_deleted"] = True
                    nested_document["document_url"] = ""
        current_ticket.chat = chat
        _append_system_message(
            current_ticket,
            f'Документ "{document_name}" откреплён пользователем {_get_user_display_name(request.user)}',
        )
        current_ticket.save()

        if document.file:
            document.file.delete(save=False)
        document.delete()

        messages.success(request, "Документ удалён из заявки.")
        return redirect("ticket", ticket_id=ticket_id)

    if "save-ticket" in request.POST:
        if not _can_manage_ticket_workflow(request.user, current_ticket):
            raise Http404("Страница не найдена")

        changes = []
        actor_name = _get_user_display_name(request.user)
        previous_technician = current_ticket.technician
        previous_additional_executors = list(current_ticket.additional_executors.all())
        previous_additional_executor_ids = {user.pk for user in previous_additional_executors}
        pending_additional_executors = previous_additional_executors
        specific_notification_users = []
        attention_recipients_changed = False

        technician_id = request.POST.get("technician")
        if technician_id:
            technician = get_object_or_404(
                get_assignable_technicians(),
                pk=technician_id,
            )
            if current_ticket.technician_id != technician.id:
                attention_recipients_changed = True
                previous_assignee = _get_user_display_name(previous_technician)
                current_ticket.technician = technician
                changes.append(
                    (
                        f"Исполнитель заявки изменен на {_get_user_display_name(technician)} пользователем {actor_name}",
                        "assignment",
                        {
                            "actor": actor_name,
                            "previous_assignee": previous_assignee,
                            "assignee": _get_user_display_name(technician),
                        },
                    )
                )
                _add_ticket_participants(
                    current_ticket,
                    request.user,
                    current_ticket.created_by,
                    previous_technician,
                    technician,
                )
                _notify_ticket_users(
                    "Вам назначена заявка",
                    f'Вам назначена заявка №{current_ticket.id} с темой "{current_ticket.title}".',
                    technician,
                    ticket=current_ticket,
                    exclude_user=request.user,
                    level=Notification.Levels.SUCCESS,
                    email_body=_build_ticket_email_body(
                        current_ticket,
                        "Вы назначены основным исполнителем",
                        actor=actor_name,
                        details=[
                            f"Предыдущий исполнитель: {previous_assignee or 'не назначен'}",
                            f"Новый исполнитель: {_get_user_display_name(technician)}",
                        ],
                    ),
                )
                specific_notification_users.append(technician)

        requested_additional_executor_ids = []
        for value in request.POST.getlist("additional_executors"):
            if not value:
                continue
            if value == str(current_ticket.technician_id):
                continue
            if value not in requested_additional_executor_ids:
                requested_additional_executor_ids.append(value)

        selected_additional_executors = list(
            get_assignable_technicians().filter(pk__in=requested_additional_executor_ids)
        )
        selected_additional_executors.sort(
            key=lambda user: requested_additional_executor_ids.index(str(user.pk))
            if str(user.pk) in requested_additional_executor_ids
            else 0
        )
        selected_additional_executor_ids = {user.pk for user in selected_additional_executors}

        if selected_additional_executor_ids != previous_additional_executor_ids:
            attention_recipients_changed = True
            added_executors = [
                user for user in selected_additional_executors if user.pk not in previous_additional_executor_ids
            ]
            removed_executors = [
                user for user in previous_additional_executors if user.pk not in selected_additional_executor_ids
            ]
            pending_additional_executors = selected_additional_executors

            change_parts = []
            event_meta = {"actor": actor_name}
            if added_executors:
                added_names = ", ".join(_get_user_display_name(user) for user in added_executors)
                change_parts.append(f"добавлены соисполнители: {added_names}")
                event_meta["added_assignees"] = added_names
            if removed_executors:
                removed_names = ", ".join(_get_user_display_name(user) for user in removed_executors)
                change_parts.append(f"сняты соисполнители: {removed_names}")
                event_meta["removed_assignees"] = removed_names

            changes.append(
                (
                    f"Соисполнители заявки изменены пользователем {actor_name}: {'; '.join(change_parts)}",
                    "co_assignment",
                    event_meta,
                )
            )

            if added_executors:
                _notify_ticket_users(
                    f"Вас назначили соисполнителем заявки №{current_ticket.id}",
                    f'Вы назначены соисполнителем по заявке №{current_ticket.id} с темой "{current_ticket.title}".',
                    *added_executors,
                    ticket=current_ticket,
                    exclude_user=request.user,
                    level=Notification.Levels.SUCCESS,
                    email_body=_build_ticket_email_body(
                        current_ticket,
                        "Вы назначены соисполнителем",
                        actor=actor_name,
                        details=change_parts,
                    ),
                )
                specific_notification_users.extend(added_executors)

        new_progress = request.POST.get("progress")
        if new_progress in Ticket.Progres.values and current_ticket.progress != new_progress:
            current_ticket.progress = new_progress
            changes.append(
                (
                    f'Статус заявки изменен на "{current_ticket.progress}" пользователем {actor_name}',
                    "progress",
                    {
                        "actor": actor_name,
                        "progress": current_ticket.progress,
                    },
                )
            )

        for message_text, event_type, event_meta in changes:
            _append_system_message(
                current_ticket,
                message_text,
                event_type=event_type,
                event_meta=event_meta,
            )

        if changes:
            if attention_recipients_changed:
                _reset_ticket_attention_notifications(current_ticket)
            current_ticket.save()
            current_ticket.additional_executors.set(pending_additional_executors)
            _add_ticket_participants(
                current_ticket,
                request.user,
                current_ticket.created_by,
                previous_technician,
                current_ticket.technician,
                *previous_additional_executors,
                *pending_additional_executors,
            )
            _notify_ticket_users(
                "Обновление по заявке",
                f'По заявке №{current_ticket.id} появились изменения.',
                current_ticket.created_by,
                current_ticket.technician,
                *pending_additional_executors,
                *current_ticket.participants.all(),
                ticket=current_ticket,
                exclude_user=request.user,
                exclude_users=specific_notification_users,
                level=Notification.Levels.INFO,
                email_body=_build_ticket_email_body(
                    current_ticket,
                    "Изменены параметры маршрутизации заявки",
                    actor=actor_name,
                    details=[message_text for message_text, _, _ in changes],
                ),
            )
            messages.success(request, "Изменения по заявке сохранены.")
            _maybe_send_ticket_attention_notification(current_ticket)
            return redirect("ticket", ticket_id=ticket_id)

        return redirect("ticket", ticket_id=ticket_id)

    if "close-ticket" in request.POST:
        if not _can_change_ticket_status(request.user, current_ticket):
            raise Http404("Страница не найдена")

        current_ticket.status = Ticket.Status.CLOSED
        actor_name = _get_user_display_name(request.user)
        _append_system_message(
            current_ticket,
            f"Заявка закрыта пользователем {actor_name}",
            event_type="closed",
            event_meta={"actor": actor_name},
        )
        current_ticket.save()
        _notify_ticket_users(
            "Заявка закрыта",
            f'Заявка №{current_ticket.id} с темой "{current_ticket.title}" закрыта.',
            current_ticket.created_by,
            current_ticket.technician,
            *current_ticket.participants.all(),
            ticket=current_ticket,
            exclude_user=request.user,
            level=Notification.Levels.WARNING,
            email_body=_build_ticket_email_body(
                current_ticket,
                "Заявка закрыта",
                actor=actor_name,
                details=[f"Заявка закрыта пользователем {actor_name}."],
            ),
        )
        messages.success(request, "Заявка закрыта.")
        return redirect("index")

    if "open-ticket" in request.POST:
        if not _can_change_ticket_status(request.user, current_ticket):
            raise Http404("Страница не найдена")

        current_ticket.status = Ticket.Status.OPENED
        _reset_ticket_attention_notifications(current_ticket)
        actor_name = _get_user_display_name(request.user)
        _append_system_message(
            current_ticket,
            f"Заявка открыта пользователем {actor_name}",
            event_type="reopened",
            event_meta={"actor": actor_name},
        )
        current_ticket.save()
        _maybe_send_ticket_attention_notification(current_ticket)
        _notify_ticket_users(
            "Заявка открыта повторно",
            f'Заявка №{current_ticket.id} с темой "{current_ticket.title}" открыта повторно.',
            current_ticket.created_by,
            current_ticket.technician,
            *current_ticket.participants.all(),
            ticket=current_ticket,
            exclude_user=request.user,
            level=Notification.Levels.WARNING,
            email_body=_build_ticket_email_body(
                current_ticket,
                "Заявка открыта повторно",
                actor=actor_name,
                details=[f"Заявка повторно открыта пользователем {actor_name}."],
            ),
        )
        messages.success(request, "Заявка открыта повторно.")
        return redirect("index")

    uploaded_documents = request.FILES.getlist("chat_documents")
    validation_error = _validate_uploaded_documents(uploaded_documents)
    if validation_error:
        messages.error(request, validation_error)
        return redirect("ticket", ticket_id=ticket_id)

    if uploaded_documents:
        created_documents = [
            Document.objects.create(ticket=current_ticket, file=uploaded_document)
            for uploaded_document in uploaded_documents
        ]
        _append_document_message(current_ticket, request.user, created_documents)

    raw_message_text = request.POST.get("message_text")
    message_html = sanitize_rich_text(raw_message_text)
    message_text = rich_text_to_plain_text(message_html)
    if rich_text_has_text(message_html):
        chat = list(current_ticket.chat or [])
        chat.append(
            {
                "author": _get_user_display_name(request.user),
                "message": message_text,
                "message_html": message_html,
                "datetime": _current_timestamp(),
            }
        )
        current_ticket.chat = chat

    if uploaded_documents or rich_text_has_text(message_html):
        current_ticket.save()
        notification_details = []
        if rich_text_has_text(message_html):
            notification_details.append(
                f'Новое сообщение от {_get_user_display_name(request.user)}: "{message_text}"'
            )
        if uploaded_documents:
            uploaded_names = ", ".join(document.file.name.rsplit("/", 1)[-1] for document in created_documents)
            notification_details.append(
                f"Прикреплены документы ({len(created_documents)}): {uploaded_names}"
            )
        _notify_ticket_users(
            "Обновление по заявке",
            f'По заявке №{current_ticket.id} появилось новое сообщение.',
            current_ticket.created_by,
            current_ticket.technician,
            *current_ticket.participants.all(),
            ticket=current_ticket,
            exclude_user=request.user,
            level=Notification.Levels.INFO,
            email_body=_build_ticket_email_body(
                current_ticket,
                "Новое сообщение в чате заявки",
                actor=_get_user_display_name(request.user),
                details=notification_details,
            ),
        )

    return redirect("ticket", ticket_id=ticket_id)
