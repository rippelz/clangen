# ClanGen: Auto Life

A fork of [ClanGen](https://github.com/ClanGenOfficial/clangen) that plays itself.
Launch it and the clan simply lives: no menus, no clicking. Leave it open and watch.

> This is a personal mod. Upstream ClanGen does not accept AI-assisted contributions
> (see `AGENTS.md`), so please don't open PRs from this fork against the official repo.

## What it does

The camp stays on screen and the clan lives in it. Cats wander between their dens,
kits play together, apprentices tag along after their mentors, mates share tongues,
the medicine cat checks on the sick, and every so often a cat's thought floats above
its head. You can click any cat, even mid-stride, to open its profile.

Everything you'd normally do through menus happens as a scene in camp. Under the
hood it calls the same game code the menus use, so the simulation is ClanGen's own.

1. **Dawn.** The moon's events are told as cards. The cats each story is about walk
   into the clearing and are ringed in gold while it's told, and the leader steps up
   for ceremonies.
2. **The leader's word.** Sometimes the leader climbs to their den and announces a
   decision: a new clan focus (hunting when prey is short, herbs when the store is
   low, rest when many cats are hurt, raids or aid depending on their personality),
   a stance toward another clan, or what to do about an outsider.
3. **Patrols.** One to three a moon, chosen by need. The patrol gathers near the
   clearing, someone calls them out, and they walk out of camp. Their story is told
   with the patrol art while they're away, and whether they press on, turn back or
   pick a fight follows their temperament. Then whoever made it walks back in, along
   with anyone they brought home.
4. **Dusk.** The cats head to their dens, night falls over camp, and the moon passes.
   New kits appear in the nursery, and the clan is saved.

New clans are founded automatically (on first launch, or when a clan dies out).

## Screen

ClanGen's layout is a fixed 800x700, so autoplay opens fullscreen and scales the game
to the monitor's full height. On a 16:9 screen, the spare width either side holds two
panels. The **Chronicle** on the left logs every story beat and patrol outcome, grouped
by moon. On the right is an overview of the clan: season, head count, focus, needs, the
leader, deputy and medicine cat, and how the neighbouring clans feel about you.

Set `[display] mode` in `autoplay.toml` to `"window"` for a widescreen window
(`window_size`, 1600x900 by default) or `"classic"` for ClanGen's own 800x700 window.
F11 switches between fullscreen and the window.

## Interacting

Just use the game. Any click, scroll or key press hands control to you right away, so
you can open profiles, family trees and relationships, or patrol by hand. After you've
been idle for a while (45s by default), autoplay picks the story back up from where
it left off. It never yanks you out of clan creation or settings.

| Key | Action |
| --- | --- |
| F6 | Toggle autoplay on/off |
| F7 | Cycle speed: 0.5x, 1x, 2x, 4x |
| F8 | Skip the current card or pause |
| F11 | Fullscreen / window |

While you have control, a pill in the bottom-left corner counts down to autoplay resuming.

## Settings

Edit `autoplay.toml` to change pacing, idle timeout, new-clan game mode, display
and more. Set `CLANGEN_AUTOPLAY=0` in the environment to launch as plain ClanGen.

## Code

Everything lives in `scripts/autoplay/` and is wired into `main.py` with a handful of hooks:

- `director.py`: the scene state machine and input hand-off
- `brain.py`: what to decide (patrol plans, patrol choices, focus, leader's den stance)
- `camp_life.py`: the cats moving around camp (wandering, social visits, gathering, patrols leaving)
- `leader_actions.py`: focus and leader's-den decisions, applied without opening the dens
- `story.py`: diffs the clan across a timeskip and turns events into cards
- `overlay.py`: draws cards, thought bubbles, the HUD and click ripples
- `panels.py`: the widescreen side panels (chronicle and clan overview)
- `clan_factory.py`: founds random clans through the normal creation code
