"""Tests for the push_langfuse_dashboard management command."""

import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

import httpx
import pytest
import respx

from chat.management.commands.push_langfuse_dashboard import DEFAULT_DEFINITION

HOST = "http://langfuse.test"
WIDGETS_URL = f"{HOST}/api/public/unstable/dashboard-widgets"
DASHBOARDS_URL = f"{HOST}/api/public/unstable/dashboards"
PROJECTS_URL = f"{HOST}/api/public/projects"


def _page(items):
    return {
        "data": items,
        "meta": {"page": 1, "limit": 50, "totalItems": len(items), "totalPages": 1},
    }


@pytest.fixture(autouse=True)
def _langfuse_settings(settings):
    settings.LANGFUSE_ENABLED = True
    settings.LANGFUSE_HOST = HOST
    settings.LANGFUSE_PUBLIC_KEY = "pk-test"
    settings.LANGFUSE_SECRET_KEY = "sk-test"


@pytest.fixture
def definition_file(tmp_path):
    """A two-widget dashboard definition written to disk."""
    definition = {
        "name": "Router",
        "description": "Router dashboard",
        "filters": [],
        "widgets": [
            {
                "placement": {"id": "a", "x": 0, "y": 0, "width": 6, "height": 6},
                "widget": {
                    "name": "Router · A",
                    "view": "observations",
                    "dimensions": [],
                    "metrics": [{"measure": "count", "agg": "count"}],
                    "filters": [],
                    "chartType": "NUMBER",
                },
            },
            {
                "placement": {"id": "b", "x": 6, "y": 0, "width": 6, "height": 6},
                "widget": {
                    "name": "Router · B",
                    "description": "b",
                    "view": "scores-categorical",
                    "dimensions": [{"field": "stringValue"}],
                    "metrics": [{"measure": "count", "agg": "count"}],
                    "filters": [],
                    "chartType": "PIE",
                    "chartConfig": {"row_limit": 5},
                },
            },
        ],
    }
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(definition))
    return path


def _widget(widget_id, name):
    return {
        "id": widget_id,
        "name": name,
        "description": "",
        "view": "observations",
        "dimensions": [],
        "metrics": [],
        "filters": [],
        "chartType": "NUMBER",
        "chartConfig": {"type": "NUMBER"},
    }


def _dashboard(dashboard_id, name, widgets=()):
    return {
        "id": dashboard_id,
        "name": name,
        "description": "",
        "definition": {"widgets": list(widgets)},
        "filters": [],
    }


def _mock_lists(widgets=(), dashboards=()):
    respx.get(WIDGETS_URL).mock(return_value=httpx.Response(200, json=_page(list(widgets))))
    respx.get(DASHBOARDS_URL).mock(return_value=httpx.Response(200, json=_page(list(dashboards))))
    respx.get(PROJECTS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "proj-1", "name": "P"}]})
    )


@respx.mock
def test_creates_widgets_and_dashboard_when_missing(definition_file):
    """First run: every widget is POSTed, then the dashboard with placements."""
    _mock_lists()
    create_widget = respx.post(WIDGETS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_widget("w-a", "Router · A")),
            httpx.Response(200, json=_widget("w-b", "Router · B")),
        ]
    )
    create_dashboard = respx.post(DASHBOARDS_URL).mock(
        return_value=httpx.Response(200, json=_dashboard("d-1", "Router"))
    )

    out = StringIO()
    call_command("push_langfuse_dashboard", "--definition", str(definition_file), stdout=out)

    assert create_widget.call_count == 2
    first_body = json.loads(create_widget.calls[0].request.content)
    assert first_body == {
        "name": "Router · A",
        "description": "",
        "view": "observations",
        "dimensions": [],
        "metrics": [{"measure": "count", "agg": "count"}],
        "filters": [],
        "chartType": "NUMBER",
    }
    second_body = json.loads(create_widget.calls[1].request.content)
    assert second_body["chartConfig"] == {"row_limit": 5}
    assert create_widget.calls[0].request.headers["Authorization"].startswith("Basic ")

    assert create_dashboard.call_count == 1
    dashboard_body = json.loads(create_dashboard.calls[0].request.content)
    assert dashboard_body["name"] == "Router"
    assert dashboard_body["description"] == "Router dashboard"
    assert dashboard_body["filters"] == []
    assert dashboard_body["definition"]["widgets"] == [
        {"type": "widget", "id": "a", "widgetId": "w-a", "x": 0, "y": 0, "width": 6, "height": 6},
        {"type": "widget", "id": "b", "widgetId": "w-b", "x": 6, "y": 0, "width": 6, "height": 6},
    ]
    assert f"{HOST}/project/proj-1/dashboards/d-1" in out.getvalue()


