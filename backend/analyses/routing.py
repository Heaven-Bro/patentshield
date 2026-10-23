"""WebSocket routes for analysis progress updates."""

from django.urls import re_path

from analyses.consumers import AnalysisProgressConsumer

websocket_urlpatterns = [
    re_path(
        r"ws/v1/analyses/(?P<analysis_id>[0-9a-f-]+)/$",
        AnalysisProgressConsumer.as_asgi(),
    ),
]
