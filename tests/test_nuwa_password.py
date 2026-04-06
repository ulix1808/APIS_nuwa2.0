from nuwa_password import hash_password, verify_password


def test_verify_seed_change_me() -> None:
    stored = "pbkdf2_sha256$62d2d8c7f9b444e5940f6ededf5af065$5a85445dbbcae175efcf249c3c92ea34a9e0020aa7320ffd84fed4172b90fcfa"
    assert verify_password("ChangeMe!", stored)
    assert not verify_password("wrong", stored)


def test_roundtrip_hash() -> None:
    h = hash_password("secret-pass-9")
    assert verify_password("secret-pass-9", h)
    assert not verify_password("other", h)
