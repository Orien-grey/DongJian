"""Project-local AI settings with Windows CurrentUser DPAPI protection."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .. import paths
from .config import SemanticConfig


SETTINGS_VERSION = 1
SETTINGS_FILE_NAME = "llm-settings.json"


class AISettingsError(ValueError):
    """A safe, user-facing settings error without secret material."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_functions():
    if os.name != "nt":
        raise AISettingsError("Windows DPAPI is unavailable on this platform")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError as exc:
        raise AISettingsError("Windows DPAPI is unavailable") from exc
    return crypt32, kernel32


def _protect(value: str) -> str:
    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    protected = _DataBlob()
    crypt32, kernel32 = _dpapi_functions()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(ctypes.byref(source), "ChongZu AI API key", None, None, None, 0, ctypes.byref(protected)):
        raise AISettingsError("Windows could not protect the AI API key")
    try:
        encrypted = ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)
    return base64.b64encode(encrypted).decode("ascii")


def _unprotect(encoded: str) -> str:
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise AISettingsError("saved AI settings are invalid") from exc
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    unprotected = _DataBlob()
    crypt32, kernel32 = _dpapi_functions()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(unprotected),
    ):
        raise AISettingsError("saved AI settings cannot be unlocked for this Windows user")
    try:
        result = ctypes.string_at(unprotected.pbData, unprotected.cbData).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise AISettingsError("saved AI settings contain an invalid API key") from exc
    finally:
        kernel32.LocalFree(unprotected.pbData)
        if description:
            kernel32.LocalFree(description)
    return result


def settings_path(project_root: Path | str | None = None) -> Path:
    root = Path(project_root or paths.PROJECT_ROOT).resolve()
    return root / "workspace" / "state" / SETTINGS_FILE_NAME


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise AISettingsError(f"{field} must be text")
    return value.strip()


def _timeout(value: object) -> int:
    if isinstance(value, bool):
        raise AISettingsError("timeout must be a whole number")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AISettingsError("timeout must be a whole number") from exc
    if parsed < 1 or parsed > 600:
        raise AISettingsError("timeout must be between 1 and 600 seconds")
    return parsed


def _config(base_url: str, model: str, timeout: int, api_key: str) -> SemanticConfig:
    value = SemanticConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
    )
    if value.configured:
        try:
            value.validate_for_use()
        except ValueError as exc:
            raise AISettingsError(str(exc)) from exc
    return value


