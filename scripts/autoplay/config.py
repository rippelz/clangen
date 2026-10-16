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
        "card_min_importance": 2,
    },
    "display": {
        "mode": "fullscreen",
        "window_size": [1600, 900],
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


def _display() -> tuple[bool, str, list]:
    cfg = load_config()
    mode = str(cfg["display"]["mode"]).lower()
    if os.environ.get("CLANGEN_AUTOPLAY_WINDOW"):
        mode = "window"
    return cfg["autoplay"]["enabled"], mode, cfg["display"]["window_size"]


def boot_fullscreen(saved: bool) -> bool:
    """Whether the game should open fullscreen."""
    enabled, mode, _ = _display()
    if not enabled or mode not in ("fullscreen", "window"):
        return saved
    return mode == "fullscreen"


def display_override() -> "tuple[int, int] | None":
    """Size for a widescreen window laid out like fullscreen, or None for ClanGen's 800x700."""
    enabled, mode, size = _display()
    if not enabled or mode == "classic":
        return None
    import pygame

    forced = os.environ.get("CLANGEN_AUTOPLAY_WINDOW")  # e.g. 1920x1080, for testing
    if forced:
        w, h = (int(v) for v in forced.lower().split("x"))
        return w, h
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        w, h = 1600, 900
    desk = pygame.display.get_desktop_sizes() or [(w, h)]
    dw, dh = desk[0]
    # leave room for panels/decorations on smaller screens
    fit = min(1.0, (dw * 0.95) / w, (dh * 0.9) / h)
    return max(800, int(w * fit)), max(700, int(h * fit))
