from django.urls import path
from .views import (
    Login_View,
    MyLogoutView,
    TicketCreateView,
    download_document,
    index,
    notifications_feed,
    profile_settings,
    send_message,
    ticket,
    ticket_edit,
)
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path('add/', TicketCreateView.as_view(), name='add'),
    path('', index, name='index'),
    path('login/', Login_View, name='login'),
    path('logout/', MyLogoutView.as_view(), name='logout'),
    path('notifications/', notifications_feed, name='notifications_feed'),
    path('profiles/', profile_settings, name='profiles'),
    path('profile/', profile_settings, name='profile_settings'),
    path('documents/<int:document_id>/download/', download_document, name='download_document'),
    path('<int:ticket_id>/edit/', ticket_edit, name='ticket_edit'),
    path('<int:ticket_id>/', ticket, name='ticket'),
    path('send_message/<int:ticket_id>/', send_message, name='send_message'),
    ]
