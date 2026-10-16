"""The autoplay director: plays ClanGen like a patient, attentive player.

It drives the real screens (patrol screen, events screen, dens) by posting the same
pygame_gui button events a mouse click would, so every bit of game logic runs
exactly as it does for a human. Each moon it:

  camp (thought bubbles) -> leadership (focus / leader's den) -> patrols
  -> timeskip -> the moon's story, card by card -> a headline profile spotlight

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
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pygame
import pygame_gui

import scripts.game_structure.screen_settings as screen_settings
from scripts.autoplay import brain, story
from scripts.autoplay.clan_factory import found_random_clan
from scripts.autoplay.config import load_config
from scripts.autoplay.overlay import Bubble, Card, Overlay, clean_text
from scripts.autoplay.panels import Panels
from scripts.cat.cats import Cat
from scripts.cat.enums import CatRank
from scripts.clan_package.get_clan_cats import get_living_clan_cat_count
from scripts.clan_package.settings import get_clan_setting, set_clan_setting
from scripts.events_module.patrol.enums import PatrolChoice
from scripts.events_module.thoughts.generate_thoughts import get_new_thought
from scripts.game_structure import game
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


class _Abort(Exception):
    pass


class Director:
    def __init__(self):
        self.cfg = load_config()
        self.enabled = bool(self.cfg["autoplay"]["enabled"])
        self.speed = float(self.cfg["autoplay"]["speed"]) or 1.0
        self.idle_resume = float(self.cfg["autoplay"]["idle_resume_seconds"])

        self.overlay = Overlay()
        self.panels = Panels()
        self.mode = "auto" if self.enabled else "off"
        self.last_input = 0.0

        self.task = None
        self.wait = None
        self.critical = False  # true while a timeskip is in flight: don't abandon it
        self._manual_requested = False
        self.caption = ""

        # story state survives interruptions
        self.pending_cards: list[Card] = []
        self.pending_headline = (None, "")
        self.progress = {}
        self._last_thought_cat = None
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
        else:
            self.pending_cards = [story.title_card()]
            self.pending_cards[0].header = f"Moon {game.clan.age}"
        self._go(GameScreen.CAMP)

    def on_clan_loaded(self):
        """The player switched clans: forget anything tied to the old one."""
        self._drop_task()
        self.pending_cards.clear()
        self.pending_headline = (None, "")
        self.progress = {}
        self.panels.clear()

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
        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN, pygame.MOUSEWHEEL, pygame.QUIT):
            self.last_input = time.monotonic()
            if self.mode == "auto":
                self._enter_manual()

    def update(self, dt: float):
        self.overlay.update(dt * self.speed if self.mode == "auto" else dt)
        self.panels.update(dt)

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

    def draw(self, surface: pygame.Surface):
        self.panels.draw(surface, self.overlay.hud_text)
        self.overlay.draw(
            surface,
            show_story=self.mode == "auto",
            show_bubbles=game.current_screen == GameScreen.CAMP and not game.switch_screens,
        )
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
                w.elapsed += dt
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

    def _press(self, element, kind=pygame_gui.UI_BUTTON_START_PRESS):
        if element is None:
            return False
        try:
            if not element.is_enabled:
                return False
            self.overlay.add_ripple(element.rect.center)
        except AttributeError:
            pass
        pygame.event.post(
            pygame.event.Event(
                kind,
                {
                    "ui_element": element,
                    "ui_object_id": getattr(element, "most_specific_combined_id", ""),
                },
            )
        )
        return True

    def _close_windows(self):
        try:
            for window in list(screen_settings.MANAGER.get_window_stack().get_full_stack()):
                window.kill()
        except Exception:
            logger.exception("Autoplay: couldn't close open windows")

    def _work_in_flight(self) -> bool:
        es = game.all_screens.get(GameScreen.EVENTS)
        ps = game.all_screens.get(GameScreen.PATROL)
        threads = [
            getattr(es, "events_thread", None),
            getattr(ps, "start_patrol_thread", None),
            getattr(ps, "proceed_patrol_thread", None),
        ]
        return any(t is not None and t.is_alive() for t in threads)

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

    @staticmethod
    def _read(text: str, cps: float, minimum: float = 3.0) -> float:
        return story.reading_time(text or "", cps, minimum)

    # ================================================================ the show

    def _main_loop(self):
        pace = self.cfg["pacing"]
        while True:
            if game.clan is None or get_living_clan_cat_count(Cat) == 0:
                yield from self._scene_game_over()
                continue

            if self.pending_cards:
                yield from self._scene_story()
                continue

            prog = self._moon_progress()
            if not prog["camp"]:
                yield from self._scene_camp(pace["camp_seconds"])
                prog["camp"] = True
            if not prog["leadership"]:
                prog["leadership"] = True
                yield from self._scene_leadership()
            yield from self._scene_patrols()
            yield from self._scene_timeskip()

    # ---------------------------------------------------------------- camp

    def _scene_camp(self, seconds: float, caption: str = "resting in camp"):
        yield from self._goto(GameScreen.CAMP)
        self.caption = caption
        interval = float(self.cfg["pacing"]["thought_interval"])
        t, next_thought = 0.0, 1.2
        while t < seconds:
            yield Wait(0.25)
            t += 0.25
            if t >= next_thought:
                self._spawn_thought()
                next_thought += interval * random.uniform(0.7, 1.3)

    def _spawn_thought(self):
        camp = self._screen(GameScreen.CAMP)
        buttons = [b for b in getattr(camp, "cat_buttons", []) if b.visible]
        if not buttons:
            return
        choices = [b for b in buttons if b.cat_id != self._last_thought_cat] or buttons
        button = random.choice(choices)
        cat = Cat.fetch_cat(button.cat_id)
        if not isinstance(cat, Cat):
            return
        if not cat.thought:
            try:
                get_new_thought(cat, cat.next_thought_type)
            except Exception:
                return
        if not cat.thought:
            return
        self._last_thought_cat = cat.ID
        self.overlay.add_bubble(
            Bubble(button.rect.copy(), str(cat.name), str(cat.thought), duration=4.2)
        )

    # ---------------------------------------------------------------- story

    def _scene_story(self):
        yield from self._goto(GameScreen.CAMP)
        self.caption = "telling the moon's story"
        while self.pending_cards:
            card = self.pending_cards.pop(0)
            self.overlay.show_card(card)
            if card.style != "title":
                self.panels.add(card.header, card.text, card.accent, card.cats[0] if card.cats else None)
            while not card.done:
                yield Wait(0.1)
            self.overlay.clear_card()
            yield Wait(0.35)

        cat, caption = self.pending_headline
        self.pending_headline = (None, "")
        if cat is not None and self.cfg["story"]["profile_spotlights"]:
            yield from self._scene_profile(cat, caption)

    def _scene_profile(self, cat, caption: str, seconds: float = 10.0):
        switch_set_value(Switch.cat, cat.ID)
        game.last_screen_forProfile = GameScreen.CAMP
        yield from self._goto(GameScreen.PROFILE)
        self.caption = caption
        yield Wait(seconds)
        yield from self._goto(GameScreen.CAMP)

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
                duration=self._read(text, self.cfg["pacing"]["reading_cps"], 6),
            ),
        ]

    # ---------------------------------------------------------------- leadership

    def _scene_leadership(self):
        has_meds = any(
            c.status.rank.is_any_medicine_rank() and c.status.alive_in_player_clan and c.can_work()
            for c in Cat.all_cats_list
        )
        if brain.focus_change_allowed() and random.random() < 0.6:
            current = next((f for f in brain.FOCUS_ALL if get_clan_setting(f)), "business_as_usual")
            code, target = brain.choose_focus(current, has_meds)
            if code:
                yield from self._scene_focus(code, target)

        leader = game.clan.leader
        if (
            leader
            and leader.status.alive_in_player_clan
            and game.clan.all_other_clans
            and random.random() < 0.45
        ):
            yield from self._scene_leader_den(leader)

    def _scene_focus(self, code: str, target):
        yield from self._goto(GameScreen.WARRIOR_DEN)
        self.caption = "the leader weighs the clan's focus"
        wd = self._screen(GameScreen.WARRIOR_DEN)
        yield Wait(2.5)
        button = getattr(wd, "focus_buttons", {}).get(code)
        if button is not None and self._press(button):
            yield Wait(2.5)
            if code in wd.other_clan_settings:
                if target is not None:
                    game.clan.clans_in_focus = [target.name]
                    wd.save_focus()
            else:
                self._press(wd.save_button)
            self.caption = f"new focus: {code.replace('_', ' ')}"
            yield Wait(3.0)
        yield from self._goto(GameScreen.CAMP)

    def _scene_leader_den(self, leader):
        yield from self._goto(GameScreen.LEADER_DEN)
        ld = self._screen(GameScreen.LEADER_DEN)
        self.caption = f"{leader.name} considers the other clans"
        yield Wait(2.5)
        if ld.no_leader:
            yield from self._goto(GameScreen.CAMP)
            return

        clans = game.clan.all_other_clans
        weights = [
            2.0 if c in brain.hostile_clans() or c in brain.friendly_clans() else 1.0
            for c in clans
        ]
        idx = random.choices(range(len(clans)), weights)[0]
        if self._press(ld.other_clan_selection_elements.get(f"button{idx}")):
            yield Wait(2.0)
        stance = brain.choose_clan_interaction(leader, ld.focus_clan)
        if self._press(ld.focus_frame_elements.get(f"{stance}_interaction")):
            self.caption = f"{leader.name} chooses to {'provoke' if stance == 'negative' else 'befriend'} {ld.focus_clan.name}"
            yield Wait(4.0)

        if random.random() < 0.35 and self._press(ld.focus_frame_elements.get("outsiders_tab")):
            self.caption = f"{leader.name} thinks about the cats beyond the borders"
            yield Wait(2.0)
            outsiders = list(ld.outsider_cat_buttons.values())
            if outsiders and self._press(random.choice(outsiders)):
                yield Wait(2.0)
                available = {
                    b.text.replace("screens.leader_den.", ""): b
                    for b in ld.focus_button.values()
                    if b.alive() and b.visible and b.is_enabled
                }
                action = brain.choose_outsider_action(leader, list(available))
                if action and self._press(available[action]):
                    yield Wait(4.0)
        yield from self._goto(GameScreen.CAMP)

    # ---------------------------------------------------------------- patrols

    def _scene_patrols(self):
        prog = self._moon_progress()
        ps = self._screen(GameScreen.PATROL)
        pace = self.cfg["pacing"]

        # pick an interrupted patrol back up rather than abandoning the cats mid-event
        data = ps.in_progress_data
        if (
            data
            and data.get("patrol_stage") == "patrol_events"
            and data.get("current_moon") == game.clan.age
            and data.get("clan_name") == game.clan.name
        ) or (game.current_screen == GameScreen.PATROL and ps.patrol_stage == "patrol_events"):
            yield from self._goto(GameScreen.PATROL)
            if ps.patrol_stage == "patrol_events":
                self.caption = "the patrol continues"
                yield from self._finish_patrol(ps)

        while True:
            if prog["patrols_planned"] is not None and prog["patrols_done"] >= prog["patrols_planned"]:
                break
            ps.in_progress_data = None
            if game.current_screen == GameScreen.PATROL and ps.patrol_stage == "patrol_complete":
                self._press(ps.elements.get("patrol_again"))
                yield Until(lambda: ps.patrol_stage == "choose_cats", 3)
            else:
                yield from self._goto(GameScreen.PATROL)
                if ps.patrol_stage != "choose_cats":
                    ps.open_choose_cats_screen()

            able = list(ps.able_cats or [])
            if prog["patrols_planned"] is None:
                prog["patrols_planned"] = brain.patrol_count(
                    len(able), int(pace["min_patrols"]), int(pace["max_patrols"])
                )
            if prog["patrols_done"] >= prog["patrols_planned"]:
                break

            patrol_type, members = brain.plan_patrol(able)
            if not members:
                prog["patrols_planned"] = prog["patrols_done"]
                break

            self.caption = f"choosing cats for a {_PATROL_LABELS.get(patrol_type, 'patrol')}"
            yield Wait(1.5)
            for cat in members:
                ps.selected_cat = cat
                ps.update_selected_cat()
                ps.update_button()
                yield Wait(1.0)
                if cat not in ps.current_patrol and len(ps.current_patrol) < 6:
                    self._press(ps.elements.get("add_remove_cat"))
                    yield Until(lambda c=cat: c in ps.current_patrol, 2)
                yield Wait(0.4)

            if not ps.current_patrol:
                prog["patrols_done"] += 1
                continue

            type_buttons = {"training": "paw", "border": "claws", "hunting": "mouse", "med": "herb"}
            if ps.patrol_type != patrol_type:
                # buttons toggle, so pressing the active one returns to a general patrol
                if patrol_type in type_buttons:
                    button = type_buttons[patrol_type]
                elif ps.patrol_type in type_buttons and ps.patrol_type != "med":
                    button = type_buttons[ps.patrol_type]
                else:
                    button = None
                if button and self._press(ps.elements.get(button)):
                    yield Wait(1.2)
            yield Wait(0.8)

            if not self._press(ps.elements.get("patrol_start")):
                prog["patrols_done"] += 1
                continue
            self.caption = f"the {_PATROL_LABELS.get(ps.patrol_type, 'patrol')} sets out"
            started = Until(
                lambda: ps.patrol_stage == "patrol_events" or game.current_screen != GameScreen.PATROL,
                30,
            )
            yield started
            prog["patrols_done"] += 1
            if game.current_screen != GameScreen.PATROL or not started.ok:
                continue
            yield from self._finish_patrol(ps)

        if game.current_screen == GameScreen.PATROL:
            ps.in_progress_data = None
            if not self._press(ps.elements.get("clan_return")):
                self._go(GameScreen.CAMP)
            yield Until(lambda: game.current_screen == GameScreen.CAMP and not game.switch_screens, 5)


    def _finish_patrol(self, ps):
        """Read the patrol intro, make the call, and read the outcome."""
        pace = self.cfg["pacing"]
        yield Until(lambda: "proceed" in ps.elements, 2)

        yield Wait(self._read(ps.display_text, pace["reading_cps"], 5))
        antag = ps.elements.get("antagonize")
        can_antag = bool(antag is not None and antag.visible)
        choice = brain.decide_patrol(ps.patrol_obj, can_antag)
        button_name, verb = {
            PatrolChoice.PROCEED: ("proceed", "presses on"),
            PatrolChoice.DECLINE: ("not_proceed", "turns back"),
            PatrolChoice.ANTAGONIZE: ("antagonize", "picks a fight"),
        }[choice]
        self.caption = f"the patrol {verb}"
        if not self._press(ps.elements.get(button_name)):
            self._press(ps.elements.get("proceed"))
        yield Until(lambda: ps.patrol_stage == "patrol_complete", 30)
        result = clean_text(ps.results_text or "")
        if result:
            self.panels.add(_PATROL_LABELS.get(ps.patrol_type, "patrol").capitalize(), result, (96, 110, 70))
        yield Wait(
            self._read(f"{ps.display_text} {ps.results_text}", pace["reading_cps"], 5) + 1
        )

    # ---------------------------------------------------------------- timeskip

    def _scene_timeskip(self):
        yield from self._goto(GameScreen.EVENTS)
        es = self._screen(GameScreen.EVENTS)
        self.caption = "the moon wanes"
        yield Wait(2.0)

        self._close_windows()  # a stray dialog would swallow the timeskip click
        if not get_clan_setting("deputy") and not game.clan.deputy:
            # with no player to appoint one, a deputy-less clan can't survive its leader
            set_clan_setting("deputy", True)
        snapshot = story.ClanSnapshot.take()
        old_thread = es.events_thread
        if not self._press(es.timeskip_button, pygame_gui.UI_BUTTON_PRESSED):
            yield Wait(2.0)
            return

        self.critical = True
        try:
            self.caption = "a moon passes..."
            began = Until(lambda: es.events_thread is not old_thread, 5)
            yield began
            if not began.ok:
                # something (a dialog?) swallowed the click; don't replay the old moon
                self.caption = "waiting..."
                yield Wait(2.0)
                return
            yield Until(
                lambda: es.events_thread is not None
                and not es.events_thread.is_alive()
                and "work_thread" not in es.work_done,
                300,
            )
            # timeskip_done() runs in EventsScreen.on_use the frame the thread finishes
            yield Wait(0.2)
        finally:
            self.critical = False

        moon = story.diff(snapshot)
        pace, st = self.cfg["pacing"], self.cfg["story"]
        self.pending_cards = story.build_moon_cards(
            list(game.cur_events_list),
            moon,
            float(pace["reading_cps"]),
            float(pace["card_min_seconds"]),
            int(st["card_min_importance"]),
        )
        self.pending_headline = story.headline_cat(moon)

        if self.cfg["autoplay"]["save_every_moon"] and get_living_clan_cat_count(Cat) > 0:
            try:
                es.save_button.save_game(current_screen=es)
            except Exception:
                logger.exception("Autoplay save failed")

        self.caption = "a new moon rises"
        yield Wait(3.5)  # let the viewer glance at the event log

    # ---------------------------------------------------------------- endings

    def _scene_game_over(self):
        self._close_windows()
        if game.clan is not None:
            name, age = str(game.clan.name), game.clan.age
            self.caption = "the clan is no more"
            card = Card(
                text=f"After {age} moons, the last of {name} are gone.\nStarClan remembers them.",
                header=f"The End of {name}",
                style="title",
                duration=9,
            )
            self.overlay.show_card(card)
            while not card.done:
                yield Wait(0.1)
            self.overlay.clear_card()

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
            self.pending_cards = self._founding_cards()
            self._go(GameScreen.CAMP)
            yield Until(lambda: game.current_screen == GameScreen.CAMP and not game.switch_screens, 5)
        else:
            self.mode = "off"
            yield Wait(1)


director = Director()
