"""Settings from ~/.argus/config.toml (written by setup.sh; safe to edit by hand)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

# Password managers, the keychain and System Settings: Argus may never look at or click in these.
DEFAULT_BLOCKED_APPS = [
    "com.1password.1password", "com.agilebits.onepassword7", "com.agilebits.onepassword-osx", "1Password",
    "com.bitwarden.desktop", "Bitwarden",
    "com.apple.Passwords", "Passwords",
    "com.apple.keychainaccess", "Keychain Access",
    "org.keepassxc.keepassxc", "KeePassXC",
    "in.sinew.Enpass-Desktop", "Enpass",
    "com.lastpass.LastPass", "LastPass",
    "com.apple.systempreferences", "com.apple.Settings", "System Settings", "System Preferences",
]


def argus_home() -> Path:
    return Path(os.environ.get("ARGUS_HOME") or "~/.argus").expanduser()


@dataclass
class Settings:
    name: str = "Argus"
    user_name: str = ""
    # listening
    followup_seconds: float = 8.0
    conversation_idle_seconds: float = 120.0
    extra_variants: List[str] = field(default_factory=list)
    soft_variants: List[str] = field(default_factory=lambda: ["august", "angus"])
    voice_lock: bool = False
    voice_lock_threshold: float = 0.45
    mic_device: str = ""
    vad_threshold: float = 0.5
    # voice
    voice_engine: str = "piper"
    say_voice: str = "Daniel"
    rate: int = 190
    speed: float = 1.0
    greeting: str = "Online and listening."
    # brain
    session_idle_minutes: float = 30.0
    hermes_env: str = ""            # default: $HERMES_HOME/.env, else ~/.hermes/.env
    # security
    approval_needs_touch_id: bool = True
    pause_when_locked: bool = True
    blocked_apps: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_APPS))
    blocked_sites: List[str] = field(default_factory=list)
    # phone
    phone_enabled: bool = False
    phone_host: str = ""
    phone_port: int = 8765

    # ---- paths -------------------------------------------------------------------------------------
    @property
    def home(self) -> Path:
        return argus_home()

    @property
    def hermes_env_file(self) -> str:
        if self.hermes_env:
            return self.hermes_env
        return str(Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser() / ".env")

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def audit_path(self) -> Path:
        return self.home / "logs" / "audit.jsonl"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "argus.sock"

    @property
    def voiceprint_path(self) -> Path:
        return self.home / "voiceprint.npy"

    @property
    def phone_token_path(self) -> Path:
        return self.home / "phone_token"

    @property
    def learned_variants_path(self) -> Path:
        return self.home / "learned_names.txt"

    def all_variants(self) -> List[str]:
        learned: List[str] = []
        if self.learned_variants_path.exists():
            learned = [w.strip().lower() for w in self.learned_variants_path.read_text().split() if w.strip()]
        return [v.lower() for v in self.extra_variants] + learned


_SECTIONS = {
    "listening": ("followup_seconds", "conversation_idle_seconds", "extra_variants", "soft_variants", "voice_lock",
                  "voice_lock_threshold", "mic_device", "vad_threshold"),
    "voice": ("voice_engine", "say_voice", "rate", "speed", "greeting"),
    "brain": ("session_idle_minutes", "hermes_env"),
    "security": ("approval_needs_touch_id", "pause_when_locked", "blocked_apps", "blocked_sites"),
    "phone": ("phone_enabled", "phone_host", "phone_port"),
}
_KEY_ALIASES = {("voice", "engine"): "voice_engine", ("phone", "enabled"): "phone_enabled",
                ("phone", "host"): "phone_host", ("phone", "port"): "phone_port"}


def load(path: Path = None) -> Settings:
    path = Path(path) if path else argus_home() / "config.toml"
    settings = Settings()
    if not path.exists():
        return settings
    with path.open("rb") as fh:
        data: Dict[str, Any] = tomllib.load(fh)
    known = {f.name: f for f in fields(Settings)}
    for key in ("name", "user_name"):
        if key in data:
            setattr(settings, key, data[key])
    for section, keys in _SECTIONS.items():
        table = data.get(section) or {}
        for raw_key, value in table.items():
            key = _KEY_ALIASES.get((section, raw_key), raw_key)
            if key in keys and key in known:
                default = getattr(settings, key)
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, float):
                    value = float(value)
                elif isinstance(default, int):
                    value = int(value)
                elif isinstance(default, list):
                    value = [str(v) for v in value]
                setattr(settings, key, value)
    if section_missing := [s for s in data if s not in _SECTIONS and s not in ("name", "user_name")]:
        raise ValueError(f"unknown section(s) in {path}: {', '.join(section_missing)}")
    return settings


def ensure_private_dirs(settings: Settings) -> None:
    for d in (settings.home, settings.run_dir, settings.audit_path.parent, settings.models_dir):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
