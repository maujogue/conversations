"""Tests for the push_router_prompt management command."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from chat.router.prompts import DEFAULT_ROUTER_PROMPT, ROUTER_PROMPT_NAME


def run(*args):
    out = StringIO()
    call_command("push_router_prompt", *args, stdout=out)
    return out.getvalue()


def test_requires_langfuse_enabled(settings):
    settings.LANGFUSE_ENABLED = False
    with pytest.raises(CommandError):
        run()


def test_creates_prompt_when_missing(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.side_effect = Exception("404 not found")
    client.create_prompt.return_value = mock.Mock(version=1)
    with mock.patch("langfuse.get_client", return_value=client):
        out = run()
    client.create_prompt.assert_called_once_with(
        name=ROUTER_PROMPT_NAME,
        prompt=DEFAULT_ROUTER_PROMPT,
        labels=["production"],
        type="text",
        commit_message=mock.ANY,
    )
    assert "Created" in out and "v1" in out


def test_skips_when_production_text_is_identical(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(
        prompt=DEFAULT_ROUTER_PROMPT, version=4, is_fallback=False
    )
    with mock.patch("langfuse.get_client", return_value=client):
        out = run()
    client.create_prompt.assert_not_called()
    assert "up to date" in out


def test_creates_new_version_when_text_differs(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(prompt="old text", version=4, is_fallback=False)
    client.create_prompt.return_value = mock.Mock(version=5)
    with mock.patch("langfuse.get_client", return_value=client):
        out = run()
    client.create_prompt.assert_called_once()
    assert "v5" in out


def test_dry_run_does_not_write(settings):
    settings.LANGFUSE_ENABLED = True
    client = mock.Mock()
    client.get_prompt.return_value = mock.Mock(prompt="old text", version=4, is_fallback=False)
    with mock.patch("langfuse.get_client", return_value=client):
        out = run("--dry-run")
    client.create_prompt.assert_not_called()
    assert "Would create" in out
