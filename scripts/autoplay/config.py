"""Loads autoplay.toml, falling back to defaults for anything missing."""

import logging
import os

import tomli

logger = logging.getLogger(__name__)

DEFAULTS = {
    "autoplay": {
        "enabled": True,
        "speed": 1.0,
        "idle_resume_seconds": 45,
        "new_clan_mode": "expanded",
        "restart_on_game_over": True,
        "save_every_moon": True,
    },
    "pacing": {
        "camp_seconds": 18,
        "thought_interval": 3.5,
        "min_patrols": 1,
        "max_patrols": 3,
        "reading_cps": 28,
        "card_min_seconds": 4.5,
    },
    "story": {
        "profile_spotlights": True,
        "card_min_importance": 2,
    },
    "hotkeys": {
        "toggle": "f6",
        "speed": "f7",
        "skip": "f8",
    },
}

CONFIG_PATH = "autoplay.toml"


def _merge(base: dict, override: dict) -> dict:
    out = {}
    for key, value in base.items():
        if isinstance(value, dict):
            out[key] = _merge(value, override.get(key, {}) or {})
        else:
            out[key] = override.get(key, value)
    return out


def load_config() -> dict:
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomli.load(f)
        except (OSError, tomli.TOMLDecodeError):
            logger.exception("Could not read %s, using autoplay defaults", CONFIG_PATH)

    cfg = _merge(DEFAULTS, data)

    # env override so walld / launchers can force it on or off
    env = os.environ.get("CLANGEN_AUTOPLAY")
    if env is not None:
        cfg["autoplay"]["enabled"] = env.strip().lower() not in ("0", "false", "off")

    return cfg
