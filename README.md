# STS Advisor

A real-time **strategic advisor for Slay the Spire**. You play every fight yourself;
on each decision screen — card rewards, paths, shops, campfires, events, Neow, boss
relics — a Claude model reads the live game state and pops a recommendation in a small
overlay. It keeps a running "thesis" so its advice stays coherent across the whole climb.

It rides on **CommunicationMod**: the mod streams game state to a small Python bridge,
the bridge asks Claude (via the Claude Code CLI), and advice appears in a window beside
the game. You make every actual click.

---

## ⚠️ What this costs you

This runs on **your own Claude subscription** (Max or Pro) through the Claude Code CLI —
**no API key, no per-use billing**. But you do need an active Claude plan; this repo is
just the brains that drive it, not the AI itself. If you don't have Claude Code set up
and logged in, it won't run.

---

## Prerequisites

| Need | Where |
| --- | --- |
| Slay the Spire (Steam) | the game itself |
| **ModTheSpire**, **BaseMod**, **CommunicationMod** | Steam Workshop (links below) |
| **Claude Code** CLI, logged in (Max/Pro) | https://docs.claude.com/claude-code |
| **Python 3** | https://www.python.org/downloads/ (tick "Add to PATH") |

Workshop items (subscribe to all three):
- ModTheSpire — https://steamcommunity.com/sharedfiles/filedetails/?id=1605060445
- BaseMod — https://steamcommunity.com/sharedfiles/filedetails/?id=1605833019
- CommunicationMod — https://steamcommunity.com/sharedfiles/filedetails/?id=2131373661

---

## Install

1. **Subscribe** to the three Workshop mods above, then launch the game once through
   ModTheSpire so they download and install.
2. **Set up Claude Code**: install it, run `claude`, and `/login` with your Anthropic
   account (Max or Pro).
3. **Get this project**: download the latest release `.zip` and extract it anywhere
   (a path *without spaces* is simplest, e.g. `C:\Tools\sts-advisor`).
4. **Run the installer** — it detects its own location and points CommunicationMod at it:
   - Right-click `install.ps1` → **Run with PowerShell**, or from a terminal:
     ```
     powershell -ExecutionPolicy Bypass -File install.ps1
     ```
   It checks Python + Claude Code and writes the CommunicationMod config for you.

That's it — no API keys, no manual path editing.

---

## Run

1. Launch Slay the Spire **via ModTheSpire** with BaseMod + CommunicationMod enabled.
   The advice overlay **opens automatically** with the game and **closes when you quit**.
   Run the game in **borderless/windowed** mode so you can see the overlay beside it.
2. Start a run. On each decision screen, advice shows up in the overlay and in
   `state\latest_advice.txt`. Combat is all yours — no calls happen during fights.

(To open the overlay by hand instead, set `"launch_viewer": false` in `config.json`
and run `py sts_viewer.py` yourself.)

First decision after launch is the slowest (the model warms up); after that it's a
couple seconds per screen.

---

## How it works (short version)

- A persistent (warm) `claude` process answers each decision over a streaming session,
  so the agent startup cost is paid once, not per call.
- Extended "thinking" is disabled (`max_thinking_tokens: 0`) — that's the big speed win.
- The model re-emits its full plan ("thesis") only when something changes; routine turns
  return just the pick.
- The bridge only ever sends non-altering `WAIT` commands to the game, so it observes
  while **you** play — it never makes a move for you.

---

## Config knobs (`config.json`)

| Key | Default | What |
| --- | --- | --- |
| `model` | `claude-haiku-4-5` | Fast + sharp. `claude-sonnet-4-6` for heavier reasoning. |
| `max_thinking_tokens` | `0` | Raise (e.g. `4000`) for deeper deliberation on hard calls — slower. |
| `fast_mode` | `true` | Terse output. `false` for fuller advice (CONFIDENCE / LOOK AHEAD). |
| `model_by_screen` | `{}` | Per-screen model overrides, e.g. `{"BOSS_REWARD": "claude-sonnet-4-6"}`. |
| `prefight_heads_up` | `false` | `true` adds a brief threat read at the start of each fight. |
| `debounce_seconds` | `1.5` | How long a screen must hold still before it's advised. |
| `card_data_dir` | `data\sts1` | Folder with `cards.json`/`relics.json`. Their exact text (base + upgraded) is injected per screen so the model reads cards instead of guessing. Vanilla StS1 only; missing files just disable the feature. |

Paths in `config.json` are relative to the project folder, so you can move/rename it
freely (just re-run `install.ps1` afterward so CommunicationMod gets the new path).

---

## Troubleshooting

- **No advice appears.** Check `state\debug.log`. A good launch shows
  `=== STS_ADVISOR bridge starting ===` then `warm session up`. If the file never
  updates, CommunicationMod didn't launch the bridge — confirm BaseMod + CommunicationMod
  are enabled in ModTheSpire, and re-run `install.ps1`.
- **"not logged in" / auth errors.** Run `claude` in a terminal and make sure a normal
  prompt works; the bridge uses that same login.
- **Folder path has spaces.** `install.ps1` handles it via a short path, but moving to a
  space-free folder (e.g. `C:\Tools\sts-advisor`) is the cleanest fix.

---

## Credits

- [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod) by ForgottenArbiter — the game-state bridge this builds on.
- Strategy system prompt: `STS_ADVISOR.md` (included).

You play the Spire; this just backseats well.
