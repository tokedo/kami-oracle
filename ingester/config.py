"""Configuration loader.

Reads ``.env`` from the repo root (if present) and exposes typed accessors.
Defaults mirror ``env.template`` so tests can run without a ``.env`` file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    rpc_url: str
    db_path: Path
    window_days: int
    poll_interval_s: float
    log_level: str
    api_token: str | None
    prune_interval_s: float
    rate_limit_per_min: int

    @property
    def world_address(self) -> str:
        return "0x2729174c265dbBd8416C6449E0E813E88f43D0E7"

    @property
    def abi_dir(self) -> Path:
        return REPO_ROOT / "kami_context" / "abi"

    @property
    def vendor_sha_path(self) -> Path:
        return REPO_ROOT / "kami_context" / "UPSTREAM_SHA"


def load_config() -> Config:
    load_dotenv(REPO_ROOT / ".env", override=False)

    rpc_url = os.environ.get(
        "YOMINET_RPC_URL",
        "https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz",
    )
    db_path_str = os.environ.get("KAMI_ORACLE_DB_PATH", "db/kami-oracle.duckdb")
    db_path = Path(db_path_str)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    api_token_raw = os.environ.get("KAMI_ORACLE_API_TOKEN", "").strip()
    api_token = api_token_raw or None

    return Config(
        rpc_url=rpc_url,
        db_path=db_path,
        window_days=int(os.environ.get("KAMI_ORACLE_WINDOW_DAYS", "7")),
        poll_interval_s=float(os.environ.get("KAMI_ORACLE_POLL_INTERVAL_S", "3")),
        log_level=os.environ.get("KAMI_ORACLE_LOG_LEVEL", "INFO").upper(),
        api_token=api_token,
        prune_interval_s=float(os.environ.get("KAMI_ORACLE_PRUNE_INTERVAL_S", "3600")),
        rate_limit_per_min=int(os.environ.get("KAMI_ORACLE_RATE_LIMIT_PER_MIN", "60")),
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
