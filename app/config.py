"""Loading and validation of the two YAML config files.

Everything the user can tune lives in config/config.yml; everything that breaks
when LinkedIn reshuffles its markup lives in config/selectors.yml. Keeping them
apart means a DOM fix never risks clobbering someone's filter lists.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

# Where the config lives inside the container. Overridable mostly so the unit
# tests can point somewhere else.
CONFIG_DIR = Path(os.environ.get("LINKEDINPD_CONFIG_DIR", "/app/config"))

VALID_MODES = ("all", "except", "stalk")


@dataclass
class Filters:
    mode: str = "except"
    poster_allow: list[str] = field(default_factory=list)
    poster_block: list[str] = field(default_factory=list)
    liker_allow: list[str] = field(default_factory=list)
    liker_block: list[str] = field(default_factory=list)
    skip_suggested: bool = True


@dataclass
class Pacing:
    min_seconds_between_likes: int = 300
    max_seconds_between_likes: int = 600
    max_likes_per_day: int = 40
    max_scrolls_per_pass: int = 25
    min_seconds_between_passes: int = 900
    max_seconds_between_passes: int = 1800


@dataclass
class Browser:
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-US"
    start_paused: bool = False


@dataclass
class Config:
    filters: Filters
    pacing: Pacing
    browser: Browser
    selectors: dict


def _only_known_keys(raw: dict, cls) -> dict:
    """Drop keys the dataclass does not know about.

    A typo in the YAML should not blow up at startup and leave the user staring
    at a stack trace; it just falls back to the default for that field.
    """
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return {k: v for k, v in (raw or {}).items() if k in known}


def _as_name_list(value) -> list[str]:
    """Normalise a YAML name list: strip blanks, lowercase for matching later."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def parse(raw: dict, selectors: dict) -> Config:
    """Turn the raw YAML mapping into a validated Config.

    Split out from load() so the dashboard can validate what the user typed
    before anything is written to disk.
    """
    filters = Filters(**_only_known_keys(raw.get("filters"), Filters))
    for name in ("poster_allow", "poster_block", "liker_allow", "liker_block"):
        setattr(filters, name, _as_name_list(getattr(filters, name)))

    if filters.mode not in VALID_MODES:
        raise ValueError(
            f"filters.mode is {filters.mode!r}, expected one of {VALID_MODES}"
        )
    # A stalk list that is empty would silently like nothing forever, which
    # looks exactly like a broken engine. Catch it here instead.
    if filters.mode == "stalk" and not (filters.poster_allow or filters.liker_allow):
        raise ValueError(
            "filters.mode is 'stalk' but poster_allow and liker_allow are both "
            "empty, so nothing would ever be liked"
        )

    pacing = Pacing(**_only_known_keys(raw.get("pacing"), Pacing))
    if pacing.min_seconds_between_likes > pacing.max_seconds_between_likes:
        raise ValueError("pacing.min_seconds_between_likes exceeds the max")
    if pacing.min_seconds_between_passes > pacing.max_seconds_between_passes:
        raise ValueError("pacing.min_seconds_between_passes exceeds the max")
    # Anything faster than a minute stops being "lenient automation".
    if pacing.min_seconds_between_likes < 60:
        raise ValueError("pacing.min_seconds_between_likes must be at least 60")

    browser = Browser(**_only_known_keys(raw.get("browser"), Browser))

    return Config(
        filters=filters, pacing=pacing, browser=browser, selectors=selectors
    )


def _read_selectors(config_dir: Path) -> dict:
    return yaml.safe_load(
        (config_dir / "selectors.yml").read_text(encoding="utf-8")
    ) or {}


def load(config_dir: Path | None = None) -> Config:
    config_dir = Path(config_dir or CONFIG_DIR)

    # config.yml is the user's copy; fall back to the shipped example so a fresh
    # container still boots with sane defaults instead of crashing.
    user_path = config_dir / "config.yml"
    if not user_path.exists():
        user_path = config_dir / "config.example.yml"

    raw = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
    return parse(raw, _read_selectors(config_dir))


def as_dict(cfg: Config) -> dict:
    """The editable part of a Config, in the same shape as config.yml."""
    return {
        "filters": asdict(cfg.filters),
        "pacing": asdict(cfg.pacing),
        "browser": asdict(cfg.browser),
    }


def save(raw: dict, config_dir: Path | None = None) -> Config:
    """Validate, then write config/config.yml. Raises ValueError on bad input.

    Validation happens first so a typo in the dashboard never leaves a broken
    file behind that would stop the next container start.
    """
    config_dir = Path(config_dir or CONFIG_DIR)
    cfg = parse(raw, _read_selectors(config_dir))

    header = (
        "# Written by the LinkediNPD dashboard. Hand edits are fine too; the\n"
        "# example file next to this one documents every field.\n"
    )
    body = yaml.safe_dump(as_dict(cfg), sort_keys=False, allow_unicode=True)
    (config_dir / "config.yml").write_text(header + body, encoding="utf-8")
    return cfg
