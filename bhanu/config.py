"""Configuration for the Bhanu Indian Day Trader.

Paper trading is the default and can never be switched on by accident:
``paper`` defaults to ``True`` here, and nothing else in this package
places a real order. A JSON file may override the numeric limits, but
the default configuration is always a paper configuration.

Defaults (INR):
    budget            100_000
    max_loss_per_trade    500
    max_daily_loss      1_500
    max_trades_per_day      3
    min_rr                1.5
    position_cap_pct     0.75   (at most 75% of the budget in one position)
    cash_buffer_pct      0.10   (keep 10% of the budget as cash)
    max_order          80_000
    min_order             500
    orb_minutes            15   (09:15-09:30 IST opening range)
    flat_by           "15:15"  (square off by 15:15 IST, intraday only)
    target_rr             2.0
    atr_stop_k            1.5   (stop = 1.5 x 14-bar ATR)
    paper                True
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

# Repo root is the parent of this package directory, so data/config.json
# lives at <repo>/data/config.json regardless of the working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "data" / "config.json"


@dataclass
class Config:
    """Trading limits and session rules. All money values are INR."""

    budget: float = 100_000
    max_loss_per_trade: float = 500
    max_daily_loss: float = 1_500
    max_trades_per_day: int = 3
    min_rr: float = 1.5
    position_cap_pct: float = 0.75
    cash_buffer_pct: float = 0.10
    max_order: float = 80_000
    min_order: float = 500
    orb_minutes: int = 15
    flat_by: str = "15:15"
    target_rr: float = 2.0
    atr_stop_k: float = 1.5
    paper: bool = True

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError("budget must be positive")
        if self.max_loss_per_trade <= 0:
            raise ValueError("max_loss_per_trade must be positive")
        if self.max_daily_loss < self.max_loss_per_trade:
            raise ValueError("max_daily_loss must be >= max_loss_per_trade")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be >= 1")
        if not 0 < self.position_cap_pct <= 1:
            raise ValueError("position_cap_pct must be in (0, 1]")
        if not 0 <= self.cash_buffer_pct < 1:
            raise ValueError("cash_buffer_pct must be in [0, 1)")
        if self.max_order < self.min_order:
            raise ValueError("max_order must be >= min_order")
        if self.min_rr <= 0 or self.target_rr <= 0:
            raise ValueError("min_rr and target_rr must be positive")
        if self.atr_stop_k <= 0:
            raise ValueError("atr_stop_k must be positive")
        if self.orb_minutes <= 0:
            raise ValueError("orb_minutes must be positive")
        # flat_by must look like "HH:MM".
        parts = self.flat_by.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"flat_by must be 'HH:MM', got {self.flat_by!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"flat_by out of range: {self.flat_by!r}")

    @property
    def position_cap(self) -> float:
        """Maximum rupees in a single position."""
        return self.budget * self.position_cap_pct

    @property
    def usable_cash(self) -> float:
        """Budget left after the cash buffer."""
        return self.budget * (1.0 - self.cash_buffer_pct)


def load_config(path: str | Path | None = None) -> Config:
    """Load a Config, applying JSON overrides on top of the defaults.

    Args:
        path: JSON file to read. Defaults to ``data/config.json`` under
            the repo root. A missing file simply means "use defaults";
            unknown keys are ignored so old files stay compatible.

    Returns:
        A Config. ``paper`` is True unless the file explicitly sets it.
    """
    cfg = Config()
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return cfg
    with open(config_path, "r", encoding="utf-8") as fh:
        overrides = json.load(fh)
    if not isinstance(overrides, dict):
        raise ValueError(f"config file must contain a JSON object: {config_path}")
    known = {f.name for f in fields(Config)}
    clean = {k: v for k, v in overrides.items() if k in known}
    return Config(**{**cfg.__dict__, **clean})


def save_config(cfg: Config, path: str | Path | None = None) -> Path:
    """Write a Config to JSON (defaults location if ``path`` is None)."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(cfg.__dict__, fh, indent=2)
    return config_path


if __name__ == "__main__":
    # Offline demo: defaults only, no file read beyond the default path.
    cfg = load_config()
    print(f"paper={cfg.paper} budget={cfg.budget:,.0f} INR")
    print(f"max_loss_per_trade={cfg.max_loss_per_trade:,.0f} "
          f"max_daily_loss={cfg.max_daily_loss:,.0f} "
          f"max_trades_per_day={cfg.max_trades_per_day}")
    print(f"position_cap={cfg.position_cap:,.0f} usable_cash={cfg.usable_cash:,.0f}")
    print(f"ORB={cfg.orb_minutes} min flat_by={cfg.flat_by} "
          f"target_rr={cfg.target_rr} atr_stop_k={cfg.atr_stop_k}")
