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
PROJECT_CONFIG_VERSION = 1
PROJECT_CONFIG_FIELDS = frozenset({"base_url", "api_key", "model", "timeout_seconds", "vision_enabled"})


class AISettingsError(ValueError):
    """A safe, user-facing settings error without secret material."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CONFIG_INVALID",
        stage: str = "validation",
        technical_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.technical_detail = technical_detail


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
    if not crypt32.CryptProtectData(ctypes.byref(source), "DongJian AI API key", None, None, None, 0, ctypes.byref(protected)):
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


def project_config_path(project_root: Path | str | None = None) -> Path:
    root = Path(project_root or paths.PROJECT_ROOT).resolve()
    return root / "config" / "llm.json"


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


def _config(
    base_url: str,
    model: str,
    timeout: int,
    api_key: str,
    *,
    vision_enabled: bool = False,
) -> SemanticConfig:
    value = SemanticConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
        vision_enabled=vision_enabled,
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
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
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
class ProjectAIConfig:
    """The portable, directly editable project configuration contract."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 120
    vision_enabled: bool = False

    def config(self) -> SemanticConfig:
        return _config(
            self.base_url,
            self.model,
            self.timeout_seconds,
            self.api_key,
            vision_enabled=self.vision_enabled,
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "vision_enabled": self.vision_enabled,
        }


class ProjectAIConfigStore:
    """Atomic JSON store used by the portable product and its Settings UI."""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self.project_root = Path(project_root or paths.PROJECT_ROOT).resolve()
        self.path = project_config_path(self.project_root)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _validate_project_path(self) -> None:
        expected = project_config_path(self.project_root)
        try:
            actual = self.path.resolve(strict=False)
        except OSError as exc:
            raise AISettingsError(
                "project AI configuration path cannot be resolved",
                code="PROJECT_ROOT_MISMATCH",
                stage="prepare",
                technical_detail=type(exc).__name__,
            ) from exc
        if actual != expected:
            raise AISettingsError(
                "project AI configuration path is outside the current project",
                code="PROJECT_ROOT_MISMATCH",
                stage="prepare",
                technical_detail="resolved config path does not match project root",
            )

    def read(self) -> ProjectAIConfig:
        self._validate_project_path()
        try:
            encoded = self.path.read_text(encoding="utf-8-sig")
        except PermissionError as exc:
            raise AISettingsError(
                "project AI configuration cannot be read",
                code="CONFIG_PERMISSION_DENIED",
                stage="read",
                technical_detail=type(exc).__name__,
            ) from exc
        except UnicodeError as exc:
            raise AISettingsError(
                "project AI configuration cannot be read",
                code="CONFIG_INVALID",
                stage="read",
                technical_detail=type(exc).__name__,
            ) from exc
        except OSError as exc:
            code = "CONFIG_DIRECTORY_UNAVAILABLE" if not self.path.parent.exists() else "CONFIG_WRITE_FAILED"
            raise AISettingsError(
                "project AI configuration cannot be read",
                code=code,
                stage="read",
                technical_detail=type(exc).__name__,
            ) from exc
        # A copied portable bundle may contain a zero-byte placeholder while
        # it is being filled in.  Treat that as the documented offline state,
        # just like an object whose contract fields are all blank.
        if not encoded.strip():
            return ProjectAIConfig(base_url="", api_key="", model="")
        try:
            raw = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise AISettingsError(
                "project AI configuration cannot be read",
                code="CONFIG_INVALID",
                stage="read",
                technical_detail=type(exc).__name__,
            ) from exc
        if not isinstance(raw, Mapping):
            raise AISettingsError("project AI configuration must be a JSON object", code="CONFIG_INVALID", stage="validation")
        unknown = set(raw) - PROJECT_CONFIG_FIELDS
        if unknown:
            raise AISettingsError("project AI configuration contains unsupported fields", code="CONFIG_INVALID", stage="validation")
        try:
            base_url = _text(raw.get("base_url", ""), "base URL")
            api_key = _text(raw.get("api_key", ""), "API key")
            model = _text(raw.get("model", ""), "model")
            timeout = _timeout(raw.get("timeout_seconds", 120))
            vision_enabled = raw.get("vision_enabled", False)
            if not isinstance(vision_enabled, bool):
                raise AISettingsError("vision_enabled must be a boolean")
        except AISettingsError:
            raise
        config = ProjectAIConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
            vision_enabled=vision_enabled,
        )
        # Incomplete values are a supported offline state. Complete values
        # still receive the same URL and bound validation as legacy settings.
        try:
            config.config()
        except ValueError as exc:
            raise AISettingsError(str(exc)) from exc
        return config

    def _write(self, value: ProjectAIConfig) -> None:
        self._validate_project_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise AISettingsError(
                "project AI configuration directory is not writable",
                code="CONFIG_PERMISSION_DENIED",
                stage="directory",
                technical_detail=type(exc).__name__,
            ) from exc
        except OSError as exc:
            raise AISettingsError(
                "project AI configuration directory is unavailable",
                code="CONFIG_DIRECTORY_UNAVAILABLE",
                stage="directory",
                technical_detail=type(exc).__name__,
            ) from exc
        temporary: Path | None = None
        try:
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
                    json.dump(value.as_mapping(), handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except PermissionError as exc:
                raise AISettingsError(
                    "project AI configuration cannot be written",
                    code="CONFIG_PERMISSION_DENIED",
                    stage="write",
                    technical_detail=type(exc).__name__,
                ) from exc
            except (OSError, UnicodeError, TypeError) as exc:
                raise AISettingsError(
                    "project AI configuration cannot be written",
                    code="CONFIG_WRITE_FAILED",
                    stage="write",
                    technical_detail=type(exc).__name__,
                ) from exc
            try:
                os.replace(temporary, self.path)
            except PermissionError as exc:
                raise AISettingsError(
                    "project AI configuration cannot replace the existing file",
                    code="CONFIG_PERMISSION_DENIED",
                    stage="replace",
                    technical_detail=type(exc).__name__,
                ) from exc
            except OSError as exc:
                raise AISettingsError(
                    "project AI configuration cannot replace the existing file",
                    code="CONFIG_REPLACE_FAILED",
                    stage="replace",
                    technical_detail=type(exc).__name__,
                ) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not hide the write/replace failure.  The
                    # temp file is bounded to the project config directory and
                    # can be removed on the next successful save.
                    pass

    def save(
        self,
        *,
        base_url: object,
        model: object,
        timeout: object,
        vision_enabled: object = False,
        api_key: object | None = None,
        clear_api_key: bool = False,
    ) -> ProjectAIConfig:
        existing = self.read() if self.exists else None
        normalized_base = _text(base_url, "base URL")
        normalized_model = _text(model, "model")
        normalized_timeout = _timeout(timeout)
        if not isinstance(vision_enabled, bool):
            raise AISettingsError("vision_enabled must be a boolean")
        if clear_api_key:
            normalized_key = ""
        elif isinstance(api_key, str) and api_key.strip():
            normalized_key = api_key.strip()
        else:
            normalized_key = existing.api_key if existing is not None else ""
        saved = ProjectAIConfig(
            base_url=normalized_base,
            api_key=normalized_key,
            model=normalized_model,
            timeout_seconds=normalized_timeout,
            vision_enabled=vision_enabled,
        )
        try:
            saved.config()
        except ValueError as exc:
            raise AISettingsError(str(exc)) from exc
        self._write(saved)
        return saved


@dataclass(frozen=True)
class RuntimeAISettings:
    config: SemanticConfig
    source: str
    status: str
    enabled: bool
    api_key_configured: bool
    vision_enabled: bool = False
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
            "visionEnabled": self.vision_enabled,
            "configPath": "config/llm.json",
        }


