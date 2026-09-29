"""The family environment chain: real environment wins, then the checkout `.env`, then the
legacy config."""

from __future__ import annotations

import os

import pytest

from jev_remnants.environment import (
    TYPESAFE_SETTINGS,
    _env_file,
    load_typesafe_environment,
)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for name in TYPESAFE_SETTINGS:
        monkeypatch.delenv(name, raising=False)


def test_the_real_environment_wins_over_every_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-env-file\n")
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-shell")

    load_typesafe_environment(root=tmp_path)

    assert os.environ["TYPESAFE_API_KEY"] == "from-shell"


def test_the_checkout_env_file_fills_what_the_environment_lacks(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# config\n"
        "TYPESAFE_BASE_URL=https://drex.nace.ai\n"
        'TYPESAFE_DEFAULT_MODEL="drex-latest"\n'
        "export TYPESAFE_API_KEY=nace-key\n"
    )

    contributed = load_typesafe_environment(root=tmp_path)

    assert os.environ["TYPESAFE_BASE_URL"] == "https://drex.nace.ai"
    assert os.environ["TYPESAFE_DEFAULT_MODEL"] == "drex-latest"
    assert os.environ["TYPESAFE_API_KEY"] == "nace-key"
    assert contributed["TYPESAFE_API_KEY"] == "nace-key"


def test_a_missing_key_everywhere_raises_with_every_source_named(tmp_path, monkeypatch):
    monkeypatch.setattr("jev_remnants.environment.LEGACY_CONFIG", tmp_path / "absent")
    with pytest.raises(RuntimeError, match=r"\.env|jvn/env"):
        load_typesafe_environment(root=tmp_path)


def test_env_file_parsing_is_tolerant(tmp_path):
    path = tmp_path / "env"
    path.write_text('A=plain\nB="quoted"\n# comment\nno equals sign\n=novalue\n')
    assert _env_file(path) == {"A": "plain", "B": "quoted", "": "novalue"}
