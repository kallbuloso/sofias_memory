from pathlib import Path

import pytest

from scripts.generate_api_key import API_KEY_PREFIX, MIN_RANDOM_CHARACTERS, generate_api_key, main


def test_generate_api_key_starts_with_prefix() -> None:
    assert generate_api_key().startswith(API_KEY_PREFIX)


def test_generate_api_key_has_minimum_length() -> None:
    key = generate_api_key()

    assert len(key.removeprefix(API_KEY_PREFIX)) >= MIN_RANDOM_CHARACTERS


def test_generate_api_key_values_are_normally_different() -> None:
    assert generate_api_key() != generate_api_key()


def test_generate_api_key_has_no_whitespace() -> None:
    key = generate_api_key()

    assert not any(character.isspace() for character in key)


def test_generate_api_key_does_not_persist_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    before = set(tmp_path.iterdir())
    generate_api_key()
    after = set(tmp_path.iterdir())

    assert after == before


def test_generate_api_key_rejects_short_random_length() -> None:
    with pytest.raises(ValueError):
        generate_api_key(MIN_RANDOM_CHARACTERS - 1)


def test_main_prints_only_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == 0

    output = capsys.readouterr().out.strip()
    assert output.startswith(API_KEY_PREFIX)
    assert "\n" not in output
