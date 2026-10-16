"""Side panels for widescreen / fullscreen: the clan chronicle and a clan overview.

ClanGen's layout is a fixed 800x700, so on a 16:9 screen there are wide margins
either side of the game. These panels live in that space, so they never cover the
game or take input. Unlike overlay.py, coordinates here are real screen pixels.
"""

from dataclasses import dataclass
from typing import Optional

import i18n
import pygame

import scripts.game_structure.screen_settings as screen_settings
from scripts.autoplay import brain
from scripts.autoplay.overlay import _palette, _sprite, fonts, wrap
from scripts.cat.cats import Cat
from scripts.clan_package.settings import get_clan_setting
from scripts.game_structure import game
from scripts.game_structure.game.settings import game_setting_get

MIN_MARGIN = 230  # screen pixels; narrower margins get no panels
MAX_ENTRIES = 60


@dataclass
class Entry:
    moon: int
    header: str
    text: str
    accent: tuple
    cat: Optional[Cat] = None


class Panels:
    def __init__(self):
        self.entries: list[Entry] = []
        self._left = None  # (key, surface)
        self._right = None
        self._right_age = 0.0

    def add(self, header: str, text: str, accent=(140, 110, 70), cat=None):
        moon = game.clan.age if game.clan else 0
        self.entries.insert(0, Entry(moon, header, text, accent, cat))
        del self.entries[MAX_ENTRIES:]
        self._left = None

    def clear(self):
        self.entries.clear()
        self._left = None

    def update(self, dt: float):
        self._right_age += dt

    # ------------------------------------------------------------------ drawing

    def draw(self, surface: pygame.Surface, status: str):
        margin = screen_settings.offset[0]
        if margin < MIN_MARGIN or game.clan is None:
            return
        sw, sh = surface.get_size()
        gap = max(12, margin // 14)
        width = margin - gap * 2
        top = screen_settings.offset[1] + gap
        height = sh - top * 2

        key = (width, height, game_setting_dark(), len(self.entries), id(self.entries[0]) if self.entries else 0)
        if self._left is None or self._left[0] != key:
            self._left = (key, self._render_chronicle(width, height))
        surface.blit(self._left[1], (gap, top))

        if self._right is None or self._right_age > 1.0 or self._right[0] != (width, height, status):
            self._right_age = 0.0
            self._right = ((width, height, status), self._render_clan(width, height, status))
        surface.blit(self._right[1], (sw - margin + gap, top))

    def _panel(self, width: int, height: int) -> pygame.Surface:
        dark = game_setting_dark()
        surf = pygame.Surface((width, height), pygame.SRCALPHA)
        fill = (32, 27, 20, 205) if dark else (236, 226, 202, 215)
        edge = (120, 96, 64, 255)
        radius = max(8, width // 22)
        pygame.draw.rect(surf, fill, surf.get_rect(), border_radius=radius)
        pygame.draw.rect(surf, edge, surf.get_rect(), max(2, width // 140), border_radius=radius)
        return surf

    def _title(self, surf, text: str, y: int, pad: int) -> int:
        pal = _palette()
        img = fonts.get("title", 20).render(text, True, pal["text"])
        surf.blit(img, ((surf.get_width() - img.get_width()) // 2, y))
        y += img.get_height() + pad // 3
        pygame.draw.line(surf, (150, 124, 86), (pad, y), (surf.get_width() - pad, y), max(1, pad // 10))
        return y + pad // 2

    def _render_chronicle(self, width: int, height: int) -> pygame.Surface:
        pal = _palette()
        surf = self._panel(width, height)
        pad = max(10, width // 16)
        y = self._title(surf, "Chronicle", pad * 2 // 3, pad)
        inner = width - pad * 2

        head_font = fonts.get("bold", 12)
        body_font = fonts.get("regular", 11)
        moon_font = fonts.get("title", 13)
        last_moon = None
        bottom = height - pad
        for entry in self.entries:
            if entry.moon != last_moon:
                last_moon = entry.moon
                img = moon_font.render(f"Moon {entry.moon}", True, pal["muted"])
                if y + img.get_height() > bottom:
                    break
                surf.blit(img, (pad, y))
                y += img.get_height() + 2
            lines = wrap(entry.text, body_font, inner - pad // 2)
            if len(lines) > 8:
                lines = lines[:8]
                lines[-1] = lines[-1].rstrip(".,;: ") + "…"
            need = head_font.get_linesize() + body_font.get_linesize() * len(lines)
            if y + need > bottom:
                break
            bar_x = pad
            pygame.draw.rect(surf, entry.accent, (bar_x, y + 2, max(2, pad // 4), need - 4), border_radius=2)
            x = pad + pad // 2
            surf.blit(head_font.render(entry.header, True, entry.accent), (x, y))
            y += head_font.get_linesize()
            for line in lines:
                surf.blit(body_font.render(line, True, pal["text"]), (x, y))
                y += body_font.get_linesize()
            y += pad // 2

        if not self.entries:
            img = fonts.get("italic", 12).render("The story begins...", True, pal["muted"])
            surf.blit(img, ((width - img.get_width()) // 2, y + pad))
        else:
            # fade the oldest entries out at the bottom
            fade_h = pad * 3
            fade = pygame.Surface((width, fade_h), pygame.SRCALPHA)
            for i in range(fade_h):
                a = int(255 * (i / fade_h))
                pygame.draw.line(fade, (255, 255, 255, 255 - a), (0, i), (width, i))
            sub = surf.subsurface((0, height - fade_h - 2, width, fade_h))
            sub.blit(fade, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        return surf

    def _render_clan(self, width: int, height: int, status: str) -> pygame.Surface:
        pal = _palette()
        clan = game.clan
        surf = self._panel(width, height)
        pad = max(10, width // 16)
        y = self._title(surf, str(clan.name), pad * 2 // 3, pad)
        inner = width - pad * 2

        label_font = fonts.get("bold", 12)
        text_font = fonts.get("regular", 12)
        small = fonts.get("regular", 11)

        def row(label: str, value: str):
            nonlocal y
            lab = label_font.render(label, True, pal["muted"])
            surf.blit(lab, (pad, y))
            for line in wrap(value, text_font, inner - lab.get_width() - pad // 2)[:2]:
                surf.blit(text_font.render(line, True, pal["text"]), (pad + lab.get_width() + pad // 2, y))
                y += text_font.get_linesize()

        living = [c for c in Cat.all_cats_list if c.status.alive_in_player_clan]
        season = i18n.t(f"general.{clan.current_season}")
        row("Moon", f"{clan.age} · {season}")
        kits = sum(1 for c in living if c.status.rank.is_baby())
        apps = sum(1 for c in living if c.status.rank.is_any_apprentice_rank())
        row("Cats", f"{len(living)} ({apps} apprentices, {kits} kits)")
        focus = next((f for f in brain.FOCUS_ALL if get_clan_setting(f)), "business_as_usual")
        row("Focus", i18n.t(f"settings.{focus}"))
        needs = []
        if brain.food_is_low():
            needs.append("prey is scarce")
        if brain.herbs_are_low():
            needs.append("herbs are low")
        hurt = sum(1 for c in living if not c.can_work())
        if hurt:
            needs.append(f"{hurt} unwell")
        row("Clan", ", ".join(needs) if needs else "well fed and healthy")
        y += pad // 2

        # the cats in charge, with their sprites
        sprite = max(28, int(46 * screen_settings.screen_scale / 1.5))
        size = int(sprite / screen_settings.screen_scale)
        for title, cat in (
            ("Leader", clan.leader),
            ("Deputy", clan.deputy),
            ("Medicine cat", clan.medicine_cat),
        ):
            if cat is None or cat.dead or not cat.status.alive_in_player_clan:
                continue
            img = _sprite(cat, size)
            if img:
                surf.blit(img, (pad, y))
            x = pad + sprite + pad // 2
            ty = y + (sprite - label_font.get_linesize() - small.get_linesize()) // 2
            surf.blit(label_font.render(str(cat.name), True, pal["text"]), (x, ty))
            sub = title
            if title == "Leader" and clan.leader_lives:
                sub += f" · {clan.leader_lives} lives"
            surf.blit(small.render(sub, True, pal["muted"]), (x, ty + label_font.get_linesize()))
            y += sprite + pad // 3
        y += pad // 2

        # other clans, coloured by how they feel about us
        hostile = [c.name for c in brain.hostile_clans()]
        friendly = [c.name for c in brain.friendly_clans()]
        if clan.all_other_clans:
            surf.blit(label_font.render("Neighbours", True, pal["muted"]), (pad, y))
            y += label_font.get_linesize()
            for other in clan.all_other_clans:
                if other.name in hostile:
                    mood, col = "hostile", (160, 60, 50)
                elif other.name in friendly:
                    mood, col = "friendly", (80, 130, 70)
                else:
                    mood, col = "neutral", pal["muted"]
                name = text_font.render(str(other.name), True, pal["text"])
                surf.blit(name, (pad, y))
                m = small.render(mood, True, col)
                surf.blit(m, (width - pad - m.get_width(), y + (name.get_height() - m.get_height()) // 2))
                y += text_font.get_linesize()

        # what autoplay is doing right now, pinned to the bottom
        if status:
            lines = wrap(status, fonts.get("italic", 12), inner)[:3]
            line_h = fonts.get("italic", 12).get_linesize()
            by = height - pad - line_h * len(lines)
            if by > y:
                pygame.draw.line(surf, (150, 124, 86), (pad, by - pad // 2), (width - pad, by - pad // 2), 1)
                for line in lines:
                    img = fonts.get("italic", 12).render(line, True, pal["muted"])
                    surf.blit(img, ((width - img.get_width()) // 2, by))
                    by += line_h
        return surf


def game_setting_dark() -> bool:
    return bool(game_setting_get("dark mode"))
