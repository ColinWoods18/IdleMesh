"""security.py (IDs, secrets, verifiers, passphrases) and tokens.py (approximate token accounting)."""
import re

import pytest

from idlemesh_coord import security, tokens


# --- security --------------------------------------------------------------------------------
def test_ids_carry_their_prefix_and_are_unique():
    ids = {security.new_id("dev") for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"dev_[0-9a-f]{16}", i) for i in ids)


def test_secrets_meet_the_entropy_floor():
    # FR 02: at least 128 bits. token_urlsafe(24) is 32 characters of base64url (192 bits).
    s = security.new_secret()
    assert len(s) == 32 and re.fullmatch(r"[A-Za-z0-9_-]+", s)
    assert security.new_secret() != s
    assert len(security.new_secret(16)) >= 22  # 128 bits


def test_verifier_matches_only_the_same_secret():
    v = security.verifier("abc")
    assert re.fullmatch(r"[0-9a-f]{64}", v) and "abc" not in v
    assert security.matches("abc", v)
    assert not security.matches("abd", v)
    assert not security.matches("", v)


def test_fingerprint_is_grouped_and_deterministic():
    fp = security.fingerprint(b"public key bytes")
    assert re.fullmatch(r"([0-9A-F]{4}-){5}[0-9A-F]{4}", fp)
    assert fp == security.fingerprint(b"public key bytes") != security.fingerprint(b"other key")


@pytest.fixture(scope="module")
def passwords():
    return security.Passwords(fast=True)


def test_passphrase_hash_verifies_and_is_salted(passwords):
    h = passwords.hash("correct horse battery staple")
    assert h.startswith("$argon2id$") and "correct" not in h
    assert passwords.verify(h, "correct horse battery staple")
    assert not passwords.verify(h, "correct horse battery stapler")
    assert passwords.hash("correct horse battery staple") != h  # a fresh salt each time


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$argon2id$garbage"])
def test_missing_or_corrupt_hashes_never_verify(passwords, stored):
    # None is the "no such user" path: it still runs a hash, so timing does not reveal the login exists.
    assert passwords.verify(stored, "dummy passphrase for timing equalisation") is False


# --- tokens ----------------------------------------------------------------------------------
@pytest.mark.parametrize("text,n", [("", 0), ("hello", 1), ("Hello, world!", 4), ("a  b\tc\n", 3),
                                    ("don't", 3), ("3.14", 3)])
def test_count_tokens_splits_words_and_punctuation(text, n):
    assert tokens.count_tokens(text) == n


def test_context_tokens_adds_template_overhead_per_message():
    msgs = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi there"}]
    expected = tokens.TEMPLATE_TOKENS_FIXED + (3 + 4) + (2 + 4)
    assert tokens.context_tokens(msgs) == expected
    assert tokens.context_tokens([]) == tokens.TEMPLATE_TOKENS_FIXED
