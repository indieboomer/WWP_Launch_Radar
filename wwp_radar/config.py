"""Configuration loaded from environment variables (and an optional .env file).

Only deployment-level settings live here. Runtime settings that users change from
the dashboard (active game, launch timestamp, ...) are stored in the database.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes.

    Existing environment variables win over the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_launch_at(value: str, tz_name: str) -> int | None:
    """Parse WWP_LAUNCH_AT into epoch seconds.

    Accepts ISO dates with a time, e.g. "2026-10-15T14:00", "2026-10-15 14:00",
    "2026-10-15T14:00:00+02:00" or "...Z". Without an offset the value is read in the
    display timezone. Raises ValueError with a readable message otherwise.
    """
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"WWP_LAUNCH_AT={value!r} is not a date and time. Use e.g. WWP_LAUNCH_AT=2026-10-15 14:00 "
            f"(read as {tz_name}) or 2026-10-15T14:00:00+02:00, or leave it empty."
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return int(dt.timestamp())


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WWPLaunchRadar"
    return Path.home() / ".local" / "share" / "wwp-launch-radar"


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int | None = None) -> int:
    value = os.environ.get(name)
    try:
        result = int(value) if value not in (None, "") else default
    except ValueError:
        raise SystemExit(f"Invalid integer for {name}: {value!r}")
    if minimum is not None and result < minimum:
        raise SystemExit(f"{name} must be >= {minimum} (got {result})")
    return result


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    app_id: int = 3222640
    game_name: str = "Wild West Pioneers"
    display_tz: str = "Europe/Warsaw"
    launch_at: str = ""  # ISO timestamp with offset, optional
    ccu_interval: int = 60
    review_interval: int = 120
    collector_enabled: bool = True
    demo: bool = False
    dashboard_password: str = ""
    session_secret: str = ""
    trust_proxy_auth: bool = False
    allow_insecure_remote: bool = False
    http_timeout: float = 20.0
    http_max_retries: int = 4
    steam_max_concurrency: int = 2
    steam_min_request_gap: float = 1.0
    review_page_size: int = 100
    ai_enabled: bool = False
    anthropic_api_key: str = field(default="", repr=False)
    ai_model: str = "claude-opus-5"
    ai_interval: int = 600
    ai_batch_size: int = 120
    twitch_client_id: str = ""
    twitch_client_secret: str = field(default="", repr=False)
    twitch_category: str = ""  # Twitch category name or id; empty = the game name
    twitch_interval: int = 60
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        name = "demo.sqlite3" if self.demo else "radar.sqlite3"
        return self.data_dir / name

    @property
    def is_localhost_bind(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.dashboard_password)

    @property
    def ai_available(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_api_key)

    @property
    def twitch_available(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or Path.cwd() / ".env")
    data_dir = Path(os.environ.get("WWP_DATA_DIR") or _default_data_dir()).expanduser()
    s = Settings(
        data_dir=data_dir,
        host=os.environ.get("WWP_HOST", "127.0.0.1"),
        port=_int("WWP_PORT", 8765, 1),
        app_id=_int("WWP_APP_ID", 3222640, 1),
        game_name=os.environ.get("WWP_GAME_NAME", "Wild West Pioneers"),
        display_tz=os.environ.get("WWP_DISPLAY_TZ", "Europe/Warsaw"),
        launch_at=os.environ.get("WWP_LAUNCH_AT", ""),
        ccu_interval=_int("WWP_CCU_INTERVAL", 60, 15),
        review_interval=_int("WWP_REVIEW_INTERVAL", 120, 30),
        collector_enabled=_bool("WWP_COLLECTOR_ENABLED", True),
        demo=_bool("WWP_DEMO", False),
        dashboard_password=os.environ.get("WWP_DASHBOARD_PASSWORD", ""),
        session_secret=os.environ.get("WWP_SESSION_SECRET", ""),
        trust_proxy_auth=_bool("WWP_TRUST_PROXY_AUTH", False),
        allow_insecure_remote=_bool("WWP_ALLOW_INSECURE_REMOTE", False),
        http_timeout=float(os.environ.get("WWP_HTTP_TIMEOUT", "20")),
        http_max_retries=_int("WWP_HTTP_MAX_RETRIES", 4, 0),
        steam_max_concurrency=_int("WWP_STEAM_MAX_CONCURRENCY", 2, 1),
        steam_min_request_gap=float(os.environ.get("WWP_STEAM_MIN_REQUEST_GAP", "1.0")),
        ai_enabled=_bool("WWP_AI_ENABLED", False),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ai_model=os.environ.get("WWP_AI_MODEL", "claude-opus-5"),
        ai_interval=_int("WWP_AI_INTERVAL", 600, 60),
        ai_batch_size=_int("WWP_AI_BATCH_SIZE", 120, 5),
        twitch_client_id=os.environ.get("WWP_TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=os.environ.get("WWP_TWITCH_CLIENT_SECRET", "").strip(),
        twitch_category=os.environ.get("WWP_TWITCH_CATEGORY", "").strip(),
        twitch_interval=_int("WWP_TWITCH_INTERVAL", 60, 30),
        log_level=os.environ.get("WWP_LOG_LEVEL", "INFO"),
    )
    try:
        ZoneInfo(s.display_tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"Unknown timezone WWP_DISPLAY_TZ={s.display_tz!r} (e.g. Europe/Warsaw)")
    try:
        parse_launch_at(s.launch_at, s.display_tz)
    except ValueError as e:
        raise SystemExit(str(e))
    return s


def validate_exposure(s: Settings) -> None:
    """Refuse to expose an unauthenticated dashboard beyond localhost."""
    if s.is_localhost_bind or s.auth_enabled or s.trust_proxy_auth or s.allow_insecure_remote:
        return
    raise SystemExit(
        f"Refusing to bind to {s.host} without authentication. Set WWP_DASHBOARD_PASSWORD, "
        "or WWP_TRUST_PROXY_AUTH=true when an authenticating reverse proxy sits in front."
    )
