"""Raw-pygame drawing for the autoplay layer: story cards, HUD, thought bubbles.

Everything here is drawn on top of pygame_gui after MANAGER.draw_ui(), and never
receives input, so it can't get in the way of manual play.
All layout coordinates are in the game's unscaled 800x700 space.
"""

import html
import re
from dataclasses import dataclass, field
from typing import Optional

import pygame

import scripts.game_structure.screen_settings as screen_settings
from scripts.game_structure.game.settings import game_setting_get
from scripts.ui.generate_box import BoxStyles, get_box

_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text: str) -> str:
    """Strip pygame_gui html markup out of event text."""
    text = text.replace("<br>", "\n").replace("<br/>", "\n")
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _s(v: float) -> int:
    return int(v * screen_settings.screen_scale)


def _pos(x: float, y: float) -> tuple[int, int]:
    return (
        int(x * screen_settings.screen_scale + screen_settings.offset[0]),
        int(y * screen_settings.screen_scale + screen_settings.offset[1]),
    )


class _Fonts:
    def __init__(self):
        self._cache = {}
        self._scale = None

    def get(self, kind: str, size: int) -> pygame.font.Font:
        if self._scale != screen_settings.screen_scale:
            self._cache.clear()
            self._scale = screen_settings.screen_scale
        key = (kind, size)
        if key not in self._cache:
            path = {
                "title": "resources/fonts/clangen.ttf",
                "bold": "resources/fonts/NotoSans-Bold.ttf",
                "italic": "resources/fonts/NotoSans-Italic.ttf",
            }.get(kind, "resources/fonts/NotoSans-Medium.ttf")
            try:
                self._cache[key] = pygame.font.Font(path, max(8, _s(size)))
            except (OSError, FileNotFoundError):
                self._cache[key] = pygame.font.Font(None, max(8, _s(size)))
        return self._cache[key]


fonts = _Fonts()


