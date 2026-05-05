import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LogoutView
from django.core.mail import send_mail
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.generic.edit import CreateView

from .forms import LoginForm, PasswordUpdateForm, ProfileSettingsForm, TicketForm
from .models import (
    Document,
    Ticket,
    UserProfile,
    admin_group_name,
    executor_group_name,
    get_default_technician_user,
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
    return _is_admin(user) or ticket.created_by == user or ticket.technician == user


def _can_manage_ticket_workflow(user, ticket):
    return _is_admin(user) or (_is_executor(user) and ticket.technician == user)


def _can_change_ticket_status(user, ticket):
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


def _append_document_message(ticket, user, document):
    chat = list(ticket.chat or [])
    document_name = document.file.name.split("/")[-1]
    chat.append(
        {
            "author": _get_user_display_name(user),
            "message": f'прикрепил новый документ "{document_name}"',
            "datetime": _current_timestamp(),
            "document_id": document.pk,
            "document_name": document_name,
            "document_url": document.file.url,
            "document_deleted": False,
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

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)

        documents = self.request.FILES.getlist("documents")
        document_names = self.request.POST.getlist("document_names")
        for document, name in zip(documents, document_names):
            if document:
                doc = Document(ticket=form.instance, file=document)
                doc.file.name = name
                doc.save()

        assigned_technician = form.instance.technician or get_default_technician_user()
        _notify_users(
            "Поступила новая заявка по административно-хозяйственной службе",
            'Поступила заявка с темой "{}"'.format(form.cleaned_data["title"]),
            assigned_technician,
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
            Q(created_by=request.user) | Q(technician=request.user)
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

    return render(
        request,
        "desk/index.html",
        {
            "tickets": tickets,
            "status_choices": [
                ("CLOSED", "Закрыта"),
                ("any", "Любой"),
            ],
            "progress_choices": [
                ("IMPOSSIBLE", "Невозможно выполнить"),
                ("ACCEPTED", "Принято к рассмотрению"),
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
        },
    )


@login_required
def ticket(request, **kwargs):
    current_ticket = get_object_or_404(Ticket, pk=kwargs.get("ticket_id"))
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Страница не найдена")

    can_manage_ticket_workflow = _can_manage_ticket_workflow(request.user, current_ticket)
    users = User.objects.filter(groups__name=executor_group_name).order_by("id").distinct()

    context = {
        "current_ticket": current_ticket,
        "can_assign_technician": can_manage_ticket_workflow,
        "can_edit_progress": can_manage_ticket_workflow,
        "can_change_ticket_status": _can_change_ticket_status(request.user, current_ticket),
        "can_manage_documents": _can_manage_ticket_documents(request.user, current_ticket),
        "movement_events": _build_movement_events(current_ticket),
        "users": users,
    }
    return render(request, "desk/ticket.html", context)


@login_required
def send_message(request, ticket_id):
    current_ticket = get_object_or_404(Ticket, pk=ticket_id)
    if request.method != "POST":
        return redirect("ticket", ticket_id=ticket_id)
    if not _can_view_ticket(request.user, current_ticket):
        raise Http404("Страница не найдена")

    if "delete-document" in request.POST:
        if not _can_manage_ticket_documents(request.user, current_ticket):
            raise Http404("Страница не найдена")

        document = get_object_or_404(
            Document,
            pk=request.POST.get("document_id"),
            ticket=current_ticket,
        )
        document_name = document.file.name.split("/")[-1]

        chat = list(current_ticket.chat or [])
        for entry in chat:
            if entry.get("document_id") == document.pk:
                entry["document_deleted"] = True
                entry["document_url"] = ""
        current_ticket.chat = chat
        _append_system_message(
            current_ticket,
            f'Документ "{document_name}" откреплен пользователем {_get_user_display_name(request.user)}',
        )
        current_ticket.save()

        if document.file:
            document.file.delete(save=False)
        document.delete()

        return redirect("ticket", ticket_id=ticket_id)

    if "save-ticket" in request.POST:
        if not _can_manage_ticket_workflow(request.user, current_ticket):
            raise Http404("Страница не найдена")

        changes = []
        actor_name = _get_user_display_name(request.user)

        technician_id = request.POST.get("technician")
        if technician_id:
            technician = get_object_or_404(
                User.objects.filter(groups__name=executor_group_name).distinct(),
                pk=technician_id,
            )
            if current_ticket.technician_id != technician.id:
                previous_assignee = _get_user_display_name(current_ticket.technician)
                current_ticket.technician = technician
                _notify_users(
                    "Вам назначена заявка №{} по административно-хозяйственной службе".format(
                        current_ticket.id
                    ),
                    'Вам назначена заявка №{} с темой "{}"'.format(
                        current_ticket.id,
                        current_ticket.title,
                    ),
                    technician,
                )
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

        new_progress = request.POST.get("progress")
        if new_progress in Ticket.Progres.values and current_ticket.progress != new_progress:
            current_ticket.progress = new_progress
            subject = "Статус заявки №{} изменен".format(current_ticket.id)
            message = 'Статус заявки №{} с темой "{}" изменен на "{}"'.format(
                current_ticket.id,
                current_ticket.title,
                current_ticket.progress,
            )
            _notify_users(subject, message, current_ticket.created_by, current_ticket.technician)
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

        for message, event_type, event_meta in changes:
            _append_system_message(
                current_ticket,
                message,
                event_type=event_type,
                event_meta=event_meta,
            )

        if changes:
            current_ticket.save()
            if _can_view_ticket(request.user, current_ticket):
                return redirect("ticket", ticket_id=ticket_id)
            return redirect("index")

        return redirect("ticket", ticket_id=ticket_id)

    if "close-ticket" in request.POST:
        if not _can_change_ticket_status(request.user, current_ticket):
            raise Http404("Страница не найдена")

        current_ticket.status = Ticket.Status.CLOSED
        subject = "Заявка №{} закрыта".format(current_ticket.id)
        message = 'Заявка №{} с темой "{}" закрыта.'.format(
            current_ticket.id,
            current_ticket.title,
        )
        actor_name = _get_user_display_name(request.user)

        _notify_users(subject, message, current_ticket.technician, current_ticket.created_by)

        _append_system_message(
            current_ticket,
            f"Заявка закрыта пользователем {actor_name}",
            event_type="closed",
            event_meta={"actor": actor_name},
        )
        current_ticket.save()
        return redirect("index")

    if "open-ticket" in request.POST:
        if not _can_change_ticket_status(request.user, current_ticket):
            raise Http404("Страница не найдена")

        current_ticket.status = Ticket.Status.OPENED
        actor_name = _get_user_display_name(request.user)
        _notify_users(
            "Заявка №{} открыта повторно".format(current_ticket.id),
            'Заявка №{} с темой "{}" открыта повторно.'.format(
                current_ticket.id,
                current_ticket.title,
            ),
            current_ticket.created_by,
        )
        _notify_users(
            "Заявка №{} открыта повторно".format(current_ticket.id),
            'Заявка №{} с темой "{}" открыта повторно.'.format(
                current_ticket.id,
                current_ticket.title,
            ),
            current_ticket.technician,
        )
        _append_system_message(
            current_ticket,
            f"Заявка открыта пользователем {actor_name}",
            event_type="reopened",
            event_meta={"actor": actor_name},
        )
        current_ticket.save()
        return redirect("index")

    uploaded_documents = request.FILES.getlist("chat_documents")
    if uploaded_documents:
        for uploaded_document in uploaded_documents:
            document = Document.objects.create(ticket=current_ticket, file=uploaded_document)
            _append_document_message(current_ticket, request.user, document)

    message_text = request.POST.get("message_text")
    if message_text:
        chat = list(current_ticket.chat or [])
        chat.append(
            {
                "author": _get_user_display_name(request.user),
                "message": message_text,
                "datetime": _current_timestamp(),
            }
        )
        current_ticket.chat = chat

    if uploaded_documents or message_text:
        current_ticket.save()
    return redirect("ticket", ticket_id=ticket_id)
