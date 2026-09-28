"""Tests for the push_langfuse_model_prices management command."""

import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

import httpx
import pytest
import respx

from chat.management.commands.push_langfuse_model_prices import DEFAULT_DEFINITION

HOST = "http://langfuse.test"
MODELS_URL = f"{HOST}/api/public/models"


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
def prices_file(tmp_path):
    """A two-model price list written to disk."""
    prices = [
        {
            "modelName": "mistral-medium-3-5",
            "matchPattern": "(?i)^mistral-medium-3-5$",
            "unit": "TOKENS",
            "inputPrice": 0.0000015,
            "outputPrice": 0.0000075,
            "source": "https://mistral.ai/pricing/api",
        },
        {
            "modelName": "gpt-oss-120b",
            "matchPattern": "(?i)^(openai/)?gpt-oss-120b$",
            "unit": "TOKENS",
            "inputPrice": 0.00000003,
            "outputPrice": 0.00000017,
            "source": "https://openrouter.ai/openai/gpt-oss-120b",
        },
    ]
    path = tmp_path / "model-prices.json"
    path.write_text(json.dumps(prices))
    return path


def _model(model_id, name, langfuse_managed=False):
    """A model definition as the API returns it.

    The real API reports no `projectId`; `isLangfuseManaged` is the only thing
    that tells a built-in definition from one the project owns.
    """
    return {
        "id": model_id,
        "modelName": name,
        "matchPattern": f"(?i)^{name}$",
        "unit": "TOKENS",
        "inputPrice": 0.000001,
        "outputPrice": 0.000002,
        "isLangfuseManaged": langfuse_managed,
    }


def _mock_list(models=()):
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=_page(list(models))))


@respx.mock
def test_creates_models_when_missing(prices_file):
    """First run: every price entry is POSTed as a new model definition."""
    _mock_list()
    create = respx.post(MODELS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_model("m-1", "mistral-medium-3-5")),
            httpx.Response(200, json=_model("m-2", "gpt-oss-120b")),
        ]
    )

    out = StringIO()
    call_command("push_langfuse_model_prices", "--prices", str(prices_file), stdout=out)

    assert create.call_count == 2
    first_body = json.loads(create.calls[0].request.content)
    assert first_body == {
        "modelName": "mistral-medium-3-5",
        "matchPattern": "(?i)^mistral-medium-3-5$",
        "unit": "TOKENS",
        "inputPrice": 0.0000015,
        "outputPrice": 0.0000075,
    }
    assert create.calls[0].request.headers["Authorization"].startswith("Basic ")
    assert "create model 'mistral-medium-3-5'" in out.getvalue()
    assert "create model 'gpt-oss-120b'" in out.getvalue()
    assert "Pushed 2 model definitions." in out.getvalue()


@respx.mock
def test_replaces_our_own_models_by_name(prices_file):
    """The models API has no update verb: one of ours is deleted, then recreated."""
    _mock_list(models=[_model("m-1", "mistral-medium-3-5"), _model("m-other", "some-other-model")])
    delete_medium = respx.delete(f"{MODELS_URL}/m-1").mock(return_value=httpx.Response(204))
    create = respx.post(MODELS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_model("m-1b", "mistral-medium-3-5")),
            httpx.Response(200, json=_model("m-2", "gpt-oss-120b")),
        ]
    )
    delete_others = respx.delete(f"{MODELS_URL}/m-other")

    out = StringIO()
    call_command("push_langfuse_model_prices", "--prices", str(prices_file), stdout=out)

    assert delete_medium.call_count == 1
    assert create.call_count == 2
    # A model we do not manage is never touched, whatever its name.
    assert not delete_others.called
    assert "replace model 'mistral-medium-3-5'" in out.getvalue()
    assert "create model 'gpt-oss-120b'" in out.getvalue()


