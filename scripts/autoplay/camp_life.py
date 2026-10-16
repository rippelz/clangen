"""Makes the camp feel alive: cats wander between dens, visit friends, follow their
mentors, gather for ceremonies and walk out of camp on patrol.

Each cat button on the camp screen becomes an Actor. Actors move the real
UISpriteButtons around, so clicking a cat (even mid-stride) still opens its profile.
The director scripts actors (gather, leave, return) while the rest live on their own.
All positions are in the game's unscaled 800x700 space.
"""

import logging
import math
import random
from typing import Callable, Optional

import pygame

import scripts.game_structure.screen_settings as screen_settings
from scripts.autoplay.overlay import Bubble
from scripts.cat.cats import Cat
from scripts.cat.enums import CatRank
from scripts.game_structure.game.settings import game_setting_get
from scripts.ui.elements.sprite_button import UISpriteButton

logger = logging.getLogger(__name__)

DENS = (
    "nursery place",
    "leader place",
    "elder place",
    "medicine place",
    "apprentice place",
    "clearing place",
    "warrior place",
)

# where each rank likes to be, in DENS order (mirrors ClanScreen.choose_cat_positions)
_DEN_WEIGHTS = {
    "kit": [60, 8, 1, 1, 1, 6, 1],
    "apprentice": [1, 10, 1, 1, 100, 100, 1],
    "deputy": [1, 30, 1, 1, 1, 60, 20],
    "elder": [1, 1, 400, 1, 1, 6, 1],
    "medicine": [10, 10, 10, 300, 1, 20, 1],
    "warrior": [1, 1, 1, 1, 1, 60, 60],
    "leader": [1, 200, 1, 1, 1, 30, 1],
}

WALK_SPEED = 52.0  # px per second, unscaled
EXIT = (400.0, 668.0)


def _rank_key(cat) -> str:
    rank = cat.status.rank
    if rank in (CatRank.NEWBORN, CatRank.KITTEN):
        return "kit"
    if rank.is_any_apprentice_rank() and not rank.is_any_medicine_rank():
        return "apprentice"
    if rank.is_any_medicine_rank():
        return "medicine"
    if rank == CatRank.DEPUTY:
        return "deputy"
    if rank == CatRank.ELDER:
        return "elder"
    if cat.status.is_leader:
        return "leader"
    return "warrior"


class Actor:
    def __init__(self, button: UISpriteButton, pos: tuple[float, float]):
        self.button = button
        self.cat_id = button.cat_id
        self.x, self.y = pos
        self.path: list[tuple[float, float]] = []
        self.speed = 1.0
        self.idle = random.uniform(3, 14)  # seconds until the next ambient choice
        self.scripted = False  # the director owns this cat right now
        self.away = False  # out of camp (patrol)
        self.on_arrive: Optional[Callable] = None
        self.phase = random.random()
        self.highlight = 0.0
        self.sleeping = False

    @property
    def cat(self):
        return Cat.fetch_cat(self.cat_id)

    @property
    def walking(self) -> bool:
        return bool(self.path)

    def go(self, points, speed=1.0, on_arrive=None):
        self.path = [tuple(p) for p in points]
        self.speed = speed
        self.on_arrive = on_arrive


