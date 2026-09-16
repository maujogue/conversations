"""Chat application"""

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ChatDefaultConfig(AppConfig):
    """Configuration class for the chat application."""

    name = "chat"
    app_label = "chat"
    verbose_name = _("chat application")

    def ready(self):
        """Register deletion propagation for retained Arena metrics."""
        from chat import (  # noqa: PLC0415
            arena_retention,  # pylint: disable=unused-import,import-outside-toplevel
        )
