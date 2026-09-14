"""Configuration loading: credentials from an env file, markets from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECRETS = REPO_ROOT / "config" / "secrets.env"
DEFAULT_MARKETS = REPO_ROOT / "config" / "markets.yaml"
DEFAULT_DB = REPO_ROOT / "data" / "kalshi.sqlite"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"


@dataclass
class Credentials:
    key_id: str
    private_key_path: Path
    env: str = "prod"


def load_env_file(path: str | Path = DEFAULT_SECRETS) -> dict[str, str]:
    """Read a KEY=VALUE file. Real environment variables win over the file, so
    a one-off run can override without editing anything on disk."""
    values: dict[str, str] = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_ENV"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def load_credentials(path: str | Path = DEFAULT_SECRETS) -> Credentials:
    values = load_env_file(path)
    missing = [k for k in ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH") if not values.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}. Copy config/secrets.env.example to "
            "config/secrets.env and fill it in, or set the variables in the environment."
        )
    env = values.get("KALSHI_ENV", "prod").lower()
    if env not in ("prod", "demo"):
        raise ValueError(f"KALSHI_ENV must be 'prod' or 'demo', got {env!r}")
    return Credentials(
        key_id=values["KALSHI_KEY_ID"],
        private_key_path=Path(values["KALSHI_PRIVATE_KEY_PATH"]),
        env=env,
    )


@dataclass
class LinkedSet:
    """A group of markets we believe are economically tied to each other.

    `relation` names the no-arbitrage rule that should hold across them. The
    scanner in kmm/scanner.py dispatches on this string. Phase 1 fills these in.
    """

    name: str
    relation: str
    tickers: list[str]
    notes: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class CollectorConfig:
    tickers: list[str]
    linked_sets: list[LinkedSet] = field(default_factory=list)
    tier: str = "basic"
    rest_poll_seconds: float = 60.0
    db_path: Path = DEFAULT_DB
    raw_dir: Path = DEFAULT_RAW_DIR
    record_raw: bool = True

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_MARKETS) -> "CollectorConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"No market config at {path}. Copy config/markets.example.yaml to "
                "config/markets.yaml and edit the ticker list."
            )
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        linked = [
            LinkedSet(
                name=item["name"],
                relation=item["relation"],
                tickers=list(item["tickers"]),
                notes=item.get("notes", ""),
                params=item.get("params", {}) or {},
            )
            for item in doc.get("linked_sets", []) or []
        ]

        # Every ticker named in a linked set is collected, plus any extras listed
        # at the top level. Deduplicated, order preserved for readable logs.
        tickers: list[str] = []
        for t in list(doc.get("tickers", []) or []) + [t for s in linked for t in s.tickers]:
            if t not in tickers:
                tickers.append(t)
        if not tickers:
            raise ValueError(f"{path} lists no tickers to collect.")

        collection = doc.get("collection", {}) or {}
        return cls(
            tickers=tickers,
            linked_sets=linked,
            tier=collection.get("tier", "basic"),
            rest_poll_seconds=float(collection.get("rest_poll_seconds", 60.0)),
            db_path=Path(collection.get("db_path", DEFAULT_DB)),
            raw_dir=Path(collection.get("raw_dir", DEFAULT_RAW_DIR)),
            record_raw=bool(collection.get("record_raw", True)),
        )
