"""The autoplay director: lets the clan live its life on the camp screen.

The camp stays on screen. Cats wander, visit their mates, follow their mentors and
gather in the clearing (see camp_life.py), and everything the player would normally
do through menus happens as a scene in camp instead:

  dawn: the moon's story, with the cats involved stepping into the clearing
  -> the leader announces decisions (focus, other clans, outsiders)
  -> patrols gather, walk out of camp, and come home with their tale
  -> dusk: the cats bed down, night falls, and the moon passes

Under the hood it calls the same game code the menus do (Patrol, events.one_moon,
the den decisions), so the simulation is exactly ClanGen's own.

Any click, scroll or key press hands control to the player immediately; after a
stretch of idleness the director quietly picks the story back up.

Scenes are generators that yield Wait/Until objects, which keeps the pacing code
readable and lets a scene be dropped mid-way the moment the player takes over.
Every scene ends back in camp ("banana convergence"), so after any interruption
or failure the next moon can start from a known screen.
"""

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import i18n
import pygame

import scripts.game_structure.screen_settings as screen_settings
from scripts import events
from scripts.autoplay import brain, leader_actions, story
from scripts.autoplay.camp_life import CampLife
from scripts.autoplay.clan_factory import found_random_clan
from scripts.autoplay.config import load_config
from scripts.autoplay import overlay as overlay_mod
from scripts.autoplay.overlay import Card, Overlay, clean_text
from scripts.autoplay.panels import Panels
from scripts.autoplay.scenery import Scenery
from scripts.cat.cats import Cat
from scripts.cat.enums import CatRank
from scripts.clan_package.get_clan_cats import get_living_clan_cat_count
from scripts.clan_package.settings import get_clan_setting, set_clan_setting
from scripts.events_module.patrol.enums import PatrolChoice
from scripts.events_module.patrol.patrol import Patrol
from scripts.events_module.thoughts.generate_thoughts import get_new_thought
from scripts.game_structure import constants, game
from scripts.game_structure.game import Switch, switch_get_value
from scripts.game_structure.game.switches import switch_set_value
from scripts.screens.enums import GameScreen

logger = logging.getLogger(__name__)

# screens where a half-finished manual task would be lost if we yanked the player away
_NO_AUTO_RESUME = {
    GameScreen.SETTINGS,
    GameScreen.SWITCH_CLAN,
    GameScreen.EVENT_EDIT,
    GameScreen.MAKE_CLAN_CHOOSE_MODE,
    GameScreen.MAKE_CLAN_CHOOSE_CARDS,
    GameScreen.MAKE_CLAN_CHOOSE_NAME,
    GameScreen.MAKE_CLAN_CHOOSE_CATS,
    GameScreen.MAKE_CLAN_CHOOSE_CAMP,
    GameScreen.MAKE_CLAN_CHOOSE_SYMBOL,
    GameScreen.MAKE_CLAN_CLAN_CREATED,
}

_PATROL_LABELS = {
    "general": "patrol",
    "hunting": "hunting patrol",
    "border": "border patrol",
    "training": "training patrol",
    "med": "herb-gathering trip",
}

_PATROL_CALLS = {
    "general": ["Let's walk the territory.", "Who's coming on patrol?", "Let's see what the forest holds today."],
    "hunting": ["The fresh-kill pile won't fill itself.", "Who's hungry? Let's hunt.", "Let's bring back some prey."],
    "border": ["Let's check the scent markers.", "Time to walk the borders.", "Let's make sure the borders are safe."],
    "training": ["Training time!", "Let's see what you've learned.", "Today we practise."],
    "med": ["We're low on herbs.", "Let's gather some herbs.", "The herb store needs filling."],
}

_SPEEDS = (0.5, 1.0, 2.0, 4.0)

# dev aid: CLANGEN_AUTOPLAY_SHOTS=/some/dir saves a screenshot every few seconds
_SHOT_DIR = os.environ.get("CLANGEN_AUTOPLAY_SHOTS")
_SHOT_EVERY = float(os.environ.get("CLANGEN_AUTOPLAY_SHOT_EVERY", "3"))
if _SHOT_DIR:
    os.makedirs(_SHOT_DIR, exist_ok=True)


@dataclass
class Wait:
    seconds: float


class Until:
    def __init__(self, predicate: Callable[[], bool], timeout: float = 15.0):
        self.predicate = predicate
        self.timeout = timeout
        self.elapsed = 0.0
        self.ok = False


class _Job:
    """Runs a function on a worker thread so the camp keeps moving meanwhile."""

    def __init__(self, fn, *args):
        self.result = None
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, args=(fn, args), daemon=True)
        self.thread.start()

    def _run(self, fn, args):
        try:
            self.result = fn(*args)
        except BaseException as e:  # noqa: BLE001 - reported on the main thread
            self.error = e

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()