class CampLife:
    def __init__(self):
        self.camp = None
        self.actors: dict[str, Actor] = {}
        self._buttons_id = None
        self.ambient = True
        self.night = 0.0  # 0 day .. 1 full night, eased towards night_target
        self.night_target = 0.0
        self.say: Optional[Callable[[Actor, str], None]] = None  # set by the director

    # ------------------------------------------------------------ attachment

    def sync(self, camp) -> bool:
        """Adopt the camp screen's cat buttons. Returns True if attached."""
        buttons = getattr(camp, "cat_buttons", None)
        if not buttons or getattr(camp, "layout", None) is None:
            self.camp = None
            self.actors = {}
            self._buttons_id = None
            return False
        if self.camp is camp and self._buttons_id == id(buttons):
            return True
        self.camp = camp
        self._buttons_id = id(buttons)
        self.actors = {}
        for b in buttons:
            if not b.alive():
                continue
            cat = Cat.fetch_cat(b.cat_id)
            if cat is None:
                continue
            pos = tuple(cat.placement) if cat.placement else self._unscale(b.relative_rect.topleft)
            self.actors[b.cat_id] = Actor(b, (float(pos[0]), float(pos[1])))
        return True

    def detach(self):
        self.camp = None
        self.actors = {}
        self._buttons_id = None

    @staticmethod
    def _unscale(p):
        s = screen_settings.screen_scale
        return p[0] / s, p[1] / s

    # ------------------------------------------------------------ places

    def spots(self, den: str) -> list[tuple[float, float]]:
        layout = self.camp.layout if self.camp else None
        if not layout or den not in layout:
            return []
        return [(float(p[0][0]), float(p[0][1])) for p in layout[den]]

    @property
    def hub(self) -> tuple[float, float]:
        pts = self.spots("clearing place")
        if not pts:
            return 375.0, 400.0
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    @property
    def leader_spot(self) -> tuple[float, float]:
        pts = self.spots("leader place")
        return pts[0] if pts else self.hub

    def _free_spot(self, den: str, exclude: Actor) -> Optional[tuple[float, float]]:
        pts = self.spots(den)
        if not pts:
            return None
        random.shuffle(pts)
        for p in pts:
            if all(
                a is exclude or math.dist((a.x, a.y), p) > 22 and (not a.path or math.dist(a.path[-1], p) > 22)
                for a in self.actors.values()
            ):
                return p
        return None

    def home_spot(self, actor: Actor) -> tuple[float, float]:
        cat = actor.cat
        weights = list(_DEN_WEIGHTS[_rank_key(cat)]) if cat else [1] * len(DENS)
        for _ in range(6):
            den = random.choices(DENS, weights)[0]
            p = self._free_spot(den, actor)
            if p:
                return p
        return actor.x, actor.y

    def route(self, actor: Actor, dest) -> list[tuple[float, float]]:
        """Paths run through the clearing, so cats walk around the camp, not through dens."""
        hub = self.hub
        start = (actor.x, actor.y)
        if math.dist(start, dest) < 140 or math.dist(start, hub) < 60 or math.dist(dest, hub) < 60:
            return [dest]
        jitter = (hub[0] + random.uniform(-30, 30), hub[1] + random.uniform(-20, 20))
        return [jitter, dest]

    # ------------------------------------------------------------ scripting API

    def present(self, cat_ids=None) -> list[Actor]:
        acts = [a for a in self.actors.values() if not a.away and a.button.alive()]
        if cat_ids is not None:
            ids = set(cat_ids)
            acts = [a for a in acts if a.cat_id in ids]
        return acts

    def gather(self, cat_ids, point, radius=34.0, speed=1.15) -> list[Actor]:
        acts = self.present(cat_ids)
        n = len(acts)
        for i, a in enumerate(acts):
            ang = (i / max(1, n)) * math.tau + random.uniform(-0.2, 0.2)
            r = radius if n > 1 else 0
            dest = (point[0] + math.cos(ang) * r, point[1] + math.sin(ang) * r * 0.6)
            a.scripted = True
            a.sleeping = False
            a.go(self.route(a, dest), speed)
        return acts

    def release(self, cat_ids=None):
        for a in self.actors.values():
            if cat_ids is None or a.cat_id in cat_ids:
                a.scripted = False
                a.highlight = 0.0
                a.idle = random.uniform(1.5, 6)

    def arrived(self, acts) -> bool:
        return all(not a.walking for a in acts)

    def leave(self, cat_ids, speed=1.2) -> list[Actor]:
        acts = self.present(cat_ids)
        for a in acts:
            a.scripted = True
            exit_pt = (EXIT[0] + random.uniform(-25, 25), EXIT[1])

            def gone(actor=a):
                actor.away = True
                actor.button.hide()

            a.go(self.route(a, exit_pt) + [(exit_pt[0], exit_pt[1] + 30)], speed, gone)
        return acts

    def come_back(self, cat_ids=None, speed=1.0) -> list[Actor]:
        acts = [
            a for a in self.actors.values() if a.away and (cat_ids is None or a.cat_id in cat_ids)
        ]
        for a in acts:
            cat = a.cat
            if cat is None or not cat.status.alive_in_player_clan:
                continue
            a.away = False
            a.x, a.y = EXIT[0] + random.uniform(-25, 25), EXIT[1] + 30
            a.button.show()
            a.scripted = True
            a.go([(a.x, EXIT[1])] + self.route(a, self.hub)[-1:], speed, lambda actor=a: self.release([actor.cat_id]))
        return acts

    def bed_down(self):
        """Evening: everyone heads to their den and curls up."""
        for a in self.present():
            a.scripted = True
            a.go(self.route(a, self.home_spot(a)), 0.9, lambda actor=a: setattr(actor, "sleeping", True))

    def wake(self):
        for a in self.actors.values():
            a.sleeping = False
        self.release()

    def highlight(self, cat_ids, seconds=6.0):
        for a in self.present(cat_ids):
            a.highlight = seconds

    def reset(self):
        """The director let go mid-scene: bring everyone back to normal life."""
        for a in self.actors.values():
            if a.away:
                cat = a.cat
                if cat is not None and cat.status.alive_in_player_clan:
                    a.away = False
                    a.button.show()
                    a.x, a.y = self.home_spot(a)
            a.path = []
            a.on_arrive = None
            a.sleeping = False
        self.release()
        self.night_target = 0.0

    # ------------------------------------------------------------ roster changes

    def refresh_roster(self, newborn_ids=()):
        """After a timeskip or patrol: drop cats who are gone, add cats who arrived,
        and refresh sprites (cats grow up)."""
        if self.camp is None:
            return
        camp = self.camp
        living = {
            c.ID: c
            for c in Cat.all_cats_list
            if c.status.alive_in_player_clan and c.status.rank != CatRank.NEWBORN
        }
        for cid in list(self.actors):
            a = self.actors[cid]
            if cid not in living:
                a.button.kill()
                if a.button in camp.cat_buttons:
                    camp.cat_buttons.remove(a.button)
                del self.actors[cid]
            else:
                self._set_sprite(a)

        for cid, cat in living.items():
            if cid in self.actors or len(self.actors) >= camp.max_sprites_displayed:
                continue
            nursery = self.spots("nursery place")
            if cid in newborn_ids or (cat.status.rank == CatRank.KITTEN and nursery):
                pos = random.choice(nursery) if nursery else self.hub
                arrive_walking = False
            else:
                pos = (EXIT[0] + random.uniform(-25, 25), EXIT[1] + 30)
                arrive_walking = True
            button = self._make_button(cat, pos)
            if button is None:
                continue
            camp.cat_buttons.append(button)
            a = Actor(button, pos)
            self.actors[cid] = a
            if arrive_walking:
                a.scripted = True
                a.go([(pos[0], EXIT[1]), self.hub], 0.9, lambda actor=a: self.release([actor.cat_id]))
        self._buttons_id = id(camp.cat_buttons)

    def _shaded(self, cat, pos) -> pygame.Surface:
        """The camp screen tints sprites to the ground they stand on; do the same."""
        camp = self.camp
        image = cat.sprite.convert_alpha()
        try:
            bg = camp.game_bgs[camp.active_bg]
            s = screen_settings.screen_scale
            rect = pygame.Rect(int(pos[0] * s), int(pos[1] * s), int(50 * s), int(50 * s)).clip(bg.get_rect())
            if rect.width <= 0 or rect.height <= 0:
                return image
            blend = pygame.Surface(rect.size)
            blend.fill(pygame.transform.average_color(bg.subsurface(rect)))
            blend = pygame.transform.scale(blend, image.get_size())
            shading = camp.layout.get("cat_shading", {})
            sprite = image.copy()
            sprite.fill((255, 255, 255, 255), special_flags=pygame.BLEND_RGB_MAX)
            sprite.blit(blend, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            image.set_alpha(shading.get("blend_strength", 200))
            sprite.blit(image, (0, 0), special_flags=pygame.BLEND_ALPHA_SDL2)
            sprite.set_alpha(255)
            return sprite
        except Exception:
            return image

    def _make_button(self, cat, pos) -> Optional[UISpriteButton]:
        s = screen_settings.screen_scale
        try:
            return UISpriteButton(
                pygame.Rect(int(pos[0] * s), int(pos[1] * s), int(50 * s), int(50 * s)),
                self._shaded(cat, pos),
                mask=cat.sprite_mask,
                cat_id=cat.ID,
                starting_height=2,
            )
        except Exception:
            logger.exception("Autoplay: couldn't add %s to camp", cat.name)
            return None

    def _set_sprite(self, a: Actor):
        cat = a.cat
        if cat is None:
            return
        try:
            size = a.button.cat_image.relative_rect.size
            sprite = self._shaded(cat, (a.x, a.y)).premul_alpha()
            if game_setting_get("no sprite antialiasing"):
                sprite = pygame.transform.scale(sprite, size)
            else:
                sprite = pygame.transform.smoothscale(sprite, size)
            a.button.cat_image.set_image(sprite)
        except Exception:
            logger.exception("Autoplay: couldn't refresh a sprite")

    # ------------------------------------------------------------ per frame

    def update(self, dt: float, ambient: bool):
        if self.camp is None:
            return
        # ease the night tint
        step = dt / 2.5
        if self.night < self.night_target:
            self.night = min(self.night_target, self.night + step)
        elif self.night > self.night_target:
            self.night = max(self.night_target, self.night - step)

        s = screen_settings.screen_scale
        for a in list(self.actors.values()):
            if not a.button.alive():
                continue
            a.highlight = max(0.0, a.highlight - dt)
            hop = 0.0
            if a.path:
                tx, ty = a.path[0]
                dx, dy = tx - a.x, ty - a.y
                dist = math.hypot(dx, dy)
                move = WALK_SPEED * a.speed * dt
                if dist <= move:
                    a.x, a.y = tx, ty
                    a.path.pop(0)
                    if not a.path and a.on_arrive:
                        cb, a.on_arrive = a.on_arrive, None
                        try:
                            cb()
                        except Exception:
                            logger.exception("Autoplay: actor callback failed")
                else:
                    a.x += dx / dist * move
                    a.y += dy / dist * move
                a.phase += dt * 2.6 * a.speed
                hop = abs(math.sin(a.phase * math.pi)) * 3.0
            elif ambient and not a.scripted and not a.away:
                a.idle -= dt
                if a.idle <= 0:
                    try:
                        self._ambient(a)
                    except Exception:
                        logger.debug("Autoplay ambient choice failed", exc_info=True)
                    a.idle = random.uniform(6, 18)
            pos = (int(a.x * s), int((a.y - hop) * s))
            if a.button.relative_rect.topleft != pos:
                a.button.set_relative_position(pos)
                a.button.cat_image.set_relative_position(pos)
                a.button.target_indicator.set_relative_position(pos)

    def _ambient(self, a: Actor):
        cat = a.cat
        if cat is None or not cat.status.alive_in_player_clan:
            return
        if not cat.can_work() and cat.status.rank not in (CatRank.KITTEN, CatRank.ELDER):
            return  # the sick and injured stay put
        key = _rank_key(cat)
        roll = random.random()
        here = {o.cat_id: o for o in self.present() if o is not a and not o.scripted}

        if key == "elder" and roll < 0.7:
            return  # elders mostly doze
        if key == "kit" and roll < 0.45:
            friends = [o for o in here.values() if o.cat and _rank_key(o.cat) == "kit"]
            if friends:
                other = random.choice(friends)
                a.go([(other.x + random.uniform(-22, 22), other.y + random.uniform(-10, 10))], 1.6)
                if random.random() < 0.3:
                    self._say(a, f"pounces on {other.cat.name}")
                return
        if key == "apprentice" and cat.mentor in here and roll < 0.4:
            mentor = here[cat.mentor]
            a.go(self.route(a, (mentor.x + 26, mentor.y + 6)), 1.2)
            if random.random() < 0.4:
                self._say(a, f"practises a hunting crouch for {mentor.cat.name}")
            return
        if key == "medicine" and roll < 0.5:
            patients = [o for o in here.values() if o.cat and not o.cat.can_work() and _rank_key(o.cat) != "kit"]
            if patients:
                p = random.choice(patients)
                a.go(self.route(a, (p.x + 24, p.y + 4)), 1.0, lambda: self._say(a, f"tends to {p.cat.name}"))
                return
        mates = [here[m] for m in cat.mate if m in here]
        if mates and roll < 0.3:
            m = random.choice(mates)
            a.go(self.route(a, (m.x - 24, m.y + 2)), 0.9, lambda: self._say(a, f"shares tongues with {m.cat.name}"))
            return
        if roll < 0.12 and here and key not in ("kit", "elder"):
            other = random.choice(list(here.values()))
            if other.cat and other.cat.status.rank.is_baby() is False:
                a.go(self.route(a, (other.x + 26, other.y)), 1.0, lambda: self._say(a, f"chats with {other.cat.name}"))
                return
        if roll < 0.75:
            a.go(self.route(a, self.home_spot(a)), random.uniform(0.8, 1.1))

    def _say(self, a: Actor, text: str):
        if self.say:
            self.say(a, text)

    # ------------------------------------------------------------ drawing

    def draw(self, surface: pygame.Surface, full_screen: bool = False):
        if self.camp is None:
            return
        s = screen_settings.screen_scale
        ox, oy = screen_settings.offset
        if self.night > 0.01:
            if full_screen:
                tint = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
                ox = oy = 0
            else:
                tint = pygame.Surface((int(800 * s), int(700 * s)), pygame.SRCALPHA)
            tint.fill((12, 16, 44, int(150 * self.night)))
            surface.blit(tint, (ox, oy))
        for a in self.actors.values():
            if a.highlight <= 0 or a.away or not a.button.alive():
                continue
            alpha = int(200 * min(1.0, a.highlight / 0.6))
            w, h = int(46 * s), int(14 * s)
            ring = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.ellipse(ring, (255, 226, 140, alpha), ring.get_rect(), max(2, int(2 * s)))
            r = a.button.rect
            surface.blit(ring, (r.centerx - w // 2, r.bottom - h + int(2 * s)))

    def bubble_for(self, a: Actor, name: str, text: str, duration=4.2) -> Bubble:
        return Bubble(a.button.rect.copy(), name, text, duration=duration, follow=a.button)
