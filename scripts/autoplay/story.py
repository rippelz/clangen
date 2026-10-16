"""Turns a moon's raw event list into a paced, readable story.

Before a timeskip we snapshot the clan; afterwards we diff against it to find out
who died, who was born, who changed rank and who fell in love. That lets us pick
headers and importance reliably, without parsing the
event prose.
"""

from dataclasses import dataclass, field
from typing import Optional

import i18n

from scripts.autoplay.overlay import Card, clean_text
from scripts.cat.cats import Cat
from scripts.cat.enums import CatRank
from scripts.game_structure import game
from scripts.game_structure.monkeypatch import translate

# accent colours for card headers
GOLD = (176, 132, 52)
RED = (150, 56, 46)
GREEN = (78, 128, 66)
BLUE = (66, 96, 140)
PINK = (168, 80, 112)
PURPLE = (110, 78, 140)
BROWN = (120, 92, 60)


@dataclass
class ClanSnapshot:
    alive: set = field(default_factory=set)
    known: set = field(default_factory=set)
    ranks: dict = field(default_factory=dict)
    mates: dict = field(default_factory=dict)
    leader: Optional[str] = None
    deputy: Optional[str] = None

    @classmethod
    def take(cls) -> "ClanSnapshot":
        snap = cls()
        for cat in Cat.all_cats_list:
            snap.known.add(cat.ID)
            if cat.status.alive_in_player_clan:
                snap.alive.add(cat.ID)
                snap.ranks[cat.ID] = cat.status.rank
                snap.mates[cat.ID] = set(cat.mate)
        if game.clan:
            snap.leader = game.clan.leader.ID if game.clan.leader else None
            snap.deputy = game.clan.deputy.ID if game.clan.deputy else None
        return snap


@dataclass
class MoonDiff:
    died: set = field(default_factory=set)
    born: set = field(default_factory=set)
    joined: set = field(default_factory=set)
    rank_changes: dict = field(default_factory=dict)  # id -> new rank
    new_mates: set = field(default_factory=set)
    new_leader: Optional[str] = None
    new_deputy: Optional[str] = None


def diff(before: ClanSnapshot) -> MoonDiff:
    d = MoonDiff()
    for cat in Cat.all_cats_list:
        if cat.ID in before.alive and not cat.status.alive_in_player_clan and cat.dead:
            d.died.add(cat.ID)
        if cat.status.alive_in_player_clan:
            if cat.ID not in before.alive:
                if cat.ID not in before.known and cat.moons <= 1:
                    d.born.add(cat.ID)
                else:
                    d.joined.add(cat.ID)
            elif before.ranks.get(cat.ID) != cat.status.rank:
                d.rank_changes[cat.ID] = cat.status.rank
            if set(cat.mate) - before.mates.get(cat.ID, set()):
                d.new_mates.add(cat.ID)
    if game.clan:
        if game.clan.leader and game.clan.leader.ID != before.leader:
            d.new_leader = game.clan.leader.ID
        if game.clan.deputy and game.clan.deputy.ID != before.deputy:
            d.new_deputy = game.clan.deputy.ID
    return d


_RANK_HEADERS = {
    CatRank.LEADER: ("A New Leader", GOLD, 3),
    CatRank.DEPUTY: ("A New Deputy", GOLD, 3),
    CatRank.WARRIOR: ("Warrior Ceremony", GOLD, 2),
    CatRank.APPRENTICE: ("Apprentice Ceremony", GOLD, 2),
    CatRank.MEDICINE_APPRENTICE: ("Medicine Apprentice", GREEN, 2),
    CatRank.MEDICINE_CAT: ("Medicine Cat Ceremony", GREEN, 2),
    CatRank.MEDIATOR_APPRENTICE: ("Mediator Apprentice", BLUE, 2),
    CatRank.MEDIATOR: ("Mediator Ceremony", BLUE, 2),
    CatRank.ELDER: ("Retirement", BROWN, 2),
}


def _cats(ids) -> list:
    out = []
    for cid in ids:
        cat = Cat.fetch_cat(cid)
        if isinstance(cat, Cat) and cat not in out:
            out.append(cat)
    return out


def classify(event, moon: MoonDiff) -> tuple[str, tuple, int]:
    """Returns (header, accent colour, importance 1-3) for an EventInformation."""
    ids = set(event.cats_involved or [])
    types = set(event.types or [])

    if ids & moon.died:
        return "In Memoriam", RED, 3
    if ids & moon.born:
        return "New Life", GREEN, 3
    if ids & moon.joined and ("birth_death" in types or "misc" in types or "other_clans" in types):
        return "A New Face", GREEN, 3
    if "ceremony" in types:
        for cid in ids:
            if cid in moon.rank_changes:
                return _RANK_HEADERS.get(moon.rank_changes[cid], ("Ceremony", GOLD, 2))
        return "Ceremony", GOLD, 2
    if "birth_death" in types:
        return "Life and Death", RED, 3
    if "other_clans" in types:
        return "Beyond the Borders", PURPLE, 2
    if "relation" in types:
        if ids & moon.new_mates:
            return "Love Blooms", PINK, 2
        return "Bonds", PINK, 1
    if "health" in types:
        return "Health", BLUE, 1
    return "Clan Life", BROWN, 1


def reading_time(text: str, cps: float, minimum: float) -> float:
    return max(minimum, 1.6 + len(text) / max(cps, 5))


def build_moon_cards(
    events: list, moon: MoonDiff, cps: float, min_seconds: float, min_importance: int
) -> list[Card]:
    cards = [title_card()]
    minor_budget = 14  # keep huge clans from narrating for ten minutes

    for event in events:
        if "interaction" in (event.types or []):
            continue
        try:
            # same pronoun / name substitution the events screen does when it renders
            text = clean_text(translate(event.text, **(event.cat_dict or {})))
        except Exception:
            text = clean_text(event.text)
        if not text:
            continue
        header, accent, importance = classify(event, moon)
        cats = _cats(event.cats_involved or [])
        if importance >= min_importance:
            cards.append(
                Card(
                    text=text,
                    header=header,
                    accent=accent,
                    cats=cats,
                    style="major",
                    duration=reading_time(text, cps, min_seconds) + (1.0 if importance >= 3 else 0),
                )
            )
        elif minor_budget > 0:
            minor_budget -= 1
            cards.append(
                Card(
                    text=text,
                    header=header,
                    accent=accent,
                    cats=cats,
                    style="minor",
                    duration=reading_time(text, cps * 1.25, min_seconds * 0.7),
                )
            )
    return cards


def title_card() -> Card:
    clan = game.clan
    season = i18n.t(f"general.{clan.current_season}")
    living = [c for c in Cat.all_cats_list if c.status.alive_in_player_clan]
    kits = sum(1 for c in living if c.status.rank.is_baby())
    sub = f"{season} · {len(living)} cats"
    if kits:
        sub += f" · {kits} kit{'s' if kits != 1 else ''}"
    return Card(
        text=f"{clan.name}\n{sub}",
        header=f"Moon {clan.age}",
        style="title",
        duration=3.6,
    )
