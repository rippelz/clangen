"""The autoplay's judgement: what to patrol, who goes, and how the leader steers.

Choices lean on the clan's real needs (prey, herbs, injuries, borders) and on the
personalities of the cats involved, so different clans genuinely play differently.
"""

import random
import statistics
from typing import Optional

from scripts.cat.cats import Cat
from scripts.cat.enums import CatRank
from scripts.cat.personality import Personality
from scripts.clan_resources.supply import Supply
from scripts.config import get_config
from scripts.events_module.patrol.enums import PatrolChoice
from scripts.game_structure import game

FOCUS_ALL = (
    "business_as_usual",
    "hunting",
    "herb_gathering",
    "threaten_outsiders",
    "seek_outsiders",
    "rest_and_recover",
    "sabotage_other_clans",
    "aid_other_clans",
    "raid_other_clans",
    "hoarding",
)


def _norm(value: float) -> float:
    lo, hi = Personality.facet_range
    return (value - lo) / max(1, hi - lo)


def _living() -> list:
    return [c for c in Cat.all_cats_list if c.status.alive_in_player_clan]


def food_is_low() -> bool:
    pile = getattr(game.clan, "freshkill_pile", None)
    if game.clan.game_mode not in ("expanded", "cruel_season") or not pile:
        return False
    try:
        return not pile.clan_has_enough_food()
    except Exception:
        return False


def herbs_are_low() -> bool:
    try:
        return game.clan.herb_supply.get_overall_rating() in (Supply.EMPTY, Supply.LOW)
    except Exception:
        return False


def hostile_clans() -> list:
    hostile = get_config("reputation.other_clans.hostile") or 7
    return [c for c in game.clan.all_other_clans if c.relations <= hostile]


def friendly_clans() -> list:
    neutral = get_config("reputation.other_clans.neutral") or 17
    return [c for c in game.clan.all_other_clans if c.relations > neutral]


# ---------------------------------------------------------------- patrols


def plan_patrol(able: list) -> tuple[Optional[str], list]:
    """Pick a patrol type and its members from the cats able to patrol."""
    meds = [c for c in able if c.status.rank.is_any_medicine_rank()]
    others = [c for c in able if not c.status.rank.is_any_medicine_rank()]
    apprentices = [c for c in others if c.status.rank.is_any_apprentice_rank()]

    weights = {"general": 3.0}
    if others:
        weights["hunting"] = 3.0 + (7.0 if food_is_low() else 0.0)
        weights["border"] = 2.0 + (4.0 if hostile_clans() else 0.0)
        if apprentices:
            weights["training"] = 3.5
    else:
        weights.pop("general")
    if meds:
        weights["med"] = 1.5 + (6.0 if herbs_are_low() else 0.0)

    if not weights:
        return None, []

    patrol_type = random.choices(list(weights), list(weights.values()))[0]

    if patrol_type == "med":
        lead = random.choice(meds)
        members = [lead]
        for app_id in lead.apprentice:
            app = Cat.fetch_cat(app_id)
            if app in meds and app not in members:
                members.append(app)
        if others and random.random() < 0.35:
            members.append(random.choice(others))
        return "med", members

    if patrol_type == "training":
        app = random.choice(apprentices)
        members = [app]
        mentor = Cat.fetch_cat(app.mentor) if app.mentor else None
        if mentor in others:
            members.append(mentor)
        _fill(members, others, random.choice([0, 1, 1, 2]))
        return "training", members

    size = random.choices([1, 2, 3, 4, 5, 6], [2, 4, 5, 3, 1, 1])[0]
    pool = [c for c in others if not c.status.rank.is_any_apprentice_rank()] or others
    first = random.choice(pool)
    members = [first]
    # social flavour: mates and mentor pairs like to go out together
    for mate_id in first.mate:
        mate = Cat.fetch_cat(mate_id)
        if mate in others and mate not in members and random.random() < 0.45:
            members.append(mate)
    for app_id in first.apprentice:
        app = Cat.fetch_cat(app_id)
        if app in others and app not in members and random.random() < 0.4:
            members.append(app)
    _fill(members, others, size - len(members))
    return patrol_type, members[:6]


def _fill(members: list, pool: list, count: int):
    rest = [c for c in pool if c not in members]
    random.shuffle(rest)
    members.extend(rest[: max(0, min(count, 6 - len(members)))])


