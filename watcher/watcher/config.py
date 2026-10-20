from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "live_sync.yaml"


class WatchPath(BaseModel):
    name: str
    path: Path
    mode: Literal["event", "poll"] = "event"
    poll_interval_s: float = 30.0
    enabled_types: list[str] = []


class LiveSyncConfig(BaseModel):
    debounce_s: float = 2.0
    heartbeat_s: float = 10.0
    dead_letter_path: Path
    engine_url: str
    watch: list[WatchPath]


def load_config(path: Path = DEFAULT_CONFIG) -> LiveSyncConfig:
    cfg = LiveSyncConfig.model_validate(yaml.safe_load(path.read_text()))
    cfg.dead_letter_path = (ROOT / cfg.dead_letter_path).resolve()
    for w in cfg.watch:
        w.path = (ROOT / w.path).resolve()
    return cfg
