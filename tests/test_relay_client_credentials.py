"""A relay client's own credentials: created once, kept private, shown as a bind code.

Before 2026-10-04 the client_id was a relay client's only identity and also
its bind code, so knowing it was enough to impersonate or bind a machine. Each
machine now generates a token (trust on first use) and a bind code it shows
only on its own screen; the relay stores hashes of both. The relay side of
that contract is tested against the Go relay in tests/test_relay_contract.py.
"""

from __future__ import annotations

TOKEN_A = "a" * 43
CODE_A = "ABCDEFGHJKLM"

from aria_code.clients import aria_relay_client as client


class TestClientCredentials:
    def test_created_once_saved_privately_and_reused(self, tmp_path, monkeypatch):
        # setenv (not delenv) so monkeypatch restores whatever
        # ensure_credentials writes into os.environ.
        monkeypatch.setenv("ARIA_RELAY_CLIENT_TOKEN", "")
        monkeypatch.setenv("ARIA_RELAY_BIND_CODE", "")
        env_file = tmp_path / ".aria" / ".env"
        first = client.ensure_credentials(env_file)
        assert oct(env_file.stat().st_mode & 0o777) == "0o600"
        assert len(first[0]) >= 32 and len(first[1]) == 12
        assert client.ensure_credentials(env_file) == first
        assert env_file.read_text().count("ARIA_RELAY_CLIENT_TOKEN=") == 1

    def test_bind_codes_avoid_characters_people_misread(self):
        codes = "".join(client.new_bind_code() for _ in range(200))
        assert not set(codes) & set("0O1I")

    def test_the_bind_code_is_typed_in_groups(self):
        assert client.format_bind_code("abcdefghjklm") == "ARIA-BIND-ABCD-EFGH-JKLM"

    def test_the_register_frame_carries_the_credentials(self, monkeypatch):
        monkeypatch.setenv("ARIA_RELAY_SECRET", "s3cret")
        frame = client.register_frame("aria-a", TOKEN_A, CODE_A)
        assert frame == {"type": "register", "client_id": "aria-a", "token": TOKEN_A,
                         "bind_code": CODE_A, "secret": "s3cret"}
