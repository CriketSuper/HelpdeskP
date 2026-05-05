from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.forms import PasswordChangeForm
from django.forms import EmailInput, ModelForm, PasswordInput, Select, Textarea, TextInput

from .models import Ticket, UserProfile, executor_group_name

User = get_user_model()


class ExecutorChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        try:
            return obj.profile.verbose_name
        except UserProfile.DoesNotExist:
            return obj.get_username()


class VerboseNameBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            user_profile = UserProfile.objects.get(verbose_name=username)
            user = user_profile.user
            if user.check_password(password):
                return user
        except UserProfile.DoesNotExist:
            pass

        try:
            user = User.objects.get(username=username)
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            pass

        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None


class TicketForm(ModelForm):
    technician = ExecutorChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Выберите исполнителя...",
        label="Исполнитель",
        widget=Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["technician"].queryset = User.objects.filter(
            groups__name=executor_group_name
        ).order_by("id").distinct()

    class Meta:
        model = Ticket
        fields = ("title", "content", "criticalness", "technician")
        widgets = {
            "title": TextInput(attrs={"class": "form-control"}),
            "content": Textarea(attrs={"class": "form-control"}),
            "criticalness": Select(
                attrs={"class": "form-select"},
                choices=Ticket.Kinds.choices,
            ),
            "technician": Select(attrs={"class": "form-select"}),
        }


class LoginForm(forms.Form):
    user_profile = forms.ModelChoiceField(
        queryset=UserProfile.objects.none(),
        required=False,
        widget=forms.HiddenInput(),
    )
    username_display = forms.CharField(
        label="Пользователь",
        required=False,
        widget=TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "Начните вводить фамилию пользователя",
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        label="Пароль",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        profiles = UserProfile.objects.select_related("user").order_by("verbose_name")
        self.fields["user_profile"].queryset = profiles
        self.user_profiles = list(profiles)

    def clean(self):
        cleaned_data = super().clean()
        selected_profile = cleaned_data.get("user_profile")
        typed_value = (cleaned_data.get("username_display") or "").strip()

        if selected_profile is not None:
            cleaned_data["resolved_username"] = selected_profile.verbose_name
            return cleaned_data

        if typed_value:
            cleaned_data["resolved_username"] = typed_value
            return cleaned_data

        self.add_error("username_display", "Выберите пользователя.")
        return cleaned_data


class ProfileSettingsForm(forms.ModelForm):
    email = forms.EmailField(
        required=False,
        label="Почта для уведомлений",
        widget=EmailInput(attrs={"class": "form-control", "placeholder": "name@example.com"}),
    )

    class Meta:
        model = UserProfile
        fields = ("verbose_name", "receive_email_notifications")
        widgets = {
            "verbose_name": TextInput(attrs={"class": "form-control"}),
            "receive_email_notifications": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "verbose_name": "ФИО",
            "receive_email_notifications": "Получать уведомления по почте",
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if self.user is not None:
            self.fields["email"].initial = self.user.email

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("receive_email_notifications") and not cleaned_data.get("email"):
            self.add_error(
                "email",
                "Укажите почту для уведомлений или отключите уведомления.",
            )
        return cleaned_data

    def save(self, commit=True):
        profile = super().save(commit=False)
        if self.user is not None:
            self.user.email = self.cleaned_data["email"]
            if commit:
                self.user.save(update_fields=["email"])
        if commit:
            profile.save()
        return profile


class PasswordUpdateForm(PasswordChangeForm):
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["old_password"].widget = PasswordInput(
            attrs={"class": "form-control", "autocomplete": "current-password"}
        )
        self.fields["new_password1"].widget = PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        )
        self.fields["new_password2"].widget = PasswordInput(
            attrs={"class": "form-control", "autocomplete": "new-password"}
        )
        self.fields["old_password"].label = "Текущий пароль"
        self.fields["new_password1"].label = "Новый пароль"
        self.fields["new_password2"].label = "Подтвердите новый пароль"
