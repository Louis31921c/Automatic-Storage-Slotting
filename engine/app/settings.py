from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WH_")

    db_path: Path = ROOT / "data" / "warehouse.db"
    run_dir: Path = ROOT / "data" / "run"
    storage_json: Path = ROOT / "data" / "storage_spaces.json"
    watcher_stale_after_s: float = 30.0
    sse_ping_s: float = 15.0

    # slotting policy (PRD 4)
    slot_buildings: list[int] = [1, 2, 3, 4]      # pilot: set WH_SLOT_BUILDINGS='[2]'
    overhang_m: float = 0.05                      # per side, footprint only
    reservation_min: float = 10.0
    default_dwell_days: float = 14.0
    short_dwell_days: float = 7.0
    ship_slots: int = 40
    # several units per case (PRD 12): bins + shelving share space; pallet positions hold one pallet
    shared_storage_types: list[str] = ["bin", "parcel"]
    fill_factor: float = 0.8                      # usable share of a shared case's volume
    max_skus_per_case: int = 3

    # receiving: a delivery-note line = goods physically in; assign + book a case right away
    # (spreadsheet mode, no tablet confirmation). False = tablet drop-off flow only.
    auto_place_on_receipt: bool = True
    fresh_hours: float = 24.0                     # "freshly received" window in the status spreadsheet

    # status spreadsheet (separate exporter, never in the ingest path)
    export_enabled: bool = True
    export_path: Path = ROOT / "data" / "out" / "warehouse_status.xlsx"
    export_debounce_s: float = 3.0

    # time (PRD C6): stored UTC, shown Europe/Rome
    tz: str = "Europe/Rome"
    default_ship_hour: int = 8                    # ship date without time -> 08:00 local
    pre_ship_release_h: float = 24.0
    scheduler_interval_s: float = 900.0           # 15 min
    rename_grace_s: float = 120.0                 # file deleted -> cancel its rows after this (rename = delete+create)


settings = Settings()