def _fingerprint(*, base_url: str, model: str, timeout: int, encrypted_api_key: str | None) -> str:
    payload = json.dumps(
        {
            "baseUrl": base_url,
            "model": model,
            "timeout": timeout,
            "encryptedApiKey": encrypted_api_key or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class StoredAISettings:
    base_url: str
    model: str
    timeout: int
    encrypted_api_key: str | None
    enabled: bool = False
    tested_fingerprint: str | None = None
    last_test_status: str | None = None
    last_test_fingerprint: str | None = None

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            base_url=self.base_url,
            model=self.model,
            timeout=self.timeout,
            encrypted_api_key=self.encrypted_api_key,
        )

    def config(self) -> SemanticConfig:
        api_key = _unprotect(self.encrypted_api_key) if self.encrypted_api_key else ""
        return _config(self.base_url, self.model, self.timeout, api_key)

    def as_mapping(self) -> dict[str, object]:
        return {
            "version": SETTINGS_VERSION,
            "baseUrl": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "encryptedApiKey": self.encrypted_api_key,
            "enabled": bool(self.enabled),
            "testedFingerprint": self.tested_fingerprint,
            "lastTestStatus": self.last_test_status,
            "lastTestFingerprint": self.last_test_fingerprint,
        }


class AISettingsStore:
    """Atomic JSON metadata store; only the encrypted key is persisted."""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self.project_root = Path(project_root or paths.PROJECT_ROOT).resolve()
        self.path = settings_path(self.project_root)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> StoredAISettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AISettingsError("saved AI settings cannot be read") from exc
        if not isinstance(raw, Mapping):
            raise AISettingsError("saved AI settings have an unsupported version")
        try:
            version = int(raw.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise AISettingsError("saved AI settings have an unsupported version") from exc
        if version != SETTINGS_VERSION:
            raise AISettingsError("saved AI settings have an unsupported version")
        encrypted = raw.get("encryptedApiKey")
        if encrypted is not None and (not isinstance(encrypted, str) or not encrypted):
            raise AISettingsError("saved AI settings contain an invalid API key record")
        return StoredAISettings(
            base_url=_text(raw.get("baseUrl", ""), "base URL"),
            model=_text(raw.get("model", ""), "model"),
            timeout=_timeout(raw.get("timeout", 60)),
            encrypted_api_key=encrypted,
            enabled=bool(raw.get("enabled", False)),
            tested_fingerprint=raw.get("testedFingerprint") if isinstance(raw.get("testedFingerprint"), str) else None,
            last_test_status=raw.get("lastTestStatus") if isinstance(raw.get("lastTestStatus"), str) else None,
            last_test_fingerprint=raw.get("lastTestFingerprint") if isinstance(raw.get("lastTestFingerprint"), str) else None,
        )

    def _write(self, settings: StoredAISettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f"{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(settings.as_mapping(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(
        self,
        *,
        base_url: object,
        model: object,
        timeout: object,
        api_key: object | None = None,
        clear_api_key: bool = False,
    ) -> StoredAISettings:
        existing = self.read() if self.exists else None
        normalized_base = _text(base_url, "base URL")
        normalized_model = _text(model, "model")
        normalized_timeout = _timeout(timeout)
        if clear_api_key:
            encrypted = None
        elif isinstance(api_key, str) and api_key.strip():
            encrypted = _protect(api_key.strip())
        else:
            encrypted = existing.encrypted_api_key if existing is not None else None
        # Validate complete configurations at save time, but permit an
        # intentionally incomplete offline form to be stored as disabled.
        api_key_value = _unprotect(encrypted) if encrypted else ""
        _config(normalized_base, normalized_model, normalized_timeout, api_key_value)
        saved = StoredAISettings(
            base_url=normalized_base,
            model=normalized_model,
            timeout=normalized_timeout,
            encrypted_api_key=encrypted,
            enabled=False,
            tested_fingerprint=None,
            last_test_status=None,
            last_test_fingerprint=None,
        )
        self._write(saved)
        return saved

    def mark_test_success(self) -> StoredAISettings:
        settings = self.read()
        config = settings.config()
        config.validate_for_use()
        updated = StoredAISettings(
            **{
                **settings.__dict__,
                "enabled": True,
                "tested_fingerprint": settings.fingerprint,
                "last_test_status": "success",
                "last_test_fingerprint": settings.fingerprint,
            }
        )
        self._write(updated)
        return updated

    def mark_test_failure(self) -> StoredAISettings:
        settings = self.read()
        updated = StoredAISettings(
            **{
                **settings.__dict__,
                "enabled": False,
                "tested_fingerprint": None,
                "last_test_status": "failed",
                "last_test_fingerprint": settings.fingerprint,
            }
        )
        self._write(updated)
        return updated


@dataclass(frozen=True)
class RuntimeAISettings:
    config: SemanticConfig
    source: str
    status: str
    enabled: bool
    api_key_configured: bool
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.config.configured

    def public(self) -> dict[str, object]:
        return {
            "baseUrl": self.config.base_url,
            "model": self.config.model,
            "timeout": self.config.timeout_seconds,
            "apiKeyConfigured": self.api_key_configured,
            "source": self.source,
            "status": self.status,
            "configured": self.configured,
            "enabled": self.enabled,
        }


def load_runtime_ai_settings(project_root: Path | str | None = None) -> RuntimeAISettings:
    root = Path(project_root or paths.PROJECT_ROOT).resolve()
    store = AISettingsStore(root)
    if store.exists:
        try:
            saved = store.read()
            config = saved.config()
            if not config.configured:
                status = "INCOMPLETE"
                enabled = False
            elif saved.enabled and saved.tested_fingerprint == saved.fingerprint:
                status = "ENABLED"
                enabled = True
            elif saved.last_test_status == "failed" and saved.last_test_fingerprint == saved.fingerprint:
                status = "CONNECTION_FAILED"
                enabled = False
            else:
                status = "UNVERIFIED"
                enabled = False
            return RuntimeAISettings(
                config=config,
                source="ui",
                status=status,
                enabled=enabled,
                api_key_configured=bool(saved.encrypted_api_key),
            )
        except AISettingsError as exc:
            return RuntimeAISettings(
                config=SemanticConfig(),
                source="ui",
                status="INVALID_CONFIGURATION",
                enabled=False,
                api_key_configured=False,
                error=str(exc),
            )
    try:
        config = SemanticConfig.from_env_file(root / ".env")
    except Exception as exc:  # no .env value is echoed
        return RuntimeAISettings(
            config=SemanticConfig(),
            source="env",
            status="INVALID_CONFIGURATION",
            enabled=False,
            api_key_configured=False,
            error=str(exc),
        )
    return RuntimeAISettings(
        config=config,
        source="env",
        status="CONFIGURED" if config.configured else "NOT_CONFIGURED",
        # The advanced .env fallback is explicitly supplied configuration and
        # remains compatible with the existing authorized CLI/API path.
        enabled=config.configured,
        api_key_configured=bool(config.api_key),
    )


def public_saved_settings(project_root: Path | str | None = None) -> dict[str, object]:
    return load_runtime_ai_settings(project_root).public()