def decide_patrol(patrol_obj, can_antagonize: bool) -> PatrolChoice:
    """Proceed, decline or antagonize, in character for the patrol."""
    cats = patrol_obj.patrol_cats or []
    if not cats:
        return PatrolChoice.PROCEED

    aggression = statistics.mean(_norm(c.personality.aggression) for c in cats)
    stability = statistics.mean(_norm(c.personality.stability) for c in cats)
    lawful = statistics.mean(_norm(c.personality.lawfulness) for c in cats)

    chance = getattr(patrol_obj.patrol_event, "chance_of_success", 50) or 50
    # risky-looking patrols make cautious cats think twice
    decline = 0.04 + max(0.0, (45 - chance) / 100) * (1.2 - aggression)
    decline += (lawful - 0.5) * 0.06 + (0.5 - stability) * 0.05
    decline = max(0.02, min(decline, 0.35))

    if can_antagonize:
        antag = 0.08 + aggression * 0.45 - lawful * 0.15
        if random.random() < max(0.03, min(antag, 0.6)):
            return PatrolChoice.ANTAGONIZE

    if random.random() < decline:
        return PatrolChoice.DECLINE
    return PatrolChoice.PROCEED


def patrol_count(able_count: int, lo: int, hi: int) -> int:
    if able_count <= 0:
        return 0
    hi = max(lo, min(hi, 1 + able_count // 3))
    return random.randint(lo, hi)


# ---------------------------------------------------------------- leadership


def focus_change_allowed() -> bool:
    last = game.clan.last_focus_change
    return last is None or last + get_config("focus.duration") <= game.clan.age


def choose_focus(current: str, has_meds: bool) -> tuple[Optional[str], Optional[object]]:
    """Return (focus_code, target_other_clan) or (None, None) to keep the current focus."""
    living = _living()
    if not living:
        return None, None
    leader = game.clan.leader if game.clan.leader and not game.clan.leader.dead else None
    aggression = _norm(leader.personality.aggression) if leader else 0.5
    sociability = _norm(leader.personality.sociability) if leader else 0.5

    hurt = sum(1 for c in living if not c.can_work()) / len(living)

    want = "business_as_usual"
    target = None
    if food_is_low():
        want = "hunting"
    elif herbs_are_low() and has_meds:
        want = "herb_gathering"
    elif hurt > 0.3:
        want = "rest_and_recover"
    elif len(living) < 9:
        want = "seek_outsiders"
    else:
        roll = random.random()
        hostile = hostile_clans()
        friendly = friendly_clans()
        if hostile and roll < aggression * 0.35:
            want, target = "raid_other_clans", random.choice(hostile)
        elif hostile and roll < aggression * 0.5:
            want, target = "sabotage_other_clans", random.choice(hostile)
        elif friendly and roll < sociability * 0.3:
            want, target = "aid_other_clans", random.choice(friendly)
        elif roll < 0.12:
            want = "threaten_outsiders" if aggression > 0.6 else "hoarding"

    if want == current:
        return None, None
    return want, target


def choose_clan_interaction(leader, focus_clan) -> str:
    """'positive' or 'negative' leader's-den stance toward another clan."""
    aggression = _norm(leader.personality.aggression) if leader else 0.5
    p_negative = 0.15 + aggression * 0.5
    if focus_clan is not None:
        if focus_clan.relations <= (get_config("reputation.other_clans.hostile") or 7):
            p_negative += 0.15
        elif focus_clan.relations > (get_config("reputation.other_clans.neutral") or 17):
            p_negative -= 0.2
    return "negative" if random.random() < p_negative else "positive"


def choose_outsider_action(leader, available: list[str]) -> Optional[str]:
    if not available:
        return None
    sociability = _norm(leader.personality.sociability) if leader else 0.5
    aggression = _norm(leader.personality.aggression) if leader else 0.5
    weights = []
    for action in available:
        if action.startswith("invite"):
            weights.append(1 + sociability * 4 + (2 if len(_living()) < 12 else 0))
        elif action.startswith("drive"):
            weights.append(1 + aggression * 2)
        elif action.startswith("hunt"):
            weights.append(0.3 + aggression * 2.5)
        elif action.startswith("search"):
            weights.append(2.5)
        else:
            weights.append(1)
    return random.choices(available, weights)[0]


def is_working_rank(cat) -> bool:
    return cat.status.rank not in (CatRank.NEWBORN, CatRank.KITTEN, CatRank.ELDER)
