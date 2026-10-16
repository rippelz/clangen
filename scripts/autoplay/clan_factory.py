"""Founds a brand-new random clan without walking through the creation menus.

Reuses the real clan-creation screen's logic (random quick start + save_clan) so the
result is identical to a player ticking "random clan" and hitting continue.
"""

import logging
from random import sample

from scripts.cat.enums import CatAge
from scripts.clan_package.settings import save_clan_settings, set_clan_setting
from scripts.game_structure import game
from scripts.game_structure.game import Switch, switch_get_value
from scripts.game_structure.game.settings import game_setting_set
from scripts.screens.enums import GameScreen

logger = logging.getLogger(__name__)


def found_random_clan(game_mode: str = "expanded") -> bool:
    """Create and save a random clan. Returns True on success."""
    mode_screen = game.all_screens.get(GameScreen.MAKE_CLAN_CHOOSE_MODE)
    if mode_screen is None:
        logger.error("Autoplay: clan creation screen missing, can't found a clan")
        return False

    if game_mode not in ("classic", "expanded", "cruel_season"):
        game_mode = "expanded"

    info = mode_screen.clan_info
    info.clear()
    info.game_mode = game_mode
    game_setting_set("game_mode", game_mode)

    mode_screen.random_quick_start()

    # random_quick_start draws members *with* replacement, so the same cat can be
    # picked twice. Re-draw a clean, duplicate-free roster.
    taken = {info.leader, info.deputy, info.medicine_cat}
    pool = [c for c in switch_get_value(Switch.possible_cats) if c not in taken]
    wanted = min(len(set(info.starting_members)), len(pool))
    # make sure there's at least one adult besides the high ranks so patrols can happen
    adults = [
        c
        for c in pool
        if c.age not in (CatAge.NEWBORN, CatAge.KITTEN, CatAge.ADOLESCENT)
    ]
    roster = sample(pool, k=wanted) if wanted else []
    if adults and not any(c in adults for c in roster):
        roster[0:1] = [adults[0]]
    info.starting_members = roster

    mode_screen.save_clan()
    mode_screen.clan_info.clear()
    # save_clan rebuilds the top menu without mute buttons; the "clan created" screen
    # normally puts them back on its continue button
    mode_screen.set_mute_button_position("bottomright")

    # nobody's around to appoint deputies by hand, and mediators add variety
    set_clan_setting("deputy", True)
    set_clan_setting("become_mediator", True)
    save_clan_settings()
    logger.info("Autoplay founded %s", game.clan.name if game.clan else "?")
    return game.clan is not None
