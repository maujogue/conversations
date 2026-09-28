"""Create or update Langfuse model definitions from docs/langfuse/model-prices.json.

Langfuse only computes a generation's `totalCost` when its model name matches a
model definition with a price; router models are not in Langfuse's built-in
list, so they show $0 in the Usage dashboard until we register one each.
"""

import json
import logging
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import httpx

logger = logging.getLogger(__name__)

# <repo>/src/backend/chat/management/commands/<this file> -> <repo>/docs/langfuse/...
# Inside the app container only src/backend is mounted (as /app), so the repo
# root may not exist: fall back to a relative path and let `--definition -`
# (stdin) be the way to feed the file from the host.
_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT_DEPTH = 5
_REPO_ROOT = _PARENTS[_REPO_ROOT_DEPTH] if len(_PARENTS) > _REPO_ROOT_DEPTH else Path(".")
DEFAULT_DEFINITION = _REPO_ROOT / "docs" / "langfuse" / "model-prices.json"
MODELS_PATH = "/api/public/models"
PAGE_SIZE = 50
PRICE_FIELDS = ("modelName", "matchPattern", "unit", "inputPrice", "outputPrice", "totalPrice")


class LangfuseModelsClient:
    """Thin wrapper over the Langfuse public model-definitions API."""

    def __init__(self, host, public_key, secret_key, timeout=30.0):
        self._client = httpx.Client(
            base_url=host.rstrip("/"),
            auth=(public_key, secret_key),
            timeout=timeout,
        )

    def close(self):
        self._client.close()

    def _request(self, method, path, expect_json=True, **kwargs):
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CommandError(
                f"Langfuse {method} {path} failed with {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise CommandError(f"Langfuse {method} {path} failed: {exc}") from exc
        return response.json() if expect_json else None

    def list_models(self):
        items = []
        page = 1
        while True:
            body = self._request("GET", MODELS_PATH, params={"page": page, "limit": PAGE_SIZE})
            items.extend(body.get("data", []))
            meta = body.get("meta") or {}
            if page >= int(meta.get("totalPages") or 0):
                return items
            page += 1

    def create_model(self, spec):
        return self._request("POST", MODELS_PATH, json=spec)

    def delete_model(self, model_id):
        # The models API answers 204 with an empty body, which is not JSON.
        return self._request("DELETE", f"{MODELS_PATH}/{model_id}", expect_json=False)


def load_prices(path):
    """Read and minimally validate the price list (a file path, or `-` for stdin)."""
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        prices = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise CommandError(
            f"Cannot read model prices {path}: {exc}. "
            "Pass --prices <path>, or --prices - and pipe the JSON on stdin."
        ) from exc

    if not isinstance(prices, list) or not prices:
        raise CommandError("Model price list needs at least one entry.")

    seen_names = set()
    for entry in prices:
        name = entry.get("modelName")
        if not name:
            raise CommandError("Every entry needs a non-empty 'modelName'.")
        if name in seen_names:
            raise CommandError(f"Duplicate modelName: {name!r}.")
        if not entry.get("matchPattern"):
            raise CommandError(f"Entry {name!r} needs a 'matchPattern'.")
        if entry.get("inputPrice") is None and entry.get("totalPrice") is None:
            raise CommandError(f"Entry {name!r} needs an 'inputPrice' or a 'totalPrice'.")
        seen_names.add(name)
    return prices


def price_payload(entry):
    """Keep only the fields the models API accepts, in a stable order."""
    return {field: entry[field] for field in PRICE_FIELDS if entry.get(field) is not None}


class Command(BaseCommand):
    """Push router model prices to Langfuse as model definitions.

    Models are matched by `modelName` among the project's own definitions, the
    ones the API reports with `isLangfuseManaged: false`. The models API has no
    update verb (PATCH answers 405) and refuses a POST whose name already exists
    in the project, so re-pushing an entry we own replaces it: delete, then
    create. Langfuse's built-in definitions are never deleted nor edited; a
    project definition of the same name shadows one.
    """

    help = "Create/update Langfuse model definitions from docs/langfuse/model-prices.json."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan without writing to Langfuse.",
        )
        parser.add_argument(
            "--prices",
            default=str(DEFAULT_DEFINITION),
            help=f"Path to the price list JSON (default: {DEFAULT_DEFINITION}).",
        )

    def handle(self, *args, **options):
        if not settings.LANGFUSE_ENABLED:
            raise CommandError("LANGFUSE_ENABLED is false; nothing to push.")
        if not (
            settings.LANGFUSE_HOST and settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY
        ):
            raise CommandError(
                "LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required."
            )

        prices = load_prices(options["prices"])
        dry_run = options["dry_run"]

        client = LangfuseModelsClient(
            settings.LANGFUSE_HOST, settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY
        )
        try:
            self._sync(client, prices, dry_run)
        finally:
            client.close()

    def _sync(self, client, prices, dry_run):
        models = client.list_models()
        # `isLangfuseManaged` is what tells a built-in definition from one of ours;
        # the models API does not return a `projectId` at all, so it cannot be used.
        named = [m for m in models if m.get("modelName")]
        ours = {m["modelName"]: m for m in named if not m.get("isLangfuseManaged")}
        builtin = {m["modelName"] for m in named if m.get("isLangfuseManaged")}

        prefix = "[dry-run] Would " if dry_run else ""
        for entry in prices:
            spec = price_payload(entry)
            name = spec["modelName"]
            current = ours.get(name)
            if current is None:
                verb = "shadow the built-in" if name in builtin else "create"
            else:
                verb = "replace"
                if not dry_run:
                    client.delete_model(current["id"])
            if not dry_run:
                client.create_model(spec)
            self.stdout.write(f"{prefix}{verb} model {name!r}")

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"Pushed {len(prices)} model definitions."))