def wrap(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            trial = f"{line} {word}".strip()
            if font.size(trial)[0] <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _palette():
    dark = game_setting_get("dark mode")
    if dark:
        return {
            "box": BoxStyles.DARK_ROUNDED_BOX,
            "text": (239, 229, 206),
            "muted": (190, 176, 150),
            "shadow": (20, 16, 10),
        }
    return {
        "box": BoxStyles.ROUNDED_BOX,
        "text": (51, 40, 26),
        "muted": (110, 92, 66),
        "shadow": (255, 248, 230),
    }


def _box(w: int, h: int) -> pygame.Surface:
    try:
        return get_box(_palette()["box"], (w, h)).copy()
    except Exception:  # theme assets missing: draw a plain panel instead
        surf = pygame.Surface((_s(w), _s(h)), pygame.SRCALPHA)
        pygame.draw.rect(
            surf, (206, 194, 168, 235), surf.get_rect(), border_radius=_s(10)
        )
        return surf


def _sprite(cat, size: int) -> Optional[pygame.Surface]:
    try:
        return pygame.transform.scale(cat.sprite, (_s(size), _s(size)))
    except Exception:
        return None


@dataclass
class Card:
    """One thing the narrator puts on screen."""

    text: str
    header: str = ""
    accent: tuple = (140, 110, 70)
    cats: list = field(default_factory=list)
    style: str = "major"  # "major", "minor" or "title"
    duration: float = 5.0
    age: float = 0.0
    art: Optional[pygame.Surface] = None  # patrol art, shown instead of sprites
    footer: str = ""  # smaller line under the text (patrol results)

    FADE = 0.45

    @property
    def alpha(self) -> int:
        a = min(1.0, self.age / self.FADE, max(0.0, self.duration - self.age) / self.FADE)
        return int(255 * max(0.0, a))

    @property
    def done(self) -> bool:
        return self.age >= self.duration


@dataclass
class Bubble:
    rect: pygame.Rect
    name: str
    text: str
    duration: float = 4.0
    age: float = 0.0
    follow: object = None  # a UI element to stay above while it moves

    @property
    def alpha(self) -> int:
        a = min(1.0, self.age / 0.3, max(0.0, self.duration - self.age) / 0.4)
        return int(255 * max(0.0, a))


@dataclass
class Ripple:
    pos: tuple
    age: float = 0.0
    duration: float = 0.6


class Overlay:
    def __init__(self):
        self.card: Optional[Card] = None
        self.bubbles: list[Bubble] = []
        self.ripples: list[Ripple] = []
        self.hud_text = ""
        self.hud_mode = "auto"  # "auto", "manual", "off"
        self.caption = ""  # small line under the HUD describing what the director does
        self._card_cache = None

    # ---- state -----------------------------------------------------------
    def show_card(self, card: Card):
        self.card = card
        self._card_cache = None

    def clear_card(self):
        self.card = None
        self._card_cache = None

    def add_bubble(self, bubble: Bubble):
        self.bubbles = [b for b in self.bubbles if b.age < b.duration][-2:]
        self.bubbles.append(bubble)

    def add_ripple(self, pos):
        self.ripples.append(Ripple(pos))

    def update(self, dt: float):
        if self.card:
            self.card.age += dt
        for b in self.bubbles:
            b.age += dt
        self.bubbles = [b for b in self.bubbles if b.age < b.duration]
        for r in self.ripples:
            r.age += dt
        self.ripples = [r for r in self.ripples if r.age < r.duration]

    # ---- drawing ---------------------------------------------------------
    def draw(self, surface: pygame.Surface, show_story: bool = True, show_bubbles: bool = True):
        if show_story:
            if show_bubbles:
                for b in self.bubbles:
                    self._draw_bubble(surface, b)
            else:
                self.bubbles.clear()
            if self.card and not self.card.done:
                self._draw_card(surface, self.card)
        for r in self.ripples:
            self._draw_ripple(surface, r)
        self._draw_hud(surface)

    def _draw_ripple(self, surface, r: Ripple):
        t = r.age / r.duration
        radius = int(_s(6) + _s(22) * t)
        alpha = int(200 * (1 - t))
        surf = pygame.Surface((radius * 2 + 4, radius * 2 + 4), pygame.SRCALPHA)
        pygame.draw.circle(
            surf, (255, 244, 214, alpha), (radius + 2, radius + 2), radius, max(2, _s(3))
        )
        surface.blit(surf, (r.pos[0] - radius - 2, r.pos[1] - radius - 2))

    def _draw_hud(self, surface):
        if not self.hud_text:
            return
        font = fonts.get("bold", 11)
        dot = {"auto": (110, 200, 110), "manual": (235, 190, 80), "off": (170, 170, 170)}[
            self.hud_mode
        ]
        label = font.render(self.hud_text, True, (245, 238, 220))
        pad = _s(6)
        w = label.get_width() + pad * 3 + _s(8)
        h = label.get_height() + pad
        pill = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(pill, (30, 24, 16, 165), pill.get_rect(), border_radius=h // 2)
        pygame.draw.circle(pill, dot, (pad + _s(4), h // 2), _s(4))
        pill.blit(label, (pad * 2 + _s(8), (h - label.get_height()) // 2))
        x, y = _pos(8, 700 - 8)
        surface.blit(pill, (x, y - h))

    def _draw_bubble(self, surface, b: Bubble):
        pal = _palette()
        name_font = fonts.get("bold", 12)
        body_font = fonts.get("italic", 12)
        max_w = _s(210)
        lines = wrap(b.text, body_font, max_w)[:4]
        name_img = name_font.render(b.name, True, pal["text"])
        body_imgs = [body_font.render(line, True, pal["text"]) for line in lines]
        pad = _s(8)
        w = max([name_img.get_width()] + [i.get_width() for i in body_imgs]) + pad * 2
        h = name_img.get_height() + sum(i.get_height() for i in body_imgs) + pad * 2
        bubble = pygame.Surface((w, h + _s(8)), pygame.SRCALPHA)
        body_rect = pygame.Rect(0, 0, w, h)
        bg = (247, 240, 222, 240) if pal["box"] == BoxStyles.ROUNDED_BOX else (58, 50, 38, 240)
        edge = (120, 96, 64, 255)
        pygame.draw.rect(bubble, bg, body_rect, border_radius=_s(10))
        pygame.draw.rect(bubble, edge, body_rect, max(1, _s(2)), border_radius=_s(10))
        tail_x = w // 2
        pygame.draw.polygon(
            bubble,
            bg,
            [(tail_x - _s(6), h - _s(2)), (tail_x + _s(6), h - _s(2)), (tail_x, h + _s(7))],
        )
        y = pad
        bubble.blit(name_img, (pad, y))
        y += name_img.get_height()
        for img in body_imgs:
            bubble.blit(img, (pad, y))
            y += img.get_height()
        bubble.set_alpha(b.alpha)

        sw, sh = surface.get_size()
        rect = b.rect
        if b.follow is not None:
            try:
                if not b.follow.alive() or not b.follow.visible:
                    return
                rect = b.follow.rect
            except AttributeError:
                pass
        x = rect.centerx - w // 2
        y = rect.top - bubble.get_height() + _s(6)
        x = max(_s(4), min(x, sw - w - _s(4)))
        y = max(_s(4), y)
        surface.blit(bubble, (x, y))

    def _draw_card(self, surface, card: Card):
        if self._card_cache is None:
            self._card_cache = self._render_card(card)
        img, pos = self._card_cache
        img.set_alpha(card.alpha)
        surface.blit(img, pos)

    def _render_card(self, card: Card):
        pal = _palette()
        if card.style == "title":
            return self._render_title(card, pal)

        major = card.style == "major"
        w = 620 if major else 540
        sprite_size = 76 if major else 44
        n_sprites = min(len(card.cats), 3 if major else 2)
        art_size = 120 if card.art is not None else 0
        if art_size:
            n_sprites = 0
        text_x = 24 + (sprite_size + 6) * n_sprites + (8 if n_sprites else 0) + (art_size + 14 if art_size else 0)
        text_w = w - text_x - 24
        body_font = fonts.get("regular", 15 if major else 13)
        header_font = fonts.get("title", 20 if major else 14)

        lines = wrap(card.text, body_font, _s(text_w))
        max_lines = 7 if major else 3
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(".,;: ") + "…"
        line_h = body_font.get_linesize()
        header_h = header_font.get_linesize() if card.header else 0
        foot_font = fonts.get("italic", 12)
        foot_lines = wrap(card.footer, foot_font, _s(text_w))[:3] if card.footer else []
        text_h = header_h + _s(4) + line_h * len(lines)
        if foot_lines:
            text_h += _s(6) + foot_font.get_linesize() * len(foot_lines)
        h_scaled = max(text_h, _s(sprite_size if n_sprites else 0), _s(art_size)) + _s(36)
        h = int(h_scaled / screen_settings.screen_scale) + 1

        surf = _box(w, h)
        if card.header:
            pygame.draw.rect(
                surf, card.accent, pygame.Rect(_s(14), _s(16), _s(5), h_scaled - _s(32)), border_radius=_s(3)
            )

        if art_size:
            art = pygame.transform.smoothscale(card.art, (_s(art_size), _s(art_size)))
            ay = (h_scaled - art.get_height()) // 2
            surf.blit(art, (_s(24), ay))
            pygame.draw.rect(surf, (120, 96, 64), (_s(24), ay, art.get_width(), art.get_height()), max(1, _s(2)))
        x = _s(24)
        for cat in card.cats[:n_sprites]:
            spr = _sprite(cat, sprite_size)
            if spr:
                surf.blit(spr, (x, (h_scaled - spr.get_height()) // 2))
            x += _s(sprite_size + 6)

        y = (h_scaled - text_h) // 2
        if card.header:
            surf.blit(header_font.render(card.header, True, card.accent), (_s(text_x), y))
            y += header_h + _s(4)
        for line in lines:
            surf.blit(body_font.render(line, True, pal["text"]), (_s(text_x), y))
            y += line_h
        if foot_lines:
            y += _s(6)
            for line in foot_lines:
                surf.blit(foot_font.render(line, True, pal["muted"]), (_s(text_x), y))
                y += foot_font.get_linesize()

        x0 = (800 - w) / 2
        y0 = 636 - h  # sits just above the camp's save button
        return surf, _pos(x0, y0)

    def _render_title(self, card: Card, pal):
        title_font = fonts.get("title", 34)
        sub_font = fonts.get("bold", 14)
        title = title_font.render(card.header, True, pal["text"])
        sub_lines = [l for l in card.text.split("\n") if l]
        subs = [sub_font.render(l, True, pal["muted"]) for l in sub_lines]
        w_scaled = max([title.get_width()] + [s.get_width() for s in subs]) + _s(60)
        h_scaled = title.get_height() + sum(s.get_height() for s in subs) + _s(40)
        w = int(w_scaled / screen_settings.screen_scale) + 1
        h = int(h_scaled / screen_settings.screen_scale) + 1
        surf = _box(w, h)
        sw = surf.get_width()
        y = _s(18)
        surf.blit(title, ((sw - title.get_width()) // 2, y))
        y += title.get_height()
        for s in subs:
            surf.blit(s, ((sw - s.get_width()) // 2, y))
            y += s.get_height()
        return surf, _pos((800 - w) / 2, 250 - h / 2)
