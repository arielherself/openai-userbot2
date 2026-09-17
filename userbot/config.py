"""Settings: defaults, then the environment, then the command line."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from .store import DEFAULT_MAX_BYTES

DEFAULT_HARNESS_PROJECT = "~/headless-harness"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Config:
    api_id: int
    api_hash: str
    session: str
    harness_host: str
    harness_port: int
    harness_project: str
    harness_python: str | None
    harness_db: str | None
    harness_spawn: bool
    model: str | None
    db_path: str
    max_db_bytes: int
    status_interval: float
    turn_timeout: float
    log_level: str

    @property
    def project_dir(self) -> str:
        return os.path.expanduser(self.harness_project)

    @classmethod
    def from_args(cls, argv=None) -> Config:
        parser = argparse.ArgumentParser(
            prog="userbot",
            description="A Telegram userbot whose replies come from headless-harness.",
        )
        parser.add_argument(
            "--api-id",
            type=int,
            default=int(_env("TG_API_ID", "0") or 0),
            help="Telegram api_id (env TG_API_ID)",
        )
        parser.add_argument(
            "--api-hash",
            default=_env("TG_API_HASH", ""),
            help="Telegram api_hash (env TG_API_HASH)",
        )
        parser.add_argument(
            "--session",
            default=_env("TG_SESSION", "userbot"),
            help="Telethon session file (env TG_SESSION)",
        )
        parser.add_argument("--harness-host", default=_env("HH_HOST", "127.0.0.1"))
        parser.add_argument(
            "--harness-port", type=int, default=int(_env("HH_PORT", "8765") or 8765)
        )
        parser.add_argument(
            "--harness-project",
            default=_env("HH_PROJECT", DEFAULT_HARNESS_PROJECT),
            help="the headless-harness checkout to run",
        )
        parser.add_argument(
            "--harness-python",
            default=_env("HH_PYTHON"),
            help="interpreter for the harness (default: its .venv)",
        )
        parser.add_argument(
            "--harness-db", default=_env("HH_DB"), help="SQLite file for the harness itself"
        )
        parser.add_argument(
            "--no-spawn",
            action="store_true",
            default=not _env_bool("HH_SPAWN", True),
            help="require a harness already listening",
        )
        parser.add_argument(
            "--model",
            default=_env("HH_MODEL"),
            help="model for new conversations (default: the harness's)",
        )
        parser.add_argument(
            "--db",
            default=_env("USERBOT_DB", "userbot.db"),
            help="where the message -> agent block mappings live",
        )
        parser.add_argument(
            "--max-db-bytes",
            type=int,
            default=int(_env("USERBOT_MAX_DB_BYTES", str(DEFAULT_MAX_BYTES)) or 0),
            help="budget for the mapping store; oldest mappings go first",
        )
        parser.add_argument(
            "--status-interval",
            type=float,
            default=float(_env("USERBOT_STATUS_INTERVAL", "2.0") or 2.0),
            help="minimum seconds between status message edits",
        )
        parser.add_argument(
            "--turn-timeout",
            type=float,
            default=float(_env("USERBOT_TURN_TIMEOUT", "3600") or 3600),
            help="give up on a turn after this many seconds",
        )
        parser.add_argument("--log-level", default=_env("USERBOT_LOG_LEVEL", "INFO"))
        args = parser.parse_args(argv)
        return cls(
            api_id=args.api_id,
            api_hash=args.api_hash,
            session=args.session,
            harness_host=args.harness_host,
            harness_port=args.harness_port,
            harness_project=args.harness_project,
            harness_python=args.harness_python,
            harness_db=args.harness_db,
            harness_spawn=not args.no_spawn,
            model=args.model,
            db_path=args.db,
            max_db_bytes=args.max_db_bytes,
            status_interval=args.status_interval,
            turn_timeout=args.turn_timeout,
            log_level=args.log_level,
        )

    def require_credentials(self) -> None:
        if not self.api_id or not self.api_hash:
            raise SystemExit(
                "Telegram credentials are missing: set TG_API_ID and TG_API_HASH "
                "(from https://my.telegram.org) or pass --api-id and --api-hash."
            )
