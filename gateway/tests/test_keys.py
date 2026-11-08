from tollgate_gateway.keys import display_prefix, generate_key, hash_key, looks_like_key


def test_keys_are_prefixed_long_and_unique() -> None:
    keys = {generate_key() for _ in range(100)}

    assert len(keys) == 100
    assert all(k.startswith("tg_") for k in keys)
    assert all(len(k) >= 40 for k in keys)  # 256 random bits, url-safe base64


def test_hash_is_stable_for_the_same_pepper() -> None:
    key = generate_key()

    assert hash_key(key, b"pepper") == hash_key(key, b"pepper")


def test_hash_depends_on_the_pepper() -> None:
    # A stolen database without the pepper can't be used to check guessed keys.
    key = generate_key()

    assert hash_key(key, b"pepper-a") != hash_key(key, b"pepper-b")


def test_hash_reveals_nothing_of_the_key() -> None:
    key = generate_key()
    digest = hash_key(key, b"pepper")

    assert len(digest) == 64  # hex SHA-256
    assert key[3:11] not in digest


def test_display_prefix_shows_only_the_start() -> None:
    key = generate_key()

    assert display_prefix(key) == key[:11]
    assert len(display_prefix(key)) < len(key) // 3


def test_api_keys_are_told_apart_from_jwts() -> None:
    assert looks_like_key(generate_key())
    assert not looks_like_key("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln")  # a JWT
    assert not looks_like_key("tg_")
    assert not looks_like_key("")
