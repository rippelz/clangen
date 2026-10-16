# ClanGen: Auto Life

A fork of [ClanGen](https://github.com/ClanGenOfficial/clangen) that plays itself.
Launch it and the clan simply lives: no menus, no clicking. Leave it open and watch.

> This is a personal mod. Upstream ClanGen does not accept AI-assisted contributions
> (see `AGENTS.md`), so please don't open PRs from this fork against the official repo.

## What it does

Every moon, the director plays the game through the real screens, so all of ClanGen's
own logic runs exactly as if a person were clicking:

1. **Camp.** The camp sits on screen while cats' thoughts pop up above their heads.
2. **Leadership.** Sometimes the leader changes the clan focus (hunting when prey is
   short, herbs when the store is low, rest when many cats are hurt, raids or aid
   depending on the leader's personality). Sometimes they visit the leader's den to
   befriend or provoke another clan, or deal with an outsider.
3. **Patrols.** One to three patrols, chosen by need. Hunting goes up when prey is low,
   border patrols when relations sour, training when there are apprentices. Mates and
   mentor pairs like to go out together. Whether a patrol proceeds, turns back or picks
   a fight follows the patrol's own temperament.
4. **Timeskip.** Presses the real "Timeskip One Moon" button and saves the clan.
5. **The story.** The moon's events are told as cards over the camp. Deaths, births,
   ceremonies and new leaders get full cards, while minor events get small ones. The
   moon's headline cat then gets a short profile spotlight.

New clans are founded automatically (on first launch, or when a clan dies out).

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

The status pill in the bottom-left corner shows what the director is doing.

## Settings

Edit `autoplay.toml` to change pacing, idle timeout, new-clan game mode, spotlights
and more. Set `CLANGEN_AUTOPLAY=0` in the environment to launch as plain ClanGen.

## Code

Everything lives in `scripts/autoplay/` and is wired into `main.py` with a handful of hooks:

- `director.py`: the scene state machine and input hand-off
- `brain.py`: the decisions (patrol plans, patrol choices, focus, leader's den)
- `story.py`: diffs the clan across a timeskip and turns events into cards
- `overlay.py`: draws cards, thought bubbles, the HUD and click ripples
- `clan_factory.py`: founds random clans through the normal creation code
