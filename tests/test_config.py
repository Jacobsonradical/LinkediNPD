"""Settings round-trip: what the dashboard saves must be what the engine loads,
and bad input must be refused before anything is written."""

import shutil
from pathlib import Path

import pytest

from app import config

REAL = Path(__file__).parent.parent / "config"


@pytest.fixture
def config_dir(tmp_path):
    """A scratch copy of config/, so saving never touches the real files."""
    for name in ("config.example.yml", "selectors.yml"):
        shutil.copy(REAL / name, tmp_path / name)
    return tmp_path


def test_falls_back_to_the_example_when_no_user_config_exists(config_dir):
    assert config.load(config_dir).filters.mode == "except"


def test_save_then_load_round_trips(config_dir):
    config.save(
        {
            "filters": {"mode": "stalk", "liker_allow": ["Peter"], "poster_block": ["Bob"]},
            "pacing": {"min_seconds_between_likes": 120, "max_seconds_between_likes": 600},
        },
        config_dir,
    )
    cfg = config.load(config_dir)
    assert cfg.filters.mode == "stalk"
    assert cfg.filters.liker_allow == ["Peter"]
    assert cfg.filters.poster_block == ["Bob"]
    assert cfg.pacing.min_seconds_between_likes == 120
    # Anything the dashboard did not send keeps its default.
    assert cfg.pacing.max_likes_per_day == 40


def test_names_are_trimmed_and_blanks_dropped(config_dir):
    cfg = config.save({"filters": {"poster_block": ["  Bob ", "", "   "]}}, config_dir)
    assert cfg.filters.poster_block == ["Bob"]


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ({"filters": {"mode": "stalk"}}, "nothing would ever be liked"),
        ({"filters": {"mode": "yolo"}}, "expected one of"),
        ({"pacing": {"min_seconds_between_likes": 30}}, "at least 60"),
        ({"pacing": {"min_seconds_between_likes": 900, "max_seconds_between_likes": 300}}, "exceeds the max"),
    ],
)
def test_invalid_settings_are_refused_and_nothing_is_written(config_dir, raw, fragment):
    with pytest.raises(ValueError, match=fragment):
        config.save(raw, config_dir)
    assert not (config_dir / "config.yml").exists()
