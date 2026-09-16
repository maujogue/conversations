"""Create or update the `router-classifier` prompt in Langfuse prompt management."""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chat.router.prompts import DEFAULT_ROUTER_PROMPT, ROUTER_PROMPT_NAME

logger = logging.getLogger(__name__)

PRODUCTION_LABEL = "production"


class Command(BaseCommand):
    """Push the local default router prompt to Langfuse under the `production` label.

    Idempotent: when the current production version already carries the same
    text, nothing is created.
    """

    help = "Create/update the router-classifier prompt in Langfuse (label: production)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing to Langfuse.",
        )

    def handle(self, *args, **options):
        if not settings.LANGFUSE_ENABLED:
            raise CommandError("LANGFUSE_ENABLED is false; nothing to push.")

        from langfuse import get_client  # noqa: PLC0415

        client = get_client()

        current_text = None
        current_version = None
        try:
            current = client.get_prompt(
                ROUTER_PROMPT_NAME,
                label=PRODUCTION_LABEL,
                type="text",
                cache_ttl_seconds=0,
                max_retries=0,
            )
        except Exception as exc:  # noqa: BLE001
            # A missing prompt raises; anything else is also "no usable version".
            logger.info("No current production prompt found: %s", exc)
        else:
            if not getattr(current, "is_fallback", False):
                current_text = current.prompt
                current_version = current.version

        if current_text == DEFAULT_ROUTER_PROMPT:
            self.stdout.write(
                f"'{ROUTER_PROMPT_NAME}' v{current_version} ({PRODUCTION_LABEL}) is up to date."
            )
            return

        if options["dry_run"]:
            self.stdout.write(
                f"Would create a new '{ROUTER_PROMPT_NAME}' version labelled "
                f"{PRODUCTION_LABEL} (current: {current_version})."
            )
            return

        created = client.create_prompt(
            name=ROUTER_PROMPT_NAME,
            prompt=DEFAULT_ROUTER_PROMPT,
            labels=[PRODUCTION_LABEL],
            type="text",
            commit_message="Pushed from chat.router.prompts.DEFAULT_ROUTER_PROMPT",
        )
        self.stdout.write(
            f"Created '{ROUTER_PROMPT_NAME}' v{created.version} labelled {PRODUCTION_LABEL}."
        )
