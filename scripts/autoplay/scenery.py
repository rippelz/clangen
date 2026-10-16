"""Wallpaper look for the camp while autoplay is in charge.

The game's buttons, den labels and frame fade away, and the camp image melts into a
soft, blurred, full-screen copy of itself, so the screen reads as a living picture
rather than a game. The moment the player takes over, the interface comes back.
"""

import logging

import pygame

import scripts.game_structure.screen_settings as screen_settings
from scripts.screens.screens_core import screens_core

logger = logging.getLogger(__name__)


class Scenery:
    def __init__(self):
        self._hidden: list = []
        self._backdrop = None  # (key, surface)
        self._camp = None  # (key, surface)

    # ------------------------------------------------------------ interface chrome

    @staticmethod
    def _chrome(camp) -> list:
        els = list(screens_core.menu_buttons.values())
        # the watermark can be built more than once (display changes), so find every copy
        try:
            root = screen_settings.MANAGER.get_root_container()
            els.extend(e for e in root.elements if "#dev_watermark" in getattr(e, "object_ids", []))
        except Exception:
            els.append(screens_core.dev_watermark)
        els.extend(getattr(camp, "camp_labels", {}).values())
        els.extend(
            getattr(camp, name, None)
            for name in ("show_den_labels", "label_toggle", "show_den_labels_text")
        )
        toggle = getattr(camp, "label_toggle", None)
        if toggle is not None:
            els.append(getattr(toggle, "checkmark", None))
        save = getattr(camp, "save_button", None)
        if save is not None:
            els.extend((save.unsaved_state, save.saving_state, save.saved_state))
        return [e for e in els if hasattr(e, "hide") and hasattr(e, "visible")]

    def hide_chrome(self, camp):
        """Run every frame: the game re-shows some of these on its own (save state, moon icon)."""
        for el in self._chrome(camp):
            try:
                if el.alive() and el.visible:
                    el.hide()
                    if el not in self._hidden:
                        self._hidden.append(el)
            except Exception:
                pass

    def restore_chrome(self, camp):
        if not self._hidden:
            return
        save = getattr(camp, "save_button", None)
        save_states = set()
        if save is not None:
            save_states = {save.unsaved_state, save.saving_state, save.saved_state}
        toggle = getattr(camp, "label_toggle", None)
        if toggle is not None and not toggle.checked:
            save_states.add(getattr(toggle, "checkmark", None))  # only show the tick if ticked
        for el in self._hidden:
            try:
                if el.alive() and el not in save_states:
                    el.show()
            except Exception:
                pass
        self._hidden.clear()
        try:
            camp.update_buttons_and_text()  # den labels follow their setting, save button its state
        except Exception:
            logger.debug("Autoplay: couldn't refresh camp buttons", exc_info=True)

    def forget(self):
        """Left camp: the next screen sets up its own buttons."""
        self._hidden.clear()

    # ------------------------------------------------------------ backdrop

    def draw_background(self, surface: pygame.Surface, camp):
        bg = camp.game_bgs.get(camp.active_bg) if hasattr(camp, "game_bgs") else None
        if bg is None:
            return
        size = surface.get_size()
        key = (id(bg), size)
        if self._backdrop is None or self._backdrop[0] != key:
            self._backdrop = (key, self._make_backdrop(bg, size))
        surface.blit(self._backdrop[1], (0, 0))

        ckey = (id(bg), bg.get_size())
        if self._camp is None or self._camp[0] != ckey:
            self._camp = (ckey, self._feathered(bg))
        surface.blit(self._camp[1], screen_settings.offset)

    @staticmethod
    def _make_backdrop(bg: pygame.Surface, size) -> pygame.Surface:
        sw, sh = size
        bw, bh = bg.get_size()
        scale = max(sw / bw, sh / bh) * 1.08
        # blur small, then scale up: soft and cheap
        small = pygame.transform.smoothscale(bg, (max(1, int(bw * scale / 8)), max(1, int(bh * scale / 8))))
        small = pygame.transform.box_blur(small, 3)
        big = pygame.transform.smoothscale(small, (int(bw * scale), int(bh * scale)))
        out = pygame.Surface(size)
        out.blit(big, ((sw - big.get_width()) // 2, (sh - big.get_height()) // 2))
        out.fill((122, 118, 112), special_flags=pygame.BLEND_MULT)
        return out

    @staticmethod
    def _feathered(bg: pygame.Surface) -> pygame.Surface:
        w, h = bg.get_size()
        img = bg.convert_alpha()
        feather = max(24, int(min(w, h) * 0.075))
        mask = pygame.Surface((w, h), pygame.SRCALPHA)
        mask.fill((255, 255, 255, 255))
        for i in range(feather):
            t = i / feather
            a = int(255 * t * t * (3 - 2 * t))  # smoothstep
            pygame.draw.rect(mask, (255, 255, 255, a), (i, i, w - 2 * i, h - 2 * i), 1)
        img.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        return img
