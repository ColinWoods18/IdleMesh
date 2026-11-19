from idlemesh_coord.fsperm import is_owner_only, restrict_to_owner, write_private


def test_write_private_is_owner_only(tmp_path):
    p = tmp_path / "secret.key"
    write_private(p, b"secret")
    assert p.read_bytes() == b"secret" and is_owner_only(p)
    write_private(p, b"rotated")  # replacing keeps the restriction
    assert p.read_bytes() == b"rotated" and is_owner_only(p)


def test_an_ordinary_file_is_not_owner_only_until_restricted(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("x")
    p.chmod(0o644)  # POSIX: readable by others. Windows: inherits the folder's ACL entries.
    assert not is_owner_only(p)
    restrict_to_owner(p)
    assert is_owner_only(p)
