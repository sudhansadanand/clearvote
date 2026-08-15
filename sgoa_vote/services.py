"""Process-wide wiring: configuration, the database, and the AGM HMAC key."""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .db.connections import Database
from .domain.credentials import load_or_create_key


class Services:
    def __init__(self, config: Config | None = None, data_dir: str | Path | None = None):
        self.config = config or Config.load()
        if data_dir is not None:
            self.config.data_dir = str(data_dir)
        self.data_path = Path(self.config.data_dir)
        self.db = Database(self.data_path)
        self.agm_key = load_or_create_key(self.data_path / "agm_key")

    @property
    def export_path(self) -> Path:
        return Path(self.config.export_dir)

    @property
    def backup_path(self) -> Path:
        return Path(self.config.backup_dir)

    def close(self) -> None:
        self.db.close()
