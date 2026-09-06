"""Local encryption key management for write-only provider credentials."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(RuntimeError):
    pass


class CredentialVault:
    def __init__(self) -> None:
        configured = os.getenv("AGENTCC_VAULT_KEY")
        if configured:
            key = configured.encode("utf-8")
        else:
            data_dir = Path(os.getenv("AGENTCC_DATA_DIR", "/data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            key_path = data_dir / "vault.key"
            if key_path.exists():
                key = key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                key_path.write_bytes(key + b"\n")
                key_path.chmod(0o600)
        try:
            self._fernet = Fernet(key)
        except ValueError as error:
            raise VaultError("AGENTCC_VAULT_KEY is not a valid Fernet key") from error

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted: str) -> str:
        try:
            return self._fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken as error:
            raise VaultError("credential cannot be decrypted with the current vault key") from error


vault = CredentialVault()
