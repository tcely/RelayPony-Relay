from django.urls import path

from signaling.views import signal

urlpatterns = [
    path("api/signal", signal, name="signal"),
]

