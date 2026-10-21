"""The Mac's side of "Connect AI account": an X25519 key pair whose public half
goes out in every heartbeat, and installing credentials that arrive sealed to it.

Payload (JSON, sealed with libsodium's sealed box by `infocus-editor connect`):
  {"backend": "claude", "kind": "oauth_token", "value": "sk-ant-oat01-…"}
  {"backend": "codex"|"grok", "kind": "auth_json", "value": "<auth.json contents>"}
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from editor.backends.cli import write_private
from editor.backends.claude import TOKEN_ITEM, TOKEN_RE
from editor.config import BACKEND_NAMES

KEY_ITEM = "pairing_key"
MAX_AUTH_JSON = 48_000


@dataclass(frozen=True)
class Credential:
    id: str
    backend: str
    ciphertext: str


@dataclass(frozen=True)
class Ack:
    id: str
    ok: bool
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.error:
            out["error"] = self.error[:300]
        return out


class InstallError(Exception):
    """A plain reason that can go back to the Portal (never contains the secret)."""


def seal(public_key_b64: str, payload: Mapping[str, Any]) -> str:
    box = SealedBox(PublicKey(base64.b64decode(public_key_b64)))
    return base64.b64encode(box.encrypt(json.dumps(dict(payload)).encode())).decode()


class Pairing:
    def __init__(self, keychain, pool, codex_home, grok_home):
        self._keychain = keychain
        self._pool = pool
        self._homes = {"codex": codex_home, "grok": grok_home}
        self._key = self._load_or_create()

    def _load_or_create(self) -> PrivateKey:
        stored = self._keychain.get(KEY_ITEM)
        if stored:
            try:
                return PrivateKey(base64.b64decode(stored))
            except (ValueError, TypeError):
                pass  # unreadable: make a new pair (old pairing codes stop working)
        key = PrivateKey.generate()
        self._keychain.set(KEY_ITEM, base64.b64encode(bytes(key)).decode())
        return key

    @property
    def public_key(self) -> str:
        return base64.b64encode(bytes(self._key.public_key)).decode()

    def unseal(self, ciphertext: str) -> dict[str, Any]:
        try:
            raw = SealedBox(self._key).decrypt(base64.b64decode(ciphertext))
            data = json.loads(raw)
        except (CryptoError, ValueError, TypeError) as e:
            raise InstallError("Couldn't decrypt the sign-in; it was sealed for a different Mac key.") from e
        if not isinstance(data, dict):
            raise InstallError("The sign-in payload is malformed.")
        return data

    def install_all(self, credentials: Sequence[Credential]) -> list[Ack]:
        return [self.install(c) for c in credentials]

    def install(self, cred: Credential) -> Ack:
        try:
            data = self.unseal(cred.ciphertext)
            backend = str(data.get("backend") or "")
            if backend != cred.backend or backend not in BACKEND_NAMES:
                raise InstallError("The sign-in is for a different AI service than expected.")
            self._write(backend, str(data.get("kind") or ""), str(data.get("value") or ""))
            problem = self._verify(backend)
            if problem:
                raise InstallError(problem)
            self._pool.reset(backend)
            return Ack(cred.id, True)
        except InstallError as e:
            return Ack(cred.id, False, str(e))
        except Exception as e:  # noqa: BLE001 - never echo the payload
            return Ack(cred.id, False, f"Couldn't install the sign-in ({e.__class__.__name__}).")

    def _write(self, backend: str, kind: str, value: str) -> None:
        if backend == "claude":
            if kind != "oauth_token" or not TOKEN_RE.match(value):
                raise InstallError("That isn't a Claude subscription token (it should start with sk-ant-oat01-).")
            self._keychain.set(TOKEN_ITEM, value)
            return
        if kind != "auth_json" or len(value) > MAX_AUTH_JSON:
            raise InstallError(f"The {backend} sign-in isn't an auth.json file.")
        try:
            parsed = json.loads(value)
        except ValueError as e:
            raise InstallError(f"The {backend} sign-in isn't valid JSON.") from e
        if not isinstance(parsed, dict) or not parsed:
            raise InstallError(f"The {backend} sign-in is empty.")
        write_private(self._homes[backend] / "auth.json", value)

    def _verify(self, backend: str) -> str | None:
        target = self._pool.get(backend)
        return target.verify() if target is not None else None


def parse_credentials(items: Sequence[Mapping[str, Any]] | None) -> tuple[Credential, ...]:
    out = []
    for item in items or []:
        try:
            out.append(Credential(str(item["id"]), str(item["backend"]), str(item["ciphertext"])))
        except (KeyError, TypeError):
            continue
    return tuple(out)
