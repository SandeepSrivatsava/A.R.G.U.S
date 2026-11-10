"""Settings from ~/.argus/config.toml (written by setup.sh; safe to edit by hand), and secrets from
~/.argus/secrets.env (owner-only; never in the repo, never on a command line)."""
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
    city: str = ""
    timezone: str = ""
    # brain
    model: str = "claude-sonnet-5-5"
    small_model: str = "claude-haiku-5-5"
    web_search: bool = True
    session_idle_minutes: float = 30.0
    max_steps: int = 30
    max_turn_tokens: int = 400_000
    context_tokens: int = 60_000
    history_messages: int = 80
    workspace_dir: str = "~/argus-workspace"
    api_base: str = "https://api.anthropic.com"
    # listening
    followup_seconds: float = 8.0
    conversation_idle_seconds: float = 120.0
    extra_variants: List[str] = field(default_factory=list)
    soft_variants: List[str] = field(default_factory=lambda: ["august", "angus"])
    voice_lock: bool = False
    voice_lock_threshold: float = 0.45
    mic_device: str = ""
    vad_threshold: float = 0.5
    clap: str = "double"            # "double", "single" or "off": clapping wakes Argus up
    # voice
    voice_engine: str = "piper"
    say_voice: str = "Daniel"
    rate: int = 190
    speed: float = 1.0
    greeting: str = "Online and listening."
    # security
    approval_needs_touch_id: bool = True
    pause_when_locked: bool = True
    screen_control: bool = True
    telegram_desktop: bool = False
    blocked_apps: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_APPS))
    blocked_sites: List[str] = field(default_factory=list)
    # telegram
    telegram_enabled: bool = True
    telegram_user_id: int = 0
    # phone + glasses
    phone_enabled: bool = False
    phone_host: str = ""
    phone_port: int = 8765
    glasses_enabled: bool = True
    # built-in scheduled jobs ("" turns one off)
    morning_headlines: str = "0 7 * * *"
    morning_briefing: str = ""

    # ---- paths -------------------------------------------------------------------------------------
    @property
    def home(self) -> Path:
        return argus_home()

    @property
    def workspace(self) -> Path:
        return Path(os.path.expanduser(self.workspace_dir))

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def db_path(self) -> Path:
        return self.home / "argus.db"

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.env"

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


# config.toml layout: top-level keys plus these sections. (section, key) -> Settings field.
_TOP = ("name", "user_name", "city", "timezone")
_SECTIONS: Dict[str, Dict[str, str]] = {
    "brain": {"model": "model", "small_model": "small_model", "web_search": "web_search",
              "session_idle_minutes": "session_idle_minutes", "max_steps": "max_steps",
              "max_turn_tokens": "max_turn_tokens", "context_tokens": "context_tokens",
              "history_messages": "history_messages", "workspace": "workspace_dir", "api_base": "api_base"},
    "listening": {k: k for k in ("followup_seconds", "conversation_idle_seconds", "extra_variants", "soft_variants",
                                 "voice_lock", "voice_lock_threshold", "mic_device", "vad_threshold", "clap")},
    "voice": {"engine": "voice_engine", "say_voice": "say_voice", "rate": "rate", "speed": "speed", "greeting": "greeting"},
    "security": {k: k for k in ("approval_needs_touch_id", "pause_when_locked", "screen_control", "telegram_desktop",
                                "blocked_apps", "blocked_sites")},
    "telegram": {"enabled": "telegram_enabled", "user_id": "telegram_user_id"},
    "phone": {"enabled": "phone_enabled", "host": "phone_host", "port": "phone_port"},
    "glasses": {"enabled": "glasses_enabled"},
    "schedule": {"morning_headlines": "morning_headlines", "morning_briefing": "morning_briefing"},
}


def _coerce(name: str, default: Any, value: Any) -> Any:
    """Check a setting's type against its default. Strict on purpose: "false" in quotes is not false."""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false (without quotes)")
        return value
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if isinstance(default, int) and not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number")
        return type(default)(value)
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f'{name} must be a list of words in quotes, like ["one", "two"]')
        return list(value)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text in quotes")
    return value


def _validate(s: "Settings") -> None:
    """Values that would make Argus misbehave quietly are errors instead."""
    from .cron import Cron, CronError
    if s.clap not in ("double", "single", "off"):
        raise ValueError('listening.clap must be "double", "single" or "off"')
    if s.voice_engine not in ("piper", "say"):
        raise ValueError('voice.engine must be "piper" or "say"')
    if s.timezone:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(s.timezone)
        except Exception:
            raise ValueError(f"unknown time zone {s.timezone!r} (use a name like America/Los_Angeles)") from None
    for name, cron in (("schedule.morning_headlines", s.morning_headlines),
                       ("schedule.morning_briefing", s.morning_briefing)):
        if cron:
            try:
                Cron(cron)
            except CronError as exc:
                raise ValueError(f"{name}: {exc}") from None
    checks = (("listening.followup_seconds", s.followup_seconds >= 0),
              ("listening.conversation_idle_seconds", s.conversation_idle_seconds >= 0),
              ("listening.voice_lock_threshold", 0 <= s.voice_lock_threshold <= 1),
              ("listening.vad_threshold", 0 < s.vad_threshold < 1),
              ("brain.max_steps", s.max_steps >= 1), ("brain.max_turn_tokens", s.max_turn_tokens >= 1000),
              ("brain.context_tokens", s.context_tokens >= 1000), ("brain.history_messages", s.history_messages >= 2),
              ("brain.session_idle_minutes", s.session_idle_minutes > 0),
              ("voice.rate", s.rate > 0), ("voice.speed", s.speed > 0),
              ("phone.port", 1 <= s.phone_port <= 65535), ("telegram.user_id", s.telegram_user_id >= 0))
    for name, ok in checks:
        if not ok:
            raise ValueError(f"{name} is out of range")


def load(path: Path = None) -> Settings:
    path = Path(path) if path else argus_home() / "config.toml"
    settings = Settings()
    if not path.exists():
        return settings
    with path.open("rb") as fh:
        data: Dict[str, Any] = tomllib.load(fh)
    unknown = [k for k in data if k not in _TOP and k not in _SECTIONS]
    if unknown:  # a typo in a security section must not be silently ignored
        raise ValueError(f"unknown setting(s) in {path}: {', '.join(unknown)}")
    for key in _TOP:
        if key in data:
            setattr(settings, key, _coerce(key, "", data[key]))
    known = {f.name for f in fields(Settings)}
    for section, mapping in _SECTIONS.items():
        table = data.get(section) or {}
        if not isinstance(table, dict):
            raise ValueError(f"[{section}] in {path} must be a section")
        for raw_key, value in table.items():
            target = mapping.get(raw_key)
            if target is None or target not in known:
                raise ValueError(f"unknown setting {section}.{raw_key} in {path}")
            try:
                setattr(settings, target, _coerce(f"{section}.{raw_key}", getattr(settings, target), value))
            except ValueError as exc:
                raise ValueError(f"{exc} (in {path})") from None
    try:
        _validate(settings)
    except ValueError as exc:
        raise ValueError(f"{exc} (in {path})") from None
    return settings


def ensure_private_dirs(settings: Settings) -> None:
    for d in (settings.home, settings.run_dir, settings.audit_path.parent, settings.models_dir):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    settings.workspace.mkdir(parents=True, exist_ok=True)


def read_secrets(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines from the owner-only secrets file. Refuses a file others can read."""
    values: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return values
    if os.stat(p).st_mode & 0o077:
        raise PermissionError(f"{p} can be read by other users; fix with: chmod 600 {p}")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values