@respx.mock
def test_a_langfuse_builtin_is_shadowed_never_deleted(prices_file):
    """Langfuse ships some of these names; its definitions are not ours to delete."""
    _mock_list(models=[_model("builtin-1", "mistral-medium-3-5", langfuse_managed=True)])
    deletes = respx.delete(url__regex=rf"{HOST}/.*")
    create = respx.post(MODELS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_model("m-1", "mistral-medium-3-5")),
            httpx.Response(200, json=_model("m-2", "gpt-oss-120b")),
        ]
    )

    out = StringIO()
    call_command("push_langfuse_model_prices", "--prices", str(prices_file), stdout=out)

    assert not deletes.called
    assert create.call_count == 2
    assert "shadow the built-in model 'mistral-medium-3-5'" in out.getvalue()


@respx.mock
def test_dry_run_only_reads(prices_file):
    """--dry-run lists the plan and performs no write."""
    _mock_list(models=[_model("m-1", "mistral-medium-3-5")])
    writes = respx.route(method__in=["POST", "DELETE"], url__regex=rf"{HOST}/.*")

    out = StringIO()
    call_command(
        "push_langfuse_model_prices", "--dry-run", "--prices", str(prices_file), stdout=out
    )

    assert not writes.called
    text = out.getvalue()
    assert "[dry-run] Would replace model 'mistral-medium-3-5'" in text
    assert "[dry-run] Would create model 'gpt-oss-120b'" in text


@respx.mock
def test_reads_prices_from_stdin(prices_file, monkeypatch):
    """`--prices -` reads the JSON from stdin (used from the app container)."""
    monkeypatch.setattr("sys.stdin", StringIO(prices_file.read_text()))
    _mock_list()
    writes = respx.route(method__in=["POST", "DELETE"], url__regex=rf"{HOST}/.*")

    call_command("push_langfuse_model_prices", "--dry-run", "--prices", "-", stdout=StringIO())

    assert not writes.called
    assert respx.get(MODELS_URL).called


@respx.mock
def test_http_error_raises_command_error(prices_file):
    """A failing Langfuse call surfaces as a CommandError with the status code."""
    _mock_list()
    respx.post(MODELS_URL).mock(return_value=httpx.Response(400, json={"message": "bad"}))

    with pytest.raises(CommandError, match="400"):
        call_command("push_langfuse_model_prices", "--prices", str(prices_file))


def test_requires_langfuse_enabled(settings, prices_file):
    settings.LANGFUSE_ENABLED = False
    with pytest.raises(CommandError, match="LANGFUSE_ENABLED"):
        call_command("push_langfuse_model_prices", "--prices", str(prices_file))


def test_requires_credentials(settings, prices_file):
    settings.LANGFUSE_SECRET_KEY = None
    with pytest.raises(CommandError, match="LANGFUSE_SECRET_KEY"):
        call_command("push_langfuse_model_prices", "--prices", str(prices_file))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda p: p.clear(), "at least one entry"),
        (lambda p: p[0].pop("modelName"), "non-empty 'modelName'"),
        (lambda p: p.append(dict(p[0])), "Duplicate modelName"),
        (lambda p: p[0].pop("matchPattern"), "needs a 'matchPattern'"),
        (
            lambda p: (p[0].pop("inputPrice"), p[0].pop("outputPrice")),
            "needs an 'inputPrice' or a 'totalPrice'",
        ),
    ],
)
def test_rejects_invalid_prices(prices_file, mutation, message):
    prices = json.loads(prices_file.read_text())
    mutation(prices)
    prices_file.write_text(json.dumps(prices))

    with pytest.raises(CommandError, match=message):
        call_command("push_langfuse_model_prices", "--prices", str(prices_file))


def test_repo_prices_are_valid_when_present():
    """The versioned docs/langfuse/model-prices.json passes validation (host checkout only)."""
    if not DEFAULT_DEFINITION.exists():
        pytest.skip("docs/ is not mounted in this environment")
    from chat.management.commands.push_langfuse_model_prices import load_prices  # noqa: PLC0415

    prices = load_prices(DEFAULT_DEFINITION)
    for entry in prices:
        assert entry["unit"] == "TOKENS"
        assert entry["inputPrice"] >= 0
        assert entry["outputPrice"] >= 0
