"""Create or update the Langfuse `Router` dashboard from docs/langfuse/router-dashboard.json."""

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
DEFAULT_DEFINITION = _REPO_ROOT / "docs" / "langfuse" / "router-dashboard.json"
WIDGETS_PATH = "/api/public/unstable/dashboard-widgets"
DASHBOARDS_PATH = "/api/public/unstable/dashboards"
PROJECTS_PATH = "/api/public/projects"
PAGE_SIZE = 50
WIDGET_FIELDS = (
    "name",
    "description",
    "view",
    "dimensions",
    "metrics",
    "filters",
    "chartType",
    "chartConfig",
)


class LangfuseDashboardClient:
    """Thin wrapper over the Langfuse public (unstable) dashboard API."""

    def __init__(self, host, public_key, secret_key, timeout=30.0):
        self._client = httpx.Client(
            base_url=host.rstrip("/"),
            auth=(public_key, secret_key),
            timeout=timeout,
        )

    def close(self):
        self._client.close()

    def _request(self, method, path, **kwargs):
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
        return response.json()

    def _list_all(self, path):
        items = []
        page = 1
        while True:
            body = self._request("GET", path, params={"page": page, "limit": PAGE_SIZE})
            items.extend(body.get("data", []))
            meta = body.get("meta") or {}
            if page >= int(meta.get("totalPages") or 0):
                return items
            page += 1

    def list_widgets(self):
        return self._list_all(WIDGETS_PATH)

    def list_dashboards(self):
        return self._list_all(DASHBOARDS_PATH)

    def create_widget(self, spec):
        return self._request("POST", WIDGETS_PATH, json=spec)

    def update_widget(self, widget_id, spec):
        return self._request("PATCH", f"{WIDGETS_PATH}/{widget_id}", json=spec)

    def create_dashboard(self, payload):
        return self._request("POST", DASHBOARDS_PATH, json=payload)

    def update_dashboard(self, dashboard_id, payload):
        return self._request("PATCH", f"{DASHBOARDS_PATH}/{dashboard_id}", json=payload)

    def project_id(self):
        """Return the project id bound to the API key, or None when unavailable."""
        try:
            body = self._request("GET", PROJECTS_PATH)
        except CommandError as exc:
            logger.info("Could not resolve the Langfuse project id: %s", exc)
            return None
        data = body.get("data") or []
        return data[0].get("id") if data else None


def load_definition(path):
    """Read and minimally validate the dashboard definition (a file path, or `-` for stdin)."""
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        definition = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise CommandError(
            f"Cannot read dashboard definition {path}: {exc}. "
            "Pass --definition <path>, or --definition - and pipe the JSON on stdin."
        ) from exc

    if not definition.get("name"):
        raise CommandError("Dashboard definition needs a non-empty 'name'.")
    widgets = definition.get("widgets") or []
    if not widgets:
        raise CommandError("Dashboard definition needs at least one widget.")

    seen_names, seen_placements = set(), set()
    for entry in widgets:
        widget = entry.get("widget") or {}
        placement = entry.get("placement") or {}
        name = widget.get("name")
        if not name:
            raise CommandError("Every widget needs a 'widget.name'.")
        if name in seen_names:
            raise CommandError(f"Duplicate widget name: {name!r}.")
        placement_id = placement.get("id")
        if not placement_id:
            raise CommandError(f"Widget {name!r} needs a 'placement.id'.")
        if placement_id in seen_placements:
            raise CommandError(f"Duplicate placement id: {placement_id!r}.")
        seen_names.add(name)
        seen_placements.add(placement_id)
    return definition


def widget_payload(widget):
    """Keep only the fields the widget API accepts, in a stable order."""
    payload = {field: widget[field] for field in WIDGET_FIELDS if field in widget}
    payload.setdefault("description", "")
    payload.setdefault("dimensions", [])
    payload.setdefault("filters", [])
    return payload


class Command(BaseCommand):
    """Push the versioned Router dashboard to Langfuse.

    Widgets and the dashboard are matched by name: existing ones are updated in
    place, missing ones are created. Nothing is ever deleted, so other
    dashboards and widgets in the project are left untouched.
    """

    help = "Create/update the Langfuse Router dashboard from docs/langfuse/router-dashboard.json."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan without writing to Langfuse.",
        )
        parser.add_argument(
            "--definition",
            default=str(DEFAULT_DEFINITION),
            help=f"Path to the dashboard JSON (default: {DEFAULT_DEFINITION}).",
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

        definition = load_definition(options["definition"])
        dry_run = options["dry_run"]

        client = LangfuseDashboardClient(
            settings.LANGFUSE_HOST, settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY
        )
        try:
            self._sync(client, definition, dry_run)
        finally:
            client.close()

    def _sync(self, client, definition, dry_run):
        existing_widgets = {w["name"]: w for w in client.list_widgets()}
        existing_dashboards = [
            d for d in client.list_dashboards() if d.get("name") == definition["name"]
        ]
        if len(existing_dashboards) > 1:
            raise CommandError(
                f"{len(existing_dashboards)} dashboards are named {definition['name']!r}; "
                "rename or delete the extra ones in the Langfuse UI first."
            )
        dashboard = existing_dashboards[0] if existing_dashboards else None

        prefix = "[dry-run] Would " if dry_run else ""
        placements = []
        for entry in definition["widgets"]:
            spec = widget_payload(entry["widget"])
            placement = dict(entry["placement"])
            current = existing_widgets.get(spec["name"])
            if current is None:
                verb = "create"
                if not dry_run:
                    current = client.create_widget(spec)
            else:
                verb = "update"
                if not dry_run:
                    current = client.update_widget(current["id"], spec)
            self.stdout.write(f"{prefix}{verb} widget {spec['name']!r}")
            placement["widgetId"] = current["id"] if current else "<new>"
            placement["type"] = "widget"
            placements.append(placement)

        payload = {
            "name": definition["name"],
            "description": definition.get("description") or "",
            "definition": {"widgets": placements},
            "filters": definition.get("filters") or [],
        }
        if dashboard is None:
            self.stdout.write(f"{prefix}create dashboard {definition['name']!r}")
            if not dry_run:
                dashboard = client.create_dashboard(payload)
        else:
            self.stdout.write(
                f"{prefix}update dashboard {definition['name']!r} ({dashboard['id']})"
            )
            if not dry_run:
                dashboard = client.update_dashboard(dashboard["id"], payload)

        if dry_run:
            return
        project_id = client.project_id()
        host = settings.LANGFUSE_HOST.rstrip("/")
        url = (
            f"{host}/project/{project_id}/dashboards/{dashboard['id']}"
            if project_id
            else f"{host} (dashboard id {dashboard['id']})"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Dashboard {definition['name']!r} is up to date with "
                f"{len(placements)} widgets: {url}"
            )
        )
