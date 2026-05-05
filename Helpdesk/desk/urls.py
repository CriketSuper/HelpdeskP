from django.urls import path
from .views import (
    Login_View,
    MyLogoutView,
    TicketCreateView,
    index,
    profile_settings,
    send_message,
    ticket,
)
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path('add/', TicketCreateView.as_view(), name='add'),
    path('', index, name='index'),
    path('login/', Login_View, name='login'),
    path('logout/', MyLogoutView.as_view(), name='logout'),
    path('profiles/', profile_settings, name='profiles'),
    path('profile/', profile_settings, name='profile_settings'),
    path('<int:ticket_id>/', ticket, name='ticket'),
    path('send_message/<int:ticket_id>/', send_message, name='send_message'),
    ]
