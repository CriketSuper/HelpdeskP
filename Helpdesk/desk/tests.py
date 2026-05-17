import os
import shutil
from email.header import decode_header
from io import BytesIO
from zipfile import ZipFile

from django.core import mail
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import LoginForm, ProfileSettingsForm, TicketForm, VerboseNameBackend
from .models import (
    Document,
    Notification,
    Ticket,
    UserProfile,
    admin_group_name,
    director_group_name,
    executor_group_name,
    get_default_technician,
)
from .views import _build_movement_events, _get_notification_email, _notify_users

User = get_user_model()


def _create_profile(user, verbose_name, position=""):
    return UserProfile.objects.create(user=user, verbose_name=verbose_name, position=position)


def _decode_header_value(value):
    parts = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8"))
        else:
            parts.append(chunk)
    return "".join(parts)


class VerboseNameBackendTests(TestCase):
    def setUp(self):
        self.backend = VerboseNameBackend()

    def test_authenticate_by_verbose_name(self):
        user = User.objects.create_user(username="ivan", password="secret123")
        _create_profile(user, "Иван Иванов")

        authenticated_user = self.backend.authenticate(
            request=None,
            username="Иван Иванов",
            password="secret123",
        )

        self.assertEqual(authenticated_user, user)

    def test_login_form_resolves_selected_profile(self):
        user = User.objects.create_user(username="ivan", password="secret123")
        profile = _create_profile(user, "Иван Иванов")

        form = LoginForm(
            data={
                "user_profile": profile.pk,
                "username_display": "Иван Иванов",
                "password": "secret123",
            }
        )

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["resolved_username"], "Иван Иванов")

    def test_default_admin_is_created_automatically(self):
        admin_user = User.objects.get(username="admin")

        self.assertTrue(admin_user.is_superuser)
        self.assertTrue(admin_user.groups.filter(name=admin_group_name).exists())
        self.assertEqual(admin_user.profile.verbose_name, "Администратор")

    def test_executor_group_is_created_automatically(self):
        self.assertTrue(Group.objects.filter(name=executor_group_name).exists())

    def test_director_group_is_created_automatically(self):
        self.assertTrue(Group.objects.filter(name=director_group_name).exists())

    def test_get_default_technician_prefers_first_director_user_id(self):
        director_group = Group.objects.get(name=director_group_name)
        executor_group = Group.objects.get(name=executor_group_name)

        director = User.objects.create_user(username="director-1", password="secret123")
        _create_profile(director, "Директор 1")
        director.groups.add(director_group)

        first_executor = User.objects.create_user(username="executor-1", password="secret123")
        _create_profile(first_executor, "Исполнитель 1")
        first_executor.groups.add(executor_group)

        second_executor = User.objects.create_user(username="executor-2", password="secret123")
        _create_profile(second_executor, "Исполнитель 2")
        second_executor.groups.add(executor_group)

        self.assertEqual(get_default_technician(), director.pk)

    def test_ticket_form_shows_directors_and_executors_in_technician_field(self):
        director_group = Group.objects.get(name=director_group_name)
        executor_group = Group.objects.get(name=executor_group_name)

        director = User.objects.create_user(username="director-1", password="secret123")
        _create_profile(director, "Директор 1")
        director.groups.add(director_group)

        executor = User.objects.create_user(username="executor-1", password="secret123")
        _create_profile(executor, "Исполнитель 1")
        executor.groups.add(executor_group)

        regular_user = User.objects.create_user(username="user-1", password="secret123")
        _create_profile(regular_user, "Пользователь 1")

        form = TicketForm()

        self.assertEqual(list(form.fields["technician"].queryset), [director, executor])

    def test_ticket_progress_choices_include_agreed_and_todo(self):
        self.assertIn(Ticket.Progres.AGREED, Ticket.Progres.values)
        self.assertIn(Ticket.Progres.TODO, Ticket.Progres.values)

    def test_login_view_accepts_search_field_submission(self):
        user = User.objects.create_user(username="petrov", password="secret123")
        profile = _create_profile(user, "Петров Петр")

        response = self.client.post(
            reverse("login"),
            {
                "user_profile": profile.pk,
                "username_display": "Петров Петр",
                "password": "secret123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("index"))


class TicketAccessTests(TestCase):
    def setUp(self):
        self.admin_group = Group.objects.get(name=admin_group_name)
        self.executor_group = Group.objects.get(name=executor_group_name)

        self.admin = User.objects.get(username="admin")

        self.executor_1 = User.objects.create_user(username="executor-1", password="secret123")
        _create_profile(self.executor_1, "Исполнитель 1")
        self.executor_1.groups.add(self.executor_group)

        self.executor_2 = User.objects.create_user(username="executor-2", password="secret123")
        _create_profile(self.executor_2, "Исполнитель 2")
        self.executor_2.groups.add(self.executor_group)

        self.user = User.objects.create_user(username="user-1", password="secret123")
        _create_profile(self.user, "Пользователь 1")

        self.other_user = User.objects.create_user(username="user-2", password="secret123")
        _create_profile(self.other_user, "Пользователь 2")

        self.assigned_ticket = Ticket.objects.create(
            title="Assigned",
            content="Assigned ticket",
            created_by=self.user,
            technician=self.executor_1,
        )
        self.created_by_executor_ticket = Ticket.objects.create(
            title="Created by executor",
            content="Executor ticket",
            created_by=self.executor_1,
            technician=self.executor_2,
        )
        self.foreign_ticket = Ticket.objects.create(
            title="Foreign",
            content="Foreign ticket",
            created_by=self.other_user,
            technician=self.executor_2,
        )

    def test_admin_sees_all_tickets(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("index"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tickets"].count(), 3)

    def test_executor_sees_created_and_assigned_tickets(self):
        self.client.force_login(self.executor_1)

        response = self.client.get(reverse("index"))

        self.assertEqual(response.status_code, 200)
        ticket_ids = {ticket.pk for ticket in response.context["tickets"]}
        self.assertEqual(
            ticket_ids,
            {self.assigned_ticket.pk, self.created_by_executor_ticket.pk},
        )

    def test_regular_user_sees_only_own_tickets(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("index"))

        self.assertEqual(response.status_code, 200)
        ticket_ids = {ticket.pk for ticket in response.context["tickets"]}
        self.assertEqual(ticket_ids, {self.assigned_ticket.pk})

    def test_index_is_paginated_by_eighteen_tickets(self):
        self.client.force_login(self.admin)

        for index in range(32):
            Ticket.objects.create(
                title=f"Bulk {index}",
                content="Bulk ticket",
                created_by=self.user,
                technician=self.executor_1,
            )

        first_page_response = self.client.get(reverse("index"))
        second_page_response = self.client.get(reverse("index"), {"page": 2})

        self.assertEqual(first_page_response.status_code, 200)
        self.assertEqual(second_page_response.status_code, 200)
        self.assertEqual(len(first_page_response.context["tickets"]), 18)
        self.assertTrue(first_page_response.context["is_paginated"])
        self.assertEqual(first_page_response.context["page_obj"].paginator.per_page, 18)
        self.assertEqual(first_page_response.context["page_obj"].paginator.count, 35)
        self.assertEqual(len(second_page_response.context["tickets"]), 17)

    def test_executor_can_reassign_and_change_progress_for_assigned_ticket(self):
        self.client.force_login(self.executor_1)

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.assigned_ticket.pk}),
            {
                "save-ticket": "true",
                "technician": self.executor_2.pk,
                "progress": Ticket.Progres.INPROGRESS,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("ticket", kwargs={"ticket_id": self.assigned_ticket.pk}))
        self.assigned_ticket.refresh_from_db()

        self.assertEqual(self.assigned_ticket.technician, self.executor_2)
        self.assertEqual(self.assigned_ticket.progress, Ticket.Progres.INPROGRESS)
        self.assertTrue(self.assigned_ticket.participants.filter(pk=self.executor_1.pk).exists())
        self.assertEqual(self.assigned_ticket.chat[-2]["event_type"], "assignment")
        self.assertEqual(self.assigned_ticket.chat[-1]["event_type"], "progress")

    def test_previous_executor_keeps_access_after_reassignment(self):
        self.assigned_ticket.participants.add(self.executor_1)
        self.assigned_ticket.technician = self.executor_2
        self.assigned_ticket.save()

        self.client.force_login(self.executor_1)

        response = self.client.get(reverse("ticket", kwargs={"ticket_id": self.assigned_ticket.pk}))

        self.assertEqual(response.status_code, 200)

    def test_regular_user_cannot_change_ticket_workflow(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.assigned_ticket.pk}),
            {
                "save-ticket": "true",
                "technician": self.executor_2.pk,
                "progress": Ticket.Progres.INPROGRESS,
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assigned_ticket.refresh_from_db()
        self.assertEqual(self.assigned_ticket.technician, self.executor_1)
        self.assertEqual(self.assigned_ticket.progress, Ticket.Progres.ACCEPTED)

    def test_author_can_close_ticket(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.assigned_ticket.pk}),
            {
                "close-ticket": "true",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("index"))
        self.assigned_ticket.refresh_from_db()
        self.assertEqual(self.assigned_ticket.status, Ticket.Status.CLOSED)
        self.assertEqual(self.assigned_ticket.chat[-1]["event_type"], "closed")

    def test_executor_cannot_close_ticket_if_not_author_or_admin(self):
        self.client.force_login(self.executor_1)

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.assigned_ticket.pk}),
            {
                "close-ticket": "true",
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assigned_ticket.refresh_from_db()
        self.assertEqual(self.assigned_ticket.status, Ticket.Status.OPENED)

    def test_author_can_edit_ticket_details_and_chat_logs_changes(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("ticket_edit", kwargs={"ticket_id": self.assigned_ticket.pk}),
            {
                "title": "Assigned updated",
                "content": "Updated content",
                "criticalness": Ticket.Kinds.HIGH,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("ticket", kwargs={"ticket_id": self.assigned_ticket.pk}),
        )
        self.assigned_ticket.refresh_from_db()
        self.assertEqual(self.assigned_ticket.title, "Assigned updated")
        self.assertEqual(self.assigned_ticket.content, "Updated content")
        self.assertEqual(self.assigned_ticket.criticalness, Ticket.Kinds.HIGH)
        chat_messages = [entry.get("message", "") for entry in self.assigned_ticket.chat]
        self.assertTrue(any("Тема заявки изменена" in message for message in chat_messages))
        self.assertTrue(any("Критичность заявки изменена" in message for message in chat_messages))
        self.assertTrue(
            any(
                'Содержание заявки изменено пользователем Пользователь 1 с "Assigned ticket" на "Updated content"' in message
                for message in chat_messages
            )
        )

    def test_admin_can_edit_foreign_ticket_details(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("ticket_edit", kwargs={"ticket_id": self.foreign_ticket.pk}),
            {
                "title": "Foreign updated",
                "content": "Admin updated content",
                "criticalness": Ticket.Kinds.CRITICAL,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.foreign_ticket.refresh_from_db()
        self.assertEqual(self.foreign_ticket.title, "Foreign updated")
        self.assertEqual(self.foreign_ticket.criticalness, Ticket.Kinds.CRITICAL)

    def test_executor_cannot_edit_ticket_details(self):
        self.client.force_login(self.executor_1)

        response = self.client.get(
            reverse("ticket_edit", kwargs={"ticket_id": self.assigned_ticket.pk})
        )

        self.assertEqual(response.status_code, 404)


class TicketDocumentFlowTests(TestCase):
    def setUp(self):
        self.media_root = os.path.join(os.getcwd(), "test_media_root")
        shutil.rmtree(self.media_root, ignore_errors=True)
        os.makedirs(self.media_root, exist_ok=True)
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()

        self.user = User.objects.create_user(username="user1", password="secret123")
        _create_profile(self.user, "Пользователь 1")
        self.ticket = Ticket.objects.create(
            title="Test ticket",
            content="Test content",
            created_by=self.user,
        )
        self.client.force_login(self.user)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_chat_upload_adds_document_to_ticket_and_chat(self):
        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.ticket.pk}),
            {
                "message_text": "Смотри файл",
                "chat_documents": [
                    SimpleUploadedFile(
                        "manual.txt",
                        b"hello",
                        content_type="text/plain",
                    ),
                    SimpleUploadedFile(
                        "guide.txt",
                        b"world",
                        content_type="text/plain",
                    ),
                ],
            },
        )

        self.assertEqual(response.status_code, 302)

        self.ticket.refresh_from_db()
        documents = list(Document.objects.filter(ticket=self.ticket).order_by("id"))

        self.assertEqual(self.ticket.related_documents.count(), 2)
        self.assertEqual(self.ticket.chat[0]["message"], "прикрепил новые документы")
        self.assertEqual(len(self.ticket.chat[0]["documents"]), 2)
        self.assertEqual(self.ticket.chat[0]["documents"][0]["document_name"], "manual.txt")
        self.assertEqual(self.ticket.chat[0]["documents"][1]["document_name"], "guide.txt")
        self.assertEqual(self.ticket.chat[1]["message"], "Смотри файл")
        self.assertEqual(documents[0].file.name.split("/")[-1], "manual.txt")
        self.assertEqual(documents[1].file.name.split("/")[-1], "guide.txt")

    def test_chat_upload_rejects_too_large_file(self):
        oversized = SimpleUploadedFile(
            "large.bin",
            b"xx",
            content_type="application/octet-stream",
        )

        with override_settings(MAX_UPLOAD_SIZE_MB=0, MAX_UPLOAD_SIZE_BYTES=1):
            response = self.client.post(
                reverse("send_message", kwargs={"ticket_id": self.ticket.pk}),
                {
                    "message_text": "too large",
                    "chat_documents": oversized,
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "превышает")
        self.assertEqual(Document.objects.filter(ticket=self.ticket).count(), 0)

    def test_delete_document_marks_chat_entry_and_removes_document(self):
        document = Document.objects.create(
            ticket=self.ticket,
            file=SimpleUploadedFile("report.txt", b"report", content_type="text/plain"),
        )
        self.ticket.chat = [
            {
                "author": "Пользователь 1",
                "message": 'прикрепил новый документ "report.txt"',
                "datetime": "2026-05-05 00:00:00",
                "document_id": document.pk,
                "document_name": "report.txt",
                "document_url": "/media/files/report.txt",
                "document_deleted": False,
            }
        ]
        self.ticket.save()

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.ticket.pk}),
            {
                "delete-document": "true",
                "document_id": document.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()

        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertTrue(self.ticket.chat[0]["document_deleted"])
        self.assertEqual(self.ticket.chat[0]["document_url"], "")

    def test_delete_document_marks_nested_chat_entry_and_removes_document(self):
        document = Document.objects.create(
            ticket=self.ticket,
            file=SimpleUploadedFile("nested.txt", b"nested", content_type="text/plain"),
        )
        self.ticket.chat = [
            {
                "author": "Пользователь 1",
                "message": "прикрепил новый документ",
                "datetime": "2026-05-05 00:00:00",
                "documents": [
                    {
                        "document_id": document.pk,
                        "document_name": "nested.txt",
                        "document_deleted": False,
                    }
                ],
            }
        ]
        self.ticket.save()

        response = self.client.post(
            reverse("send_message", kwargs={"ticket_id": self.ticket.pk}),
            {
                "delete-document": "true",
                "document_id": document.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()

        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertTrue(self.ticket.chat[0]["documents"][0]["document_deleted"])

    def test_document_download_requires_ticket_access(self):
        document = Document.objects.create(
            ticket=self.ticket,
            file=SimpleUploadedFile("report.txt", b"report", content_type="text/plain"),
        )
        outsider = User.objects.create_user(username="outsider", password="secret123")
        _create_profile(outsider, "Р§СѓР¶РѕР№")
        self.client.force_login(outsider)

        response = self.client.get(reverse("download_document", kwargs={"document_id": document.pk}))

        self.assertEqual(response.status_code, 404)

    def test_participant_can_download_document(self):
        document = Document.objects.create(
            ticket=self.ticket,
            file=SimpleUploadedFile("report.txt", b"report", content_type="text/plain"),
        )
        participant = User.objects.create_user(username="participant", password="secret123")
        _create_profile(participant, "РЈС‡Р°СЃС‚РЅРёРє")
        self.ticket.participants.add(participant)
        self.client.force_login(participant)

        response = self.client.get(reverse("download_document", kwargs={"document_id": document.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="report.txt"', response.get("Content-Disposition"))

    def test_movement_events_include_structured_system_steps(self):
        self.ticket.chat = [
            {
                "author": "Уведомление системы",
                "message": "Исполнитель заявки изменен",
                "datetime": "2026-05-06 09:00:00",
                "event_type": "assignment",
                "event_meta": {
                    "actor": "Администратор",
                    "previous_assignee": "Исполнитель 1",
                    "assignee": "Пользователь 1",
                },
            },
            {
                "author": "Уведомление системы",
                "message": "Статус заявки изменен",
                "datetime": "2026-05-06 09:30:00",
                "event_type": "progress",
                "event_meta": {
                    "actor": "Администратор",
                    "progress": "В стадии решения",
                },
            },
            {
                "author": "Уведомление системы",
                "message": "Заявка закрыта",
                "datetime": "2026-05-06 10:00:00",
                "event_type": "closed",
                "event_meta": {
                    "actor": "Администратор",
                },
            },
        ]
        self.ticket.save()

        events = _build_movement_events(self.ticket)

        self.assertEqual(events[0]["title"], "Создан")
        self.assertEqual(events[1]["title"], "Передана")
        self.assertEqual(events[1]["subtitle"], "Пользователь 1")
        self.assertIn("От кого: Исполнитель 1", events[1]["tooltip"])
        self.assertEqual(events[2]["title"], "Изменён статус")
        self.assertEqual(events[2]["subtitle"], "В стадии решения")
        self.assertEqual(events[3]["title"], "Закрыта")


class TicketCreateTests(TestCase):
    def setUp(self):
        self.director_group = Group.objects.get(name=director_group_name)
        self.executor_group = Group.objects.get(name=executor_group_name)
        self.creator = User.objects.create_user(username="creator", password="secret123")
        _create_profile(self.creator, "Создатель")

        self.director = User.objects.create_user(username="director-1", password="secret123")
        _create_profile(self.director, "Директор 1")
        self.director.groups.add(self.director_group)

        self.executor = User.objects.create_user(username="executor-1", password="secret123")
        _create_profile(self.executor, "Исполнитель 1")
        self.executor.groups.add(self.executor_group)

        self.client.force_login(self.creator)

    def test_create_form_prefills_director_as_default_technician(self):
        response = self.client.get(reverse("add"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_technician_id"], self.director.pk)
        self.assertEqual(response.context["selected_technician_name"], "Директор 1")

    def test_user_can_choose_executor_while_creating_ticket(self):
        response = self.client.post(
            reverse("add"),
            {
                "title": "Новая заявка",
                "content": "Текст заявки",
                "criticalness": Ticket.Kinds.MEDIUM,
                "technician": self.executor.pk,
                "document_names": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get(title="Новая заявка")
        self.assertEqual(ticket.created_by, self.creator)
        self.assertEqual(ticket.technician, self.executor)


class TicketDocumentExportTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(username="memo-author", password="secret123")
        _create_profile(self.creator, "Иванов Дмитрий Викторович", position="Заместитель директора")

        self.executor = User.objects.create_user(username="memo-executor", password="secret123")
        _create_profile(self.executor, "Снежная Светлана Леонидовна", position="Директор")

        self.ticket = Ticket.objects.create(
            title="Выдача картриджа",
            content="Прошу выдать картридж",
            created_by=self.creator,
            technician=self.executor,
        )
        self.client.force_login(self.creator)

    def _read_document_xml(self, response):
        with ZipFile(BytesIO(response.content)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        return xml

    def test_ticket_document_download_renders_template_placeholders(self):
        response = self.client.get(
            reverse("ticket_document_download", kwargs={"ticket_id": self.ticket.pk})
        )

        self.assertEqual(response.status_code, 200)
        content_disposition = _decode_header_value(response["Content-Disposition"])
        self.assertIn("attachment;", content_disposition)
        xml = self._read_document_xml(response)
        self.assertIn("Директору", xml)
        self.assertIn("Helpdesk", xml)
        self.assertIn("С.Л. Снежной", xml)
        self.assertIn("Заместителя директора", xml)
        self.assertIn("Иванова Д.В.", xml)
        self.assertIn("Прошу выдать картридж", xml)
        self.assertIn("г.", xml)
        self.assertNotIn("{{", xml)

    def test_ticket_document_print_returns_inline_pdf(self):
        response = self.client.get(
            reverse("ticket_document_print", kwargs={"ticket_id": self.ticket.pk})
        )

        self.assertEqual(response.status_code, 200)
        content_disposition = _decode_header_value(response["Content-Disposition"])
        self.assertIn("inline;", content_disposition)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


class LogoutTests(TestCase):
    def test_logout_accepts_post_and_redirects_to_login(self):
        user = User.objects.create_user(username="logout-user", password="secret123")
        self.client.force_login(user)

        response = self.client.post(reverse("logout"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/desk/login/")


class RoutingTests(TestCase):
    def test_root_redirects_to_desk(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/desk/")


class ProfileSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="profile-user",
            password="secret123",
            email="old@example.com",
        )
        _create_profile(self.user, "Старое Имя")
        self.client.force_login(self.user)

    def test_profile_settings_updates_name_email_and_notification_flag(self):
        response = self.client.post(
            reverse("profiles"),
            {
                "save-profile": "true",
                "next": reverse("index"),
                "verbose_name": "Новое Имя",
                "email": "new@example.com",
                "receive_email_notifications": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("index"))

        self.user.refresh_from_db()
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.verbose_name, "Новое Имя")
        self.assertEqual(self.user.email, "new@example.com")
        self.assertFalse(self.user.profile.receive_email_notifications)
        self.assertIsNone(_get_notification_email(self.user))

    def test_profiles_page_is_available(self):
        response = self.client.get(reverse("profiles"))

        self.assertEqual(response.status_code, 200)

    def test_profile_settings_form_does_not_expose_position_field(self):
        form = ProfileSettingsForm(user=self.user, instance=self.user.profile)

        self.assertNotIn("position", form.fields)

    def test_profile_settings_can_change_password(self):
        response = self.client.post(
            reverse("profiles"),
            {
                "change-password": "true",
                "old_password": "secret123",
                "new_password1": "new-secret-123",
                "new_password2": "new-secret-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secret-123"))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_HOST="smtp.example.com",
    DEFAULT_FROM_EMAIL="helpdesk@example.com",
    EMAIL_SUBJECT_PREFIX="[Helpdesk] ",
)
class NotificationTests(TestCase):
    def test_notify_users_sends_one_email_to_distinct_recipients(self):
        creator = User.objects.create_user(
            username="creator-mail",
            password="secret123",
            email="creator@example.com",
        )
        _create_profile(creator, "Создатель")

        executor = User.objects.create_user(
            username="executor-mail",
            password="secret123",
            email="executor@example.com",
        )
        _create_profile(executor, "Исполнитель")

        result = _notify_users("Тест", "Проверка", creator, executor, creator)

        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            set(mail.outbox[0].to),
            {"creator@example.com", "executor@example.com"},
        )
        self.assertEqual(mail.outbox[0].subject, "[Helpdesk] Тест")

    def test_notify_users_skips_users_with_disabled_notifications(self):
        enabled_user = User.objects.create_user(
            username="enabled-mail",
            password="secret123",
            email="enabled@example.com",
        )
        _create_profile(enabled_user, "Включён")

        disabled_user = User.objects.create_user(
            username="disabled-mail",
            password="secret123",
            email="disabled@example.com",
        )
        disabled_profile = _create_profile(disabled_user, "Выключен")
        disabled_profile.receive_email_notifications = False
        disabled_profile.save(update_fields=["receive_email_notifications"])

        result = _notify_users("Тест", "Проверка", enabled_user, disabled_user)

        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["enabled@example.com"])

    def test_notifications_feed_returns_unread_items_and_marks_them_read(self):
        user = User.objects.create_user(username="notify-user", password="secret123")
        _create_profile(user, "РЈРІРµРґРѕРјР»СЏРµРјС‹Р№")
        Notification.objects.create(
            user=user,
            title="РќРѕРІР°СЏ Р·Р°СЏРІРєР°",
            body="Р’Р°Рј РЅР°Р·РЅР°С‡РµРЅР° Р·Р°СЏРІРєР°.",
            level=Notification.Levels.SUCCESS,
            link="/desk/1/",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("notifications_feed"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["notifications"]), 1)
        self.assertFalse(
            Notification.objects.filter(user=user, is_read=False).exists()
        )

    def test_notifications_feed_collapses_multiple_updates_for_same_ticket(self):
        user = User.objects.create_user(username="notify-bulk-user", password="secret123")
        _create_profile(user, "Уведомляемый")
        Notification.objects.create(
            user=user,
            title="Обновление по заявке",
            body="Статус изменен на Согласовано.",
            level=Notification.Levels.INFO,
            link="/desk/7/",
        )
        Notification.objects.create(
            user=user,
            title="Обновление по заявке",
            body="Исполнитель изменен.",
            level=Notification.Levels.INFO,
            link="/desk/7/",
        )
        Notification.objects.create(
            user=user,
            title="Обновление по заявке",
            body="Добавлен новый документ.",
            level=Notification.Levels.INFO,
            link="/desk/7/",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("notifications_feed"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()["notifications"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["collapsed_count"], 3)
        self.assertIn("Еще 2 изменений", payload[0]["body"])
        self.assertFalse(
            Notification.objects.filter(user=user, is_read=False).exists()
        )

    def test_ticket_creation_creates_in_app_notification_for_executor(self):
        executor_group = Group.objects.get(name=executor_group_name)
        creator = User.objects.create_user(username="creator-ui", password="secret123")
        _create_profile(creator, "РЎРѕР·РґР°С‚РµР»СЊ")
        executor = User.objects.create_user(
            username="executor-ui",
            password="secret123",
            email="executor-ui@example.com",
        )
        _create_profile(executor, "РСЃРїРѕР»РЅРёС‚РµР»СЊ")
        executor.groups.add(executor_group)
        self.client.force_login(creator)

        response = self.client.post(
            reverse("add"),
            {
                "title": "РРЅС†РёРґРµРЅС‚",
                "content": "РќРѕРІР°СЏ Р·Р°СЏРІРєР°",
                "criticalness": Ticket.Kinds.MEDIUM,
                "technician": executor.pk,
                "document_names": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Notification.objects.filter(user=executor).exists())