@respx.mock
def test_updates_existing_widgets_and_dashboard_by_name(definition_file):
    """Second run: widgets and dashboard matched by name are PATCHed, nothing is created/deleted."""
    _mock_lists(
        widgets=[_widget("w-a", "Router · A"), _widget("w-other", "Someone else's widget")],
        dashboards=[_dashboard("d-other", "Other"), _dashboard("d-1", "Router")],
    )
    patch_a = respx.patch(f"{WIDGETS_URL}/w-a").mock(
        return_value=httpx.Response(200, json=_widget("w-a", "Router · A"))
    )
    create_b = respx.post(WIDGETS_URL).mock(
        return_value=httpx.Response(200, json=_widget("w-b", "Router · B"))
    )
    patch_dashboard = respx.patch(f"{DASHBOARDS_URL}/d-1").mock(
        return_value=httpx.Response(200, json=_dashboard("d-1", "Router"))
    )
    create_dashboard = respx.post(DASHBOARDS_URL)
    delete_anything = respx.delete(url__regex=rf"{HOST}/.*")

    out = StringIO()
    call_command("push_langfuse_dashboard", "--definition", str(definition_file), stdout=out)

    assert patch_a.call_count == 1
    assert create_b.call_count == 1
    assert patch_dashboard.call_count == 1
    assert not create_dashboard.called
    assert not delete_anything.called
    body = json.loads(patch_dashboard.calls[0].request.content)
    assert [w["widgetId"] for w in body["definition"]["widgets"]] == ["w-a", "w-b"]
    assert "update widget 'Router · A'" in out.getvalue()
    assert "create widget 'Router · B'" in out.getvalue()
    assert "update dashboard 'Router' (d-1)" in out.getvalue()


@respx.mock
def test_dry_run_only_reads(definition_file):
    """--dry-run lists the plan and performs no write."""
    _mock_lists(widgets=[_widget("w-a", "Router · A")])
    writes = respx.route(method__in=["POST", "PATCH", "DELETE"], url__regex=rf"{HOST}/.*")

    out = StringIO()
    call_command(
        "push_langfuse_dashboard", "--dry-run", "--definition", str(definition_file), stdout=out
    )

    assert not writes.called
    text = out.getvalue()
    assert "[dry-run] Would update widget 'Router · A'" in text
    assert "[dry-run] Would create widget 'Router · B'" in text
    assert "[dry-run] Would create dashboard 'Router'" in text


@respx.mock
def test_reads_definition_from_stdin(definition_file, monkeypatch):
    """`--definition -` reads the JSON from stdin (used from the app container)."""
    monkeypatch.setattr("sys.stdin", StringIO(definition_file.read_text()))
    _mock_lists()
    writes = respx.route(method__in=["POST", "PATCH"], url__regex=rf"{HOST}/.*")

    call_command("push_langfuse_dashboard", "--dry-run", "--definition", "-", stdout=StringIO())

    assert not writes.called
    assert respx.get(WIDGETS_URL).called


@respx.mock
def test_http_error_raises_command_error(definition_file):
    """A failing Langfuse call surfaces as a CommandError with the status code."""
    _mock_lists()
    respx.post(WIDGETS_URL).mock(return_value=httpx.Response(400, json={"message": "bad"}))

    with pytest.raises(CommandError, match="400"):
        call_command("push_langfuse_dashboard", "--definition", str(definition_file))


@respx.mock
def test_refuses_ambiguous_dashboard_name(definition_file):
    """Two dashboards with the target name: refuse rather than guess."""
    _mock_lists(dashboards=[_dashboard("d-1", "Router"), _dashboard("d-2", "Router")])

    with pytest.raises(CommandError, match="2 dashboards are named 'Router'"):
        call_command("push_langfuse_dashboard", "--definition", str(definition_file))


def test_requires_langfuse_enabled(settings, definition_file):
    settings.LANGFUSE_ENABLED = False
    with pytest.raises(CommandError, match="LANGFUSE_ENABLED"):
        call_command("push_langfuse_dashboard", "--definition", str(definition_file))


def test_requires_credentials(settings, definition_file):
    settings.LANGFUSE_SECRET_KEY = None
    with pytest.raises(CommandError, match="LANGFUSE_SECRET_KEY"):
        call_command("push_langfuse_dashboard", "--definition", str(definition_file))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.pop("name"), "non-empty 'name'"),
        (lambda d: d.__setitem__("widgets", []), "at least one widget"),
        (
            lambda d: d["widgets"][1]["widget"].__setitem__("name", "Router · A"),
            "Duplicate widget name",
        ),
        (lambda d: d["widgets"][1]["placement"].__setitem__("id", "a"), "Duplicate placement id"),
    ],
)
def test_rejects_invalid_definition(definition_file, mutation, message):
    definition = json.loads(definition_file.read_text())
    mutation(definition)
    definition_file.write_text(json.dumps(definition))

    with pytest.raises(CommandError, match=message):
        call_command("push_langfuse_dashboard", "--definition", str(definition_file))


def test_repo_definition_is_valid_when_present():
    """The versioned docs/langfuse/router-dashboard.json passes validation (host checkout only)."""
    if not DEFAULT_DEFINITION.exists():
        pytest.skip("docs/ is not mounted in this environment")
    from chat.management.commands.push_langfuse_dashboard import load_definition  # noqa: PLC0415

    definition = load_definition(DEFAULT_DEFINITION)
    assert definition["name"] == "Router"
    views = {"observations", "scores-numeric", "scores-boolean", "scores-categorical"}
    for entry in definition["widgets"]:
        assert entry["widget"]["view"] in views
        assert set(entry["placement"]) == {"id", "x", "y", "width", "height"}