class Director:
    def __init__(self):
        self.cfg = load_config()
        self.enabled = bool(self.cfg["autoplay"]["enabled"])
        self.speed = float(self.cfg["autoplay"]["speed"]) or 1.0
        self.idle_resume = float(self.cfg["autoplay"]["idle_resume_seconds"])

        self.wallpaper = str(self.cfg["display"].get("style", "wallpaper")).lower() == "wallpaper"
        overlay_mod.STYLE["wallpaper"] = self.wallpaper
        self.overlay = Overlay()
        self.panels = Panels()
        self.scenery = Scenery()
        self.camp = CampLife()
        self.camp.say = self._actor_say
        self.mode = "auto" if self.enabled else "off"
        self.last_input = 0.0

        self.task = None
        self.wait = None
        self.critical = False  # true while a timeskip is in flight: don't abandon it
        self._manual_requested = False
        self.caption = ""

        # story state survives interruptions
        self.pending_cards: list[Card] = []
        self.progress = {}
        self.jobs: list[_Job] = []
        self._patrol = None  # (Patrol, label) begun but not yet resolved
        self.history_left = 0  # moons still to fast-forward
        self._history = None  # running tally for the "story so far" card
        self._last_thought_cat = None
        self._next_thought = 2.0
        self._last_shot = 0.0
        self._shot_n = 0
        if os.environ.get("CLANGEN_AUTOPLAY_SPEED"):
            self.speed = float(os.environ["CLANGEN_AUTOPLAY_SPEED"])

    # ================================================================ hooks

    def boot(self):
        """Called once the game has loaded, instead of sitting on the main menu."""
        if not self.enabled:
            return
        self._close_windows()
        if game.clan is None or switch_get_value(Switch.error_message):
            if not found_random_clan(self.cfg["autoplay"]["new_clan_mode"]):
                logger.error("Autoplay could not found a clan; staying on the menu")
                self.mode = "off"
                return
            self.pending_cards = self._founding_cards()
            self.history_left = int(self.cfg["history"]["new_clan_moons"])
        else:
            self.pending_cards = [story.title_card()]
        self._go(GameScreen.CAMP)

    def on_clan_loaded(self):
        """The player switched clans: forget anything tied to the old one."""
        self._drop_task()
        self.pending_cards.clear()
        self.progress = {}
        self.panels.clear()
        self.camp.detach()
        self.history_left = 0
        self._history = None

    def observe_event(self, event):
        hk = self.cfg["hotkeys"]
        if event.type == pygame.KEYDOWN:
            name = pygame.key.name(event.key)
            if name == hk["toggle"]:
                self._toggle()
                return
            if name == hk["speed"]:
                i = (_SPEEDS.index(self.speed) + 1) % len(_SPEEDS) if self.speed in _SPEEDS else 1
                self.speed = _SPEEDS[i]
                return
            if name == hk["skip"]:
                self._skip()
                return
            if name == hk.get("fast_forward") and game.clan is not None:
                self.history_left += max(1, int(self.cfg["history"]["skip_moons"]))
                if self.mode != "auto":
                    self._resume()
                elif not self.critical and self._history is None:
                    self._drop_task()  # start the fast-forward now, not after this scene
                return
        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN, pygame.MOUSEWHEEL, pygame.QUIT):
            self.last_input = time.monotonic()
            if self.mode == "auto":
                self._enter_manual()

    def update(self, dt: float):
        scaled = dt * self.speed if self.mode == "auto" else dt
        self.overlay.update(scaled)
        self.panels.update(dt)

        on_camp = game.current_screen == GameScreen.CAMP and not game.switch_screens
        if on_camp and self.camp.sync(self._screen(GameScreen.CAMP)):
            self.camp.update(scaled, ambient=self.camp.night_target == 0 and self.mode != "off")
            if self.mode != "off":
                self._ambient_thoughts(scaled)
        elif not on_camp:
            self.camp.detach()

        if on_camp and self._wallpaper_active():
            self.scenery.hide_chrome(self._screen(GameScreen.CAMP))
        elif on_camp:
            self.scenery.restore_chrome(self._screen(GameScreen.CAMP))
        else:
            self.scenery.forget()

        if self.mode == "manual":
            idle = time.monotonic() - self.last_input
            if (
                idle >= self.idle_resume
                and not self._work_in_flight()
                and game.current_screen not in _NO_AUTO_RESUME
            ):
                self._resume()
        elif self.mode == "auto":
            if self.critical or not game.switch_screens:
                self._advance(dt)
        self._refresh_hud()

    def _wallpaper_active(self) -> bool:
        return (
            self.wallpaper
            and self.mode == "auto"
            and game.current_screen == GameScreen.CAMP
            and not game.switch_screens
        )

    def draw_background(self, surface: pygame.Surface):
        """Called right after the screen draws its background, before the UI."""
        if self._wallpaper_active():
            try:
                self.scenery.draw_background(surface, self._screen(GameScreen.CAMP))
            except Exception:
                logger.exception("Autoplay: backdrop failed; falling back to the game look")
                self.wallpaper = False

    def draw(self, surface: pygame.Surface):
        on_camp = game.current_screen == GameScreen.CAMP and not game.switch_screens
        if on_camp:
            self.camp.draw(surface, full_screen=self._wallpaper_active())
        self.panels.draw(surface, self.overlay.hud_text)
        self.overlay.draw(surface, show_story=self.mode == "auto", show_bubbles=on_camp)
        if _SHOT_DIR:
            now = time.monotonic()
            if now - self._last_shot >= _SHOT_EVERY:
                self._last_shot = now
                self._shot_n += 1
                pygame.image.save(surface, os.path.join(_SHOT_DIR, f"{self._shot_n:05d}.png"))

    # ================================================================ mode handling

    def _toggle(self):
        if self.mode == "auto":
            self.mode = "off"
            self._drop_task()
        else:
            self._resume()

    def _enter_manual(self):
        if self.critical:
            # let the timeskip land first; we'll flip to manual right after
            self._manual_requested = True
            return
        self.mode = "manual"
        self._drop_task()

    def _resume(self):
        self.mode = "auto"
        self._close_windows()
        self.task = None
        self.wait = None

    def _drop_task(self):
        # put an unfinished card back so the story isn't lost
        card = self.overlay.card
        if card is not None and not card.done and card.style != "title":
            card.age = 0.0
            self.pending_cards.insert(0, card)
        self.overlay.clear_card()
        self.overlay.bubbles.clear()
        if self.task is not None:
            try:
                self.task.close()
            except Exception:
                pass
        self.task = None
        self.wait = None
        self.caption = ""
        self._settle_patrol()
        self.camp.reset()

    def _settle_patrol(self):
        """A patrol that already set out still has to come home with an outcome."""
        for job in self.jobs:
            job.thread.join(timeout=5)
        self.jobs = [j for j in self.jobs if not j.done]
        if self._patrol is None:
            return
        patrol, label = self._patrol
        self._patrol = None
        try:
            text, results, _, _ = patrol.proceed_patrol(PatrolChoice.PROCEED)
            self.panels.add(label.capitalize(), clean_text(f"{text} {results}"), (96, 110, 70))
        except Exception:
            logger.exception("Autoplay: couldn't finish an interrupted patrol")

    def _skip(self):
        if self.overlay.card:
            self.overlay.card.age = max(self.overlay.card.age, self.overlay.card.duration - 0.3)
        if isinstance(self.wait, Wait):
            self.wait.seconds = 0

    def _refresh_hud(self):
        o = self.overlay
        o.hud_mode = self.mode
        speed = "" if self.speed == 1.0 else f" · {self.speed:g}x"
        if self.mode == "auto":
            o.hud_text = f"AUTO · {self.caption or 'watching'}{speed}"
        elif self.mode == "manual":
            if game.current_screen in _NO_AUTO_RESUME:
                o.hud_text = "MANUAL · press F6 to resume autoplay"
            else:
                left = max(0, int(self.idle_resume - (time.monotonic() - self.last_input)) + 1)
                o.hud_text = f"MANUAL · autoplay resumes in {left}s · F6 now"
        else:
            o.hud_text = "AUTOPLAY OFF · F6"

    # ================================================================ scheduler

    def _advance(self, dt: float):
        if self.task is None:
            self.task = self._main_loop()
            self.wait = None

        w = self.wait
        if isinstance(w, Wait):
            w.seconds -= dt * self.speed
            if w.seconds > 0:
                return
        elif isinstance(w, Until):
            try:
                ok = w.predicate()
            except Exception:
                ok = False
            if not ok:
                w.elapsed += dt * self.speed
                if w.elapsed < w.timeout:
                    return
            w.ok = ok

        try:
            self.wait = next(self.task)
        except StopIteration:
            self.task = None
        except Exception:
            logger.exception("Autoplay scene failed; recovering in camp")
            self.critical = False
            self.task = self._recover()
            self.wait = None

        if not self.critical and self._manual_requested:
            self._manual_requested = False
            self.mode = "manual"
            self._drop_task()

    def _recover(self):
        self.overlay.clear_card()
        self._settle_patrol()
        self.camp.reset()
        yield Wait(2)
        self._close_windows()
        yield from self._goto(GameScreen.CAMP)
        yield Wait(2)

    # ================================================================ helpers

    def _screen(self, name):
        return game.all_screens[name]

    def _go(self, target):
        if game.current_screen != target or switch_get_value(Switch.cur_screen) != target:
            self._screen(game.current_screen).change_screen(target)

    def _goto(self, target):
        if game.current_screen == target and not game.switch_screens:
            return
        self._go(target)
        yield Until(lambda: game.current_screen == target and not game.switch_screens, 5)

    def _in_camp(self):
        """Make sure we're looking at camp with the cats attached."""
        yield from self._goto(GameScreen.CAMP)
        yield Until(lambda: self.camp.camp is not None, 2)

    def _close_windows(self):
        try:
            for window in list(screen_settings.MANAGER.get_window_stack().get_full_stack()):
                window.kill()
        except Exception:
            logger.exception("Autoplay: couldn't close open windows")

    def _work_in_flight(self) -> bool:
        return any(not j.done for j in self.jobs)

    def _job(self, fn, *args) -> _Job:
        job = _Job(fn, *args)
        self.jobs = [j for j in self.jobs if not j.done] + [job]
        return job

    def _moon_progress(self) -> dict:
        if self.progress.get("moon") != (game.clan.save_id, game.clan.age):
            self.progress = {
                "moon": (game.clan.save_id, game.clan.age),
                "camp": False,
                "leadership": False,
                "patrols_planned": None,
                "patrols_done": 0,
            }
        return self.progress

    def _read(self, text: str, minimum: float = 3.0) -> float:
        return story.reading_time(text or "", float(self.cfg["pacing"]["reading_cps"]), minimum)

    def _actor_say(self, actor, text: str, duration: float = 4.2):
        cat = actor.cat
        if cat is not None:
            self.overlay.add_bubble(self.camp.bubble_for(actor, str(cat.name), text, duration))

    def _say(self, cat, text: str, duration: float = 4.5) -> bool:
        acts = self.camp.present([cat.ID]) if cat is not None else []
        if not acts:
            return False
        self._actor_say(acts[0], text, duration)
        return True

    def _show(self, card: Card):
        """Show a card and wait for it, logging it in the chronicle."""
        self.overlay.show_card(card)
        if card.style != "title":
            text = card.text + (f" {card.footer}" if card.footer else "")
            self.panels.add(card.header, text, card.accent, card.cats[0] if card.cats else None)
        while not card.done:
            yield Wait(0.1)
        self.overlay.clear_card()
        yield Wait(0.4)

    def _ambient_thoughts(self, dt: float):
        """Now and then a cat's current thought floats above its head."""
        if self.camp.night > 0.3 or self.overlay.card is not None and self.overlay.card.style == "major":
            return
        self._next_thought -= dt
        if self._next_thought > 0:
            return
        interval = float(self.cfg["pacing"]["thought_interval"])
        self._next_thought = interval * random.uniform(0.8, 1.6)
        acts = [a for a in self.camp.present() if a.cat_id != self._last_thought_cat]
        if not acts:
            return
        actor = random.choice(acts)
        cat = actor.cat
        if cat is None:
            return
        try:
            if not cat.thought or random.random() < 0.3:
                get_new_thought(cat, cat.next_thought_type)
        except Exception:
            return
        if cat.thought:
            self._last_thought_cat = cat.ID
            self._actor_say(actor, str(cat.thought))

    # ================================================================ the show

    def _main_loop(self):
        pace = self.cfg["pacing"]
        while True:
            if game.clan is None or get_living_clan_cat_count(Cat) == 0:
                yield from self._scene_game_over()
                continue

            yield from self._in_camp()

            if self.pending_cards:
                yield from self._scene_story()
                continue

            if self.history_left > 0:
                yield from self._scene_history()
                continue

            prog = self._moon_progress()
            if not prog["camp"]:
                yield from self._scene_idle(float(pace["camp_seconds"]))
                prog["camp"] = True
            if not prog["leadership"]:
                prog["leadership"] = True
                yield from self._scene_leadership()
            yield from self._scene_patrols()
            yield from self._scene_idle(float(pace["camp_seconds"]) * 0.6, "the clan rests")
            yield from self._scene_timeskip()

    def _scene_idle(self, seconds: float, caption: str = "life in camp"):
        self.caption = caption
        yield Wait(seconds)

    # ---------------------------------------------------------------- story

    def _scene_story(self):
        self.caption = "telling the moon's story"
        while self.pending_cards:
            card = self.pending_cards.pop(0)
            staged = self._stage(card)
            yield from self._show(card)
            if staged:
                self.camp.release(staged)

    def _stage(self, card: Card) -> list:
        """The cats a story is about step into the clearing while it's told."""
        if card.style != "major" or not card.cats:
            return []
        ids = [c.ID for c in card.cats if isinstance(c, Cat)]
        present = self.camp.present(ids)
        if not present:
            return []
        if "Ceremony" in card.header or "Leader" in card.header or "Deputy" in card.header:
            leader = game.clan.leader
            if leader is not None and leader.ID not in ids and self.camp.present([leader.ID]):
                self.camp.gather([leader.ID], self.camp.leader_spot, radius=0)
                ids.append(leader.ID)
        hub = self.camp.hub
        self.camp.gather([a.cat_id for a in present], (hub[0], hub[1] + 10), radius=30)
        self.camp.highlight(ids, card.duration)
        return ids

    def _founding_cards(self) -> list[Card]:
        clan = game.clan
        ranks = [c for c in (clan.leader, clan.deputy, clan.medicine_cat) if c]
        members = len([c for c in Cat.all_cats_list if c.status.alive_in_player_clan])
        parts = []
        if clan.leader:
            parts.append(f"{clan.leader.name} leads")
        if clan.deputy:
            parts.append(f"{clan.deputy.name} stands as deputy")
        if clan.medicine_cat:
            parts.append(f"{clan.medicine_cat.name} tends to the sick")
        text = ", ".join(parts) + f". {members} cats make the {clan.biome.lower()} camp their home."
        return [
            Card(text="A new clan is born", header=str(clan.name), style="title", duration=4.5),
            Card(
                text=text,
                header="The Founding",
                accent=story.GOLD,
                cats=ranks,
                style="major",
                duration=self._read(text, 6),
            ),
        ]

    # ---------------------------------------------------------------- leadership

    def _scene_leadership(self):
        leader = game.clan.leader
        if leader is None or not leader.status.alive_in_player_clan or not leader.can_work():
            return
        has_meds = any(
            c.status.rank.is_any_medicine_rank() and c.status.alive_in_player_clan and c.can_work()
            for c in Cat.all_cats_list
        )
        decisions = []
        if brain.focus_change_allowed() and random.random() < 0.6:
            code, target = brain.choose_focus(leader_actions.current_focus(), has_meds)
            if code:
                decisions.append(("focus", code, target))
        if game.clan.all_other_clans and random.random() < 0.45:
            decisions.append(("clan", None, None))
        if random.random() < 0.3 and leader_actions.nearby_outsiders():
            decisions.append(("outsider", None, None))
        if not decisions:
            return

        self.caption = f"{leader.name} addresses the clan"
        acts = self.camp.gather([leader.ID], self.camp.leader_spot, radius=0, speed=1.0)
        yield Until(lambda: self.camp.arrived(acts), 12)
        self.camp.highlight([leader.ID], 4)

        for kind, code, target in decisions:
            if kind == "focus":
                if not leader_actions.apply_focus(code, target):
                    continue
                name = i18n.t(f"settings.{code}")
                line = f"From now on, our focus is {name.lower()}."
                if target is not None:
                    line = f"{name}: {target.name}Clan. That is my decision."
                self._say(leader, line, 5)
                self.caption = f"new focus: {name.lower()}"
                yield Wait(2.5)
                yield from self._show(
                    Card(
                        text=f"{leader.name} sets the clan's focus to {name.lower()}"
                        + (f", aimed at {target.name}Clan." if target is not None else "."),
                        header="The Leader's Word",
                        accent=story.GOLD,
                        cats=[leader],
                        style="minor",
                        duration=self._read(name, 4),
                    )
                )
            elif kind == "clan":
                clans = game.clan.all_other_clans
                weights = [
                    2.0 if c in brain.hostile_clans() or c in brain.friendly_clans() else 1.0
                    for c in clans
                ]
                other = random.choices(clans, weights)[0]
                stance = brain.choose_clan_interaction(leader, other)
                text = leader_actions.clan_interaction(leader, other, stance)
                if not text:
                    continue
                self.caption = f"{leader.name} considers {other.name}Clan"
                self._say(leader, f"We must deal with {other.name}Clan.", 4)
                yield Wait(2.5)
                yield from self._show(
                    Card(
                        text=clean_text(text),
                        header="Beyond the Borders",
                        accent=story.PURPLE,
                        cats=[leader],
                        style="minor",
                        duration=self._read(text, 4),
                    )
                )
            elif kind == "outsider":
                result = leader_actions.outsider_interaction(leader)
                if not result:
                    continue
                cat, text = result
                self.caption = f"{leader.name} thinks about {cat.name}"
                yield from self._show(
                    Card(
                        text=clean_text(text),
                        header="Outsiders",
                        accent=story.BROWN,
                        cats=[leader, cat],
                        style="minor",
                        duration=self._read(text, 4),
                    )
                )
        self.camp.release([leader.ID])

    # ---------------------------------------------------------------- patrols

    @staticmethod
    def _able_cats() -> list:
        able = []
        for cat in Cat.all_cats_list:
            if (
                cat.ID not in game.patrolled
                and cat.status.rank.is_allowed_to_patrol()
                and cat.status.alive_in_player_clan
                and cat.can_work()
            ):
                if cat.status.rank == CatRank.NEWBORN and not constants.CONFIG["fun"]["newborns_can_patrol"]:
                    continue
                able.append(cat)
        return able

    def _scene_patrols(self):
        prog = self._moon_progress()
        pace = self.cfg["pacing"]
        while True:
            able = self._able_cats()
            if prog["patrols_planned"] is None:
                prog["patrols_planned"] = brain.patrol_count(
                    len(able), int(pace["min_patrols"]), int(pace["max_patrols"])
                )
            if prog["patrols_done"] >= prog["patrols_planned"]:
                return
            prog["patrols_done"] += 1
            patrol_type, members = brain.plan_patrol(able)
            if not members:
                prog["patrols_planned"] = prog["patrols_done"]
                return
            yield from self._run_patrol(patrol_type, members)
            yield from self._scene_idle(random.uniform(4, 8))

    def _run_patrol(self, patrol_type: str, members: list):
        label = _PATROL_LABELS.get(patrol_type, "patrol")
        lead = members[0]
        ids = [c.ID for c in members]

        # gather by the camp entrance, then head out
        self.caption = f"{lead.name} gathers a {label}"
        hub = self.camp.hub
        meet = (hub[0], hub[1] + 60)
        acts = self.camp.gather(ids, meet, radius=26)
        yield Until(lambda: self.camp.arrived(acts), 12)
        self._say(lead, random.choice(_PATROL_CALLS.get(patrol_type, _PATROL_CALLS["general"])), 3.5)
        yield Wait(2.5)

        patrol = Patrol()
        job = self._job(patrol.begin_patrol, members, patrol_type)
        self.camp.leave(ids)
        self.caption = f"the {label} sets out"
        yield Until(lambda: job.done, 30)
        if job.error is not None or not job.result:
            if job.error is not None and not isinstance(job.error, RuntimeError):
                logger.error("Autoplay patrol failed to start: %r", job.error)
            self.camp.come_back(ids)
            return
        self._patrol = (patrol, label)
        yield Wait(3)

        # the tale, told while they're away
        header = label[0].upper() + label[1:]
        intro = clean_text(job.result)
        try:
            art = patrol.get_patrol_art()
        except Exception:
            art = None
        self.overlay.show_card(
            Card(text=intro, header=header, accent=(96, 110, 70), cats=members, art=art, style="major",
                 duration=self._read(intro, 5) + 1)
        )
        while self.overlay.card is not None and not self.overlay.card.done:
            yield Wait(0.1)
        self.overlay.clear_card()

        can_antag = bool(getattr(patrol.patrol_event, "antag_success_outcomes", None))
        choice = brain.decide_patrol(patrol, can_antag)
        verb = {
            PatrolChoice.PROCEED: "presses on",
            PatrolChoice.DECLINE: "turns back",
            PatrolChoice.ANTAGONIZE: "picks a fight",
        }[choice]
        self.caption = f"the patrol {verb}"
        job = self._job(patrol.proceed_patrol, choice)
        yield Until(lambda: job.done, 30)
        self._patrol = None
        if job.error is not None:
            logger.error("Autoplay patrol failed: %r", job.error)
            self.camp.come_back(ids)
            return
        text, results, _, outcome_art = job.result
        yield Wait(0.6)
        yield from self._show(
            Card(
                text=clean_text(text),
                header=f"{header}: {verb}",
                accent=(96, 110, 70),
                cats=members,
                art=outcome_art if outcome_art is not None else art,
                footer=clean_text(results or ""),
                style="major",
                duration=self._read(f"{text} {results}", 5) + 1,
            )
        )

        # home again (whoever made it), plus anyone they brought back
        self.caption = f"the {label} returns"
        back = self.camp.come_back(ids)
        self.camp.refresh_roster()
        yield Until(lambda: self.camp.arrived(back), 12)

    # ---------------------------------------------------------------- timeskip

    def _scene_timeskip(self):
        self.caption = "dusk falls over camp"
        self._close_windows()
        self.camp.bed_down()
        self.camp.night_target = 1.0
        yield Wait(5)

        if not get_clan_setting("deputy") and not game.clan.deputy:
            # with no player to appoint one, a deputy-less clan can't survive its leader
            set_clan_setting("deputy", True)
        snapshot = story.ClanSnapshot.take()

        camp = self._screen(GameScreen.CAMP)
        if game.current_screen == GameScreen.CAMP:
            # the season can flip mid-timeskip; "default" always tracks the current one
            camp.active_blur_bg = "default"

        self.critical = True
        try:
            self.caption = "a moon passes..."
            job = self._job(events.one_moon)
            yield Until(lambda: job.done, 600)
        finally:
            self.critical = False
        if job.error is not None:
            raise job.error
        self._after_timeskip()

        moon = story.diff(snapshot)
        pace, st = self.cfg["pacing"], self.cfg["story"]
        self.pending_cards = story.build_moon_cards(
            list(game.cur_events_list),
            moon,
            float(pace["reading_cps"]),
            float(pace["card_min_seconds"]),
            int(st["card_min_importance"]),
        )
        self.camp.refresh_roster(newborn_ids=moon.born)

        if self.cfg["autoplay"]["save_every_moon"] and get_living_clan_cat_count(Cat) > 0:
            try:
                camp = self._screen(GameScreen.CAMP)
                camp.save_button.save_game(current_screen=camp)
            except Exception:
                logger.exception("Autoplay save failed")

        yield Wait(1.5)
        self.caption = "a new moon rises"
        self.camp.night_target = 0.0
        yield Wait(2.5)
        self.camp.wake()

    def _after_timeskip(self):
        """What EventsScreen.timeskip_done does that matters outside the events screen."""
        from scripts.screens.screens_core.screens_core import rebuild_moon_n_season_indicator

        switch_set_value(Switch.saved_scroll_positions, {})
        switch_set_value(Switch.saved_page_positions, {})
        on_camp = game.current_screen == GameScreen.CAMP
        try:
            rebuild_moon_n_season_indicator(change_moon=True, visible=on_camp)
        except Exception:
            logger.exception("Autoplay: moon indicator rebuild failed")
        try:
            game.audio.check(should_fade_out=True)
        except AttributeError:
            pass
        if on_camp:
            camp = self._screen(GameScreen.CAMP)
            # swap the season backdrop without set_bg's screen-entry fade
            if game.clan.current_season in camp.game_bgs:
                camp.active_bg = game.clan.current_season
                camp.active_blur_bg = "default"
            camp.save_button.update_state()

    # ---------------------------------------------------------------- history

    def _scene_history(self):
        """Fast-forward whole moons so the clan has a past: the real timeskip, a
        patrol or two and the odd leader decision each moon, with no scenes. The big
        moments go into the Chronicle as backstory."""
        clan = game.clan
        if self._history is None:
            self._history = {
                "moons": 0,
                "born": 0,
                "died": 0,
                "joined": 0,
                "leader": clan.leader.ID if clan.leader else None,
                "start": clan.age,
            }
        tally = self._history
        total = tally["moons"] + self.history_left

        self.caption = "the seasons turn"
        self._close_windows()
        self.camp.bed_down()
        self.camp.night_target = 1.0
        camp = self._screen(GameScreen.CAMP)
        camp.active_blur_bg = "default"  # the season will change under us
        yield Wait(1.5)

        while self.history_left > 0 and get_living_clan_cat_count(Cat) > 0:
            self.overlay.show_card(
                Card(
                    text=f"{clan.name}\nThe seasons turn... {tally['moons'] + 1} of {total}",
                    header=f"Moon {clan.age + 1}",
                    style="title",
                    duration=3600,
                )
            )
            yield from self._quiet_moon()
            if not get_clan_setting("deputy") and not clan.deputy:
                set_clan_setting("deputy", True)
            snapshot = story.ClanSnapshot.take()

            self.critical = True
            try:
                job = self._job(events.one_moon)
                yield Until(lambda: job.done, 600)
            finally:
                self.critical = False
            if job.error is not None:
                raise job.error
            self._after_timeskip()

            moon = story.diff(snapshot)
            tally["moons"] += 1
            tally["born"] += len(moon.born)
            tally["died"] += len(moon.died)
            tally["joined"] += len(moon.joined)
            pace = self.cfg["pacing"]
            for card in story.build_moon_cards(
                list(game.cur_events_list), moon, float(pace["reading_cps"]), 1, 3
            ):
                if card.style == "major":
                    self.panels.add(card.header, card.text, card.accent, card.cats[0] if card.cats else None)
            self.history_left -= 1
            yield Wait(0.05)

        self.overlay.clear_card()
        self.history_left = 0
        self._history = None
        self.camp.refresh_roster()
        if get_living_clan_cat_count(Cat) > 0:
            try:
                camp.save_button.save_game(current_screen=camp)
            except Exception:
                logger.exception("Autoplay save failed")
            self.pending_cards = [self._story_so_far(tally), story.title_card()]
        self.camp.night_target = 0.0
        yield Wait(2.0)
        self.camp.wake()

    def _quiet_moon(self):
        """The player's part of a moon, without the show: a decision and some patrols."""
        leader = game.clan.leader
        if leader is not None and leader.status.alive_in_player_clan and leader.can_work():
            try:
                has_meds = any(
                    c.status.rank.is_any_medicine_rank() and c.status.alive_in_player_clan and c.can_work()
                    for c in Cat.all_cats_list
                )
                if brain.focus_change_allowed() and random.random() < 0.4:
                    code, target = brain.choose_focus(leader_actions.current_focus(), has_meds)
                    if code:
                        leader_actions.apply_focus(code, target)
                if game.clan.all_other_clans and random.random() < 0.25:
                    other = random.choice(game.clan.all_other_clans)
                    leader_actions.clan_interaction(leader, other, brain.choose_clan_interaction(leader, other))
                elif random.random() < 0.2:
                    leader_actions.outsider_interaction(leader)
            except Exception:
                logger.exception("Autoplay: quiet leader decision failed")

        pace = self.cfg["pacing"]
        count = brain.patrol_count(len(self._able_cats()), int(pace["min_patrols"]), int(pace["max_patrols"]))
        for _ in range(count):
            patrol_type, members = brain.plan_patrol(self._able_cats())
            if not members:
                break
            patrol = Patrol()
            job = self._job(patrol.begin_patrol, members, patrol_type)
            yield Until(lambda: job.done, 30)
            if job.error is not None or not job.result:
                continue
            choice = brain.decide_patrol(
                patrol, bool(getattr(patrol.patrol_event, "antag_success_outcomes", None))
            )
            job = self._job(patrol.proceed_patrol, choice)
            yield Until(lambda: job.done, 30)

    def _story_so_far(self, tally: dict) -> Card:
        clan = game.clan
        parts = [f"{tally['moons']} moons have passed"]
        if tally["start"] == 0:
            parts[0] += f" since {clan.name} was founded"
        parts[0] += "."
        bits = []
        if tally["born"]:
            bits.append(f"{tally['born']} kit{'s were' if tally['born'] != 1 else ' was'} born")
        if tally["joined"]:
            bits.append(f"{tally['joined']} cat{'s' if tally['joined'] != 1 else ''} joined")
        if tally["died"]:
            bits.append(f"{tally['died']} cat{'s' if tally['died'] != 1 else ''} died")
        if bits:
            parts.append((", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]).capitalize() + ".")
        leader = clan.leader
        cats = []
        if leader is not None and leader.status.alive_in_player_clan:
            cats.append(leader)
            if leader.ID != tally["leader"]:
                parts.append(f"{leader.name} now leads the clan.")
            else:
                parts.append(f"{leader.name} still leads, with {clan.leader_lives} lives left.")
        living = get_living_clan_cat_count(Cat)
        parts.append(f"{living} cats call the camp home. Their story is in the Chronicle.")
        text = " ".join(parts)
        return Card(
            text=text,
            header="The Story So Far",
            accent=story.GOLD,
            cats=cats,
            style="major",
            duration=self._read(text, 7),
        )

    # ---------------------------------------------------------------- endings

    def _scene_game_over(self):
        self._close_windows()
        if game.clan is not None:
            name, age = str(game.clan.name), game.clan.age
            self.caption = "the clan is no more"
            self.camp.night_target = 1.0
            yield from self._show(
                Card(
                    text=f"After {age} moons, the last of {name} are gone.\nStarClan remembers them.",
                    header=f"The End of {name}",
                    style="title",
                    duration=9,
                )
            )

        if not self.cfg["autoplay"]["restart_on_game_over"]:
            self.mode = "off"
            yield Wait(1)
            return

        self.caption = "a new clan gathers"
        yield Wait(2)
        if game.current_screen == GameScreen.CAMP:
            # step off camp so it rebuilds with the new clan's cats
            yield from self._goto(GameScreen.EVENTS)
        if found_random_clan(self.cfg["autoplay"]["new_clan_mode"]):
            self.progress = {}
            self.panels.clear()
            self.camp.detach()
            self.camp.night_target = 0.0
            self.camp.night = 0.0
            self.pending_cards = self._founding_cards()
            self.history_left = int(self.cfg["history"]["new_clan_moons"])
            self._history = None
            self._go(GameScreen.CAMP)
            yield Until(lambda: game.current_screen == GameScreen.CAMP and not game.switch_screens, 5)
        else:
            self.mode = "off"
            yield Wait(1)


director = Director()
