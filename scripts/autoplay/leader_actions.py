"""The leader's decisions, applied without opening the den screens.

These call the same game code the warrior den and leader's den buttons do, so the
consequences (focus effects, other-clan and outsider events at the next timeskip)
play out exactly as if the player had clicked through.
"""

import random
from typing import Optional

from scripts.cat.cats import Cat
from scripts.cat.enums import CatGroup, CatStanding
from scripts.clan import OtherClan
from scripts.clan_package.settings import get_clan_setting, set_clan_setting
from scripts.game_structure import game
from scripts.game_structure.monkeypatch import translate
from scripts.screens.LeaderDenScreen import LeaderDenScreen

from scripts.autoplay import brain

_OTHER_CLAN_FOCUSES = ("sabotage_other_clans", "aid_other_clans", "raid_other_clans")


def current_focus() -> str:
    return next((f for f in brain.FOCUS_ALL if get_clan_setting(f)), "business_as_usual")


def apply_focus(code: str, target=None) -> bool:
    """Same effect as picking a focus in the warrior den and saving."""
    if code in _OTHER_CLAN_FOCUSES:
        if target is None:
            return False
        game.clan.clans_in_focus = [target.name]
    old = current_focus()
    if old == code:
        return False
    set_clan_setting(old, False)
    set_clan_setting(code, True)
    game.clan.last_focus_change = game.clan.age
    return True


def _den_shim(**attrs) -> LeaderDenScreen:
    # the den's decision methods only read a few attributes; borrow them without building UI
    shim = object.__new__(LeaderDenScreen)
    shim.clan_temper = game.clan.temperament
    shim.helper_cat = None
    for k, v in attrs.items():
        setattr(shim, k, v)
    return shim


def clan_interaction(leader, other_clan, stance: str) -> Optional[str]:
    """stance 'negative' or 'positive'. Returns the announcement text."""
    standing = other_clan.get_standing()
    options = OtherClan.interaction_dict.get(standing)
    if not options:
        return None
    interaction = options[0] if stance == "negative" else options[1]
    LeaderDenScreen.handle_other_clan_interaction(_den_shim(focus_clan=other_clan), interaction)
    return translate(
        f"screens.leader_den.action_clan_{interaction}", m_c=leader, other_clan=other_clan
    )


def nearby_outsiders() -> list:
    return [
        c
        for c in Cat.all_cats.values()
        if not c.dead
        and c.status.is_outsider
        and c.status.is_near(CatGroup.PLAYER_CLAN_ID)
        and (
            CatStanding.KNOWN in c.status.get_standing_with_group(CatGroup.PLAYER_CLAN_ID)
            or CatStanding.MEMBER in c.status.get_standing_with_group(CatGroup.PLAYER_CLAN_ID)
        )
        and c.age != "newborn"
    ]


def outsider_interaction(leader) -> Optional[tuple[Cat, str]]:
    """Deal with one outsider near the borders. Returns (cat, announcement)."""
    outsiders = nearby_outsiders()
    if not outsiders:
        return None
    cat = random.choice(outsiders)
    lost = cat.status.is_lost(CatGroup.PLAYER_CLAN_ID)
    available = ["hunt_down", "drive_off", "search" if lost else "invite_in"]
    action = brain.choose_outsider_action(leader, available)
    if not action:
        return None
    group = action.split("_")[0]
    LeaderDenScreen.handle_outsider_interaction(_den_shim(focus_cat=cat), group)
    return cat, translate(f"screens.leader_den.action_outsider_{group}", m_c=leader, r_c=cat)