def load_runtime_ai_settings(project_root: Path | str | None = None) -> RuntimeAISettings:
    root = Path(project_root or paths.PROJECT_ROOT).resolve()
    project_store = ProjectAIConfigStore(root)
    if project_store.exists:
        try:
            project = project_store.read()
            config = project.config()
            has_any_value = bool(config.base_url or config.api_key or config.model)
            return RuntimeAISettings(
                config=config,
                source="project",
                status="CONFIGURED" if config.configured else "INCOMPLETE" if has_any_value else "NOT_CONFIGURED",
                enabled=config.configured,
                api_key_configured=bool(config.api_key),
                vision_enabled=bool(config.configured and config.vision_enabled),
            )
        except AISettingsError as exc:
            return RuntimeAISettings(
                config=SemanticConfig(),
                source="project",
                status="INVALID_CONFIGURATION",
                enabled=False,
                api_key_configured=False,
                vision_enabled=False,
                error=str(exc),
            )
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
                vision_enabled=False,
            )
        except AISettingsError as exc:
            return RuntimeAISettings(
                config=SemanticConfig(),
                source="ui",
                status="INVALID_CONFIGURATION",
                enabled=False,
                api_key_configured=False,
                vision_enabled=False,
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
        source="env" if (root / ".env").is_file() else "offline",
        status="CONFIGURED" if config.configured else "NOT_CONFIGURED",
        # The advanced .env fallback is explicitly supplied configuration and
        # remains compatible with the existing authorized CLI/API path.
        enabled=config.configured,
        api_key_configured=bool(config.api_key),
        vision_enabled=False,
    )


def public_saved_settings(project_root: Path | str | None = None) -> dict[str, object]:
    return load_runtime_ai_settings(project_root).public()
