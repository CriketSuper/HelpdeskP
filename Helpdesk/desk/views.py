import logging
import mimetypes
import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LogoutView
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Q
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
    Document,
    Notification,
    Ticket,
    UserProfile,
    admin_group_name,
    executor_group_name,
    get_assignable_technicians,
    get_default_technician_user,
)
from .rich_text import rich_text_has_text, rich_text_to_plain_text, sanitize_rich_text

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


def _unique_users(*users, exclude_user=None):
    result = []
    seen = set()

    for user in users:
        if user is None:
            continue
        if exclude_user is not None and user.pk == exclude_user.pk:
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
    level=Notification.Levels.INFO,
):
    recipients = _unique_users(*users, exclude_user=exclude_user)
    _create_in_app_notifications(
        title,
        body,
        *recipients,
        ticket=ticket,
        level=level,
    )
    _notify_users(title, body, *recipients)


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


def _is_admin(user):
    return user.is_superuser or user.groups.filter(name=admin_group_name).exists()


def _is_executor(user):
    return user.groups.filter(name=executor_group_name).exists()


def _can_view_ticket(user, ticket):
    return (
        _is_admin(user)
        or ticket.created_by == user
        or ticket.technician == user
        or ticket.participants.filter(pk=user.pk).exists()
    )


def _can_manage_ticket_workflow(user, ticket):
    return _is_admin(user) or (_is_executor(user) and ticket.technician == user)


def _can_change_ticket_status(user, ticket):
    return _is_admin(user) or ticket.created_by == user


def _can_edit_ticket_details(user, ticket):
    return _is_admin(user) or ticket.created_by == user


def _can_manage_ticket_documents(user, ticket):
    return _is_admin(user) or ticket.created_by == user or ticket.technician == user


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
        return context

    def form_valid(self, form):
        documents = self.request.FILES.getlist("documents")
        validation_error = _validate_uploaded_documents(documents)
        if validation_error:
            form.add_error(None, validation_error)
            messages.error(self.request, validation_error)
            return self.form_invalid(form)

        form.instance.created_by = self.request.user
        response = super().form_valid(form)

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
        )
        return response


class MyLogoutView(LogoutView):
    next_page = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        previous_page = request.session.pop("previous_page", None)
        return response if previous_page is None else redirect(previous_page)


def Login_View(request):
    error_message = None
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["resolved_username"]
            password = form.cleaned_data["password"]
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect("index")
            error_message = "Неверный логин или пароль"
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
    elif _is_executor(request.user):
        tickets = Ticket.objects.filter(
            Q(created_by=request.user)
            | Q(technician=request.user)
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

    paginator = Paginator(tickets, 18)
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


@login_required
def ticket(request, **kwargs):
    current_ticket = get_object_or_404(Ticket, pk=kwargs.get("ticket_id"))
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Страница не найдена")

    can_manage_ticket_workflow = _can_manage_ticket_workflow(request.user, current_ticket)
    users = get_assignable_technicians()

    context = {
        "current_ticket": current_ticket,
        "can_assign_technician": can_manage_ticket_workflow,
        "can_edit_progress": can_manage_ticket_workflow,
        "can_edit_ticket_details": _can_edit_ticket_details(request.user, current_ticket),
        "can_change_ticket_status": _can_change_ticket_status(request.user, current_ticket),
        "can_manage_documents": _can_manage_ticket_documents(request.user, current_ticket),
        "movement_events": _build_movement_events(current_ticket),
        "users": users,
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
        original_content_text = rich_text_to_plain_text(original_content)
        original_criticalness = current_ticket.criticalness
        form = TicketEditForm(request.POST, instance=current_ticket)
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
                updated_content_text = rich_text_to_plain_text(updated_ticket.content)
                change_messages.append(
                    f'Содержание заявки изменено пользователем {actor_name} с "{original_content_text}" на "{updated_content_text}"'
                )

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
                )
                messages.success(request, "Заявка обновлена.")
            else:
                messages.info(request, "Изменений не найдено.")

            return redirect("ticket", ticket_id=updated_ticket.pk)
    else:
        form = TicketEditForm(instance=current_ticket)

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

        technician_id = request.POST.get("technician")
        if technician_id:
            technician = get_object_or_404(
                get_assignable_technicians(),
                pk=technician_id,
            )
            if current_ticket.technician_id != technician.id:
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
                )

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
            current_ticket.save()
            _add_ticket_participants(
                current_ticket,
                request.user,
                current_ticket.created_by,
                previous_technician,
                current_ticket.technician,
            )
            _notify_ticket_users(
                "Обновление по заявке",
                f'По заявке №{current_ticket.id} появились изменения.',
                current_ticket.created_by,
                current_ticket.technician,
                *current_ticket.participants.all(),
                ticket=current_ticket,
                exclude_user=request.user,
                level=Notification.Levels.INFO,
            )
            messages.success(request, "Изменения по заявке сохранены.")
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
        )
        messages.success(request, "Заявка закрыта.")
        return redirect("index")

    if "open-ticket" in request.POST:
        if not _can_change_ticket_status(request.user, current_ticket):
            raise Http404("Страница не найдена")

        current_ticket.status = Ticket.Status.OPENED
        actor_name = _get_user_display_name(request.user)
        _append_system_message(
            current_ticket,
            f"Заявка открыта пользователем {actor_name}",
            event_type="reopened",
            event_meta={"actor": actor_name},
        )
        current_ticket.save()
        _notify_ticket_users(
            "Заявка открыта повторно",
            f'Заявка №{current_ticket.id} с темой "{current_ticket.title}" открыта повторно.',
            current_ticket.created_by,
            current_ticket.technician,
            *current_ticket.participants.all(),
            ticket=current_ticket,
            exclude_user=request.user,
            level=Notification.Levels.WARNING,
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
        _notify_ticket_users(
            "Обновление по заявке",
            f'По заявке №{current_ticket.id} появилось новое сообщение.',
            current_ticket.created_by,
            current_ticket.technician,
            *current_ticket.participants.all(),
            ticket=current_ticket,
            exclude_user=request.user,
            level=Notification.Levels.INFO,
        )

    return redirect("ticket", ticket_id=ticket_id)
