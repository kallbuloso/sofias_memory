from __future__ import annotations

import secrets

API_KEY_PREFIX = "sf-"
MIN_RANDOM_CHARACTERS = 32


def generate_api_key(random_characters: int = MIN_RANDOM_CHARACTERS) -> str:
    if random_characters < MIN_RANDOM_CHARACTERS:
        msg = f"random_characters must be at least {MIN_RANDOM_CHARACTERS}"
        raise ValueError(msg)

    token = secrets.token_urlsafe(random_characters)
    if len(token) < random_characters:
        msg = "generated token is shorter than requested"
        raise RuntimeError(msg)

    return f"{API_KEY_PREFIX}{token}"


def main() -> int:
    print(generate_api_key())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
