"""Runtime configuration and the frozen AGM configuration hash."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_VERSION = "1.0.0"


def canonical_json(payload) -> str:
    """Sorted keys, no incidental whitespace. Used everywhere a hash is taken."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Config:
    # --- deployment -------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: str = "data"
    export_dir: str = "export"
    backup_dir: str = "backups"
    cookie_secure: bool = False  # flip to True the day TLS is terminated in front

    # --- governance (all reviewable before production use) ----------------
    max_proxies_per_attendee: int = 5
    default_voting_rule: str = "FOR_GT_AGAINST"
    chair_casting_vote_enabled: bool = False
    eligible_pool_id: str = "default"

    # The result reaches the hall the instant the count is made, rather than
    # waiting for the MC to release it. Closes the window in which the MC alone
    # knows the outcome -- see README, "Publishing the result".
    auto_publish_results: bool = True

    # --- sessions and rate limiting ---------------------------------------
    voter_session_hours: int = 12
    sessions_per_credential: int = 1
    operator_inactivity_minutes: int = 15
    join_rate_limit_attempts: int = 5
    join_rate_limit_window_seconds: int = 60
    join_rate_limit_cooldown_seconds: int = 60

    # --- presentation -----------------------------------------------------
    display_timezone: str = "Asia/Kolkata"
    poll_interval_ms: int = 2000

    association_name: str = "Shanti Gulmohar Owners Association"
    extra: dict = field(default_factory=dict)

    # -- governance parameters that belong in the final report -------------
    GOVERNANCE_KEYS = (
        "max_proxies_per_attendee",
        "default_voting_rule",
        "chair_casting_vote_enabled",
        "eligible_pool_id",
        "sessions_per_credential",
        "auto_publish_results",
    )

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        candidate = Path(path) if path else Path("config.json")
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            known = {f for f in cfg.__dataclass_fields__}
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)
                else:
                    cfg.extra[key] = value
        return cfg

    def as_dict(self) -> dict:
        return asdict(self)

    def governance(self) -> dict:
        return {k: getattr(self, k) for k in self.GOVERNANCE_KEYS}

    def config_hash(self) -> str:
        """Hash of the settings that affect voting outcomes.

        Deliberately excludes host/port/paths so that moving the same frozen
        configuration from a test laptop to the AGM machine does not change the
        hash scrutineers recorded.
        """
        material = {
            "version": APP_VERSION,
            "governance": self.governance(),
            "voter_session_hours": self.voter_session_hours,
            "join_rate_limit_attempts": self.join_rate_limit_attempts,
            "join_rate_limit_window_seconds": self.join_rate_limit_window_seconds,
            "association_name": self.association_name,
        }
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
