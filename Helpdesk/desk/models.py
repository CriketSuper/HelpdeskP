from django.contrib.auth import get_user_model
from django.db import models

admin_group_name = "Админ"
executor_group_name = "Исполнитель"
director_group_name = "Директор"

User = get_user_model()


def get_assignable_technicians():
    return (
        User.objects.filter(
            models.Q(groups__name=director_group_name)
            | models.Q(groups__name=executor_group_name)
        )
        .order_by("id")
        .distinct()
    )


def get_default_technician_user():
    director = User.objects.filter(groups__name=director_group_name).order_by("id").first()
    if director is not None:
        return director
    return get_assignable_technicians().first()


def get_default_technician():
    technician = get_default_technician_user()
    return technician.pk if technician else None


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    verbose_name = models.CharField(max_length=255, verbose_name="Имя пользователя")
    position = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Должность",
    )
    receive_email_notifications = models.BooleanField(
        default=True,
        verbose_name="Получать уведомления по почте",
    )

    def __str__(self):
        return self.verbose_name


class Ticket(models.Model):
    class Kinds(models.TextChoices):
        LOW = "Низкая", "Низкая"
        MEDIUM = "Средняя", "Средняя"
        HIGH = "Высокая", "Высокая"
        CRITICAL = "Критичная", "Критичная"

    class Progres(models.TextChoices):
        IMPOSSIBLE = "Невозможно выполнить", "Невозможно выполнить"
        ACCEPTED = "Принято к рассмотрению", "Принято к рассмотрению"
        AGREED = "Согласовано", "Согласовано"
        TODO = "К исполнению", "К исполнению"
        INPROGRESS = "В стадии решения", "В стадии решения"
        DECIDED = "Успешно решено", "Успешно решено"

    class Status(models.TextChoices):
        OPENED = "открыта", "Открыта"
        CLOSED = "закрыта", "Закрыта"

    title = models.CharField(max_length=100, verbose_name="Тема")
    content = models.TextField(verbose_name="Текст")
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        verbose_name="Автор",
        blank=True,
        null=True,
        related_name="created_tickets",
    )
    criticalness = models.CharField(
        max_length=25,
        choices=Kinds.choices,
        default=Kinds.MEDIUM,
        verbose_name="Критичность",
    )
    technician = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        verbose_name="Исполнитель",
        blank=True,
        null=True,
        limit_choices_to=(
            models.Q(groups__name=director_group_name)
            | models.Q(groups__name=executor_group_name)
        ),
        default=get_default_technician,
        related_name="assigned_tickets",
    )
    participants = models.ManyToManyField(
        User,
        blank=True,
        related_name="visible_tickets",
        verbose_name="Участники",
    )
    progress = models.CharField(
        max_length=25,
        choices=Progres.choices,
        default=Progres.ACCEPTED,
        verbose_name="Статус заявки",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.OPENED,
        verbose_name="Открытость заявки",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Дата и время создания",
    )
    deadline = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Срок выполнения",
    )
    published = models.DateTimeField(
        auto_now=True,
        db_index=True,
        verbose_name="Дата и время изменения",
    )
    chat = models.JSONField(default=list, blank=True, verbose_name="Чат")
    documents = models.ManyToManyField(
        "Document",
        blank=True,
        related_name="related_tickets",
        verbose_name="Документ",
    )

    def get_absolute_url(self):
        return f"{self.pk}/"

    class Meta:
        verbose_name_plural = "Заявки"
        verbose_name = "Заявка"
        ordering = ["-published"]
        permissions = [("view_all_tickets", "Can view all tickets")]


class Document(models.Model):
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="related_documents",
        verbose_name="Заявка",
    )
    file = models.FileField(
        upload_to="files/",
        blank=True,
        null=True,
        verbose_name="Документ",
    )

    def __str__(self):
        return self.file.name if self.file else ""

    class Meta:
        verbose_name_plural = "Документы"
        verbose_name = "Документ"


class Notification(models.Model):
    class Levels(models.TextChoices):
        INFO = "info", "Информация"
        SUCCESS = "success", "Успешно"
        WARNING = "warning", "Предупреждение"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Пользователь",
    )
    title = models.CharField(max_length=255, verbose_name="Заголовок")
    body = models.TextField(blank=True, verbose_name="Текст")
    level = models.CharField(
        max_length=20,
        choices=Levels.choices,
        default=Levels.INFO,
        verbose_name="Уровень",
    )
    link = models.CharField(max_length=255, blank=True, verbose_name="Ссылка")
    is_read = models.BooleanField(default=False, verbose_name="Прочитано")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"

    def __str__(self):
        return f"{self.title} -> {self.user}"
