#!/usr/bin/env python3
"""
STS_ADVISOR bridge — connects CommunicationMod (Slay the Spire) to an Anthropic
model running the STS_ADVISOR system prompt.

The human plays every fight manually. This process only OBSERVES the game over
CommunicationMod's stdin/stdout protocol and, on the strategic decision screens
the advisor "owns" (card reward, map, shop, campfire, event, Neow, boss relic,
upgrade/removal grids), calls the model for a recommendation. The model's "run
thesis" is persisted to a file between calls — that is the only memory.

CRITICAL: stdout is the command channel to CommunicationMod. NOTHING is written
to stdout except the literal `ready` handshake and protocol commands (we only
ever send the non-altering WAIT/STATE so we never move the game out from under
the player). All advice and logging go to files / stderr.

Run modes:
  python sts_advisor.py            # normal — launched by CommunicationMod
  python sts_advisor.py --selftest # offline test of detection/parsing/thesis,
                                    # no game and no network required.
"""

import sys
import os
import json
import time
import queue
import threading
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")

# Screen types (CommunicationMod `screen_type`) the advisor acts on.
OWNED_SCREENS = {
    "CARD_REWARD",   # pick/skip a card
    "MAP",           # pathing
    "SHOP_SCREEN",   # the shop buy screen
    "REST",          # campfire
    "EVENT",         # events, including Neow
    "BOSS_REWARD",   # boss relic pick
    "GRID",          # upgrade/removal/transform card selection
}


def log(cfg, msg):
    """Append a debug line to the debug log and mirror to stderr. Never stdout."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        with open(cfg["debug_log_path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def load_config(path=DEFAULT_CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Resolve relative paths against the script directory, so the whole folder can
    # be moved/renamed without editing config.json.
    for key in ("system_prompt_path", "thesis_path", "latest_advice_path",
                "advice_log_path", "debug_log_path"):
        if key in cfg and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(HERE, cfg[key])
    # Make state dir exist.
    for key in ("thesis_path", "latest_advice_path", "advice_log_path", "debug_log_path"):
        os.makedirs(os.path.dirname(cfg[key]), exist_ok=True)
    return cfg


def read_system_prompt(cfg):
    with open(cfg["system_prompt_path"], "r", encoding="utf-8") as f:
        return f.read()


def read_thesis(cfg):
    p = cfg["thesis_path"]
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def reset_thesis(cfg):
    write_atomic(cfg["thesis_path"], "")


# --------------------------------------------------------------------------- #
# Screen detection
# --------------------------------------------------------------------------- #

def describe_moment(msg):
    """
    Inspect a CommunicationMod message and decide whether it is an advisable
    moment. Returns (kind, signature, game_state) or (None, None, gs).

    kind is one of: "screen", "prefight", or None.
    signature is a stable string identifying this exact decision so we fire the
    model exactly once per screen (not on every WAIT poll).
    """
    gs = msg.get("game_state")
    if not msg.get("in_game") or gs is None:
        return None, None, gs

    screen_type = gs.get("screen_type", "NONE")
    floor = gs.get("floor")
    act = gs.get("act")

    if screen_type in OWNED_SCREENS:
        choice_list = gs.get("choice_list", [])
        screen_state = gs.get("screen_state", {})
        sig_payload = json.dumps(
            {"s": screen_type, "f": floor, "a": act,
             "c": choice_list, "ss": screen_state},
            sort_keys=True, default=str,
        )
        return "screen", f"SCREEN|{screen_type}|{floor}|{hash(sig_payload)}", gs

    # Optional pre-fight heads-up: only at the very start of a new combat.
    combat = gs.get("combat_state")
    if combat:
        turn = combat.get("turn", 1)
        monsters = combat.get("monsters", [])
        alive = sorted(m.get("id", "?") for m in monsters if not m.get("is_gone"))
        if turn <= 1 and alive:
            return "prefight", f"PREFIGHT|{floor}|{','.join(alive)}", gs

    return None, None, gs


def heartbeat_command(msg):
    """Pick a NON-ALTERING command to keep the protocol alive without moving the
    game. Prefer WAIT; fall back to STATE (throttled by caller)."""
    cmds = [c.lower() for c in msg.get("available_commands", [])]
    if "wait" in cmds:
        return "wait", None
    if "state" in cmds:
        return "state", None
    # Last resort: WAIT is almost always valid even if not advertised.
    return "wait", None


# --------------------------------------------------------------------------- #
# Model call + response parsing
# --------------------------------------------------------------------------- #

THESIS_START = "<<<THESIS>>>"
THESIS_END = "<<<END THESIS>>>"

# The advisor prompt mandates the first pair. Live testing showed the model
# occasionally drifts to the second; accept it so a stray variant never costs us
# the whole run's continuity.
THESIS_MARKERS = [
    (THESIS_START, THESIS_END),
    ("===THESIS_START===", "===THESIS_END==="),
]

# Appended to the system prompt in CLI mode to hold the model to the exact output
# contract (markers + plain text). Verified to fix marker drift in testing.
OUTPUT_CONTRACT = (
    "OUTPUT CONTRACT — follow EXACTLY, overriding any default formatting habit:\n"
    "- Plain text only. NO markdown headers, NO bold, NO horizontal rules.\n"
    "- Start with: PICK: / CONFIDENCE: / WHY: / (optional) LOOK AHEAD:\n"
    "- Then the thesis, delimited by these LITERAL lines, character-for-character:\n"
    "  <<<THESIS>>>\n  ...\n  <<<END THESIS>>>\n"
    "- Do NOT write ===THESIS_START=== or any other variant. The parser keys on "
    "<<<THESIS>>> and <<<END THESIS>>> exactly.\n"
    "- Be terse."
)


# Used in streaming mode. The session remembers the whole run AND we still feed
# the prior thesis as input each turn, so re-emitting the full thesis every turn
# is wasted output (the dominant latency cost). Emit it only when it changes.
STREAM_CONTRACT = (
    "OUTPUT CONTRACT — follow EXACTLY, overriding any default formatting habit:\n"
    "- Plain text only. No markdown headers, bold, or rules.\n"
    "- ALWAYS begin with: PICK: / CONFIDENCE: / WHY: then optional LOOK AHEAD:. "
    "Keep WHY to 1-2 sentences; LOOK AHEAD to at most 2 short notes.\n"
    "- THESIS HANDLING: this is a PERSISTENT session and you also receive the prior "
    "thesis as input every turn, so do NOT restate it when nothing changed. Emit the "
    "thesis block ONLY when you are actually updating it (a new LOCKED fact, or an "
    "archetype / route / priorities shift), OR when the prior thesis is empty (first "
    "turn). On a routine turn where the plan is unchanged, OMIT the thesis block "
    "entirely and output just the PICK block.\n"
    "- When you DO emit it, delimit with the literal lines <<<THESIS>>> and "
    "<<<END THESIS>>> exactly (never ===THESIS_START=== or any variant), and keep it tight."
)

# Fast mode: output is the latency bottleneck, so demand extreme brevity. The
# session remembers the run, so most turns need only the pick + one reason.
FAST_CONTRACT = (
    "OUTPUT CONTRACT — be MAXIMALLY terse; latency is critical. Plain text only.\n"
    "Output ONLY:\n"
    "  PICK: <exact choice name, or SKIP>\n"
    "  WHY: <one sentence, <=20 words, the single decisive reason>\n"
    "Add a CONFIDENCE: line ONLY when the call is genuinely risky/close. "
    "Omit LOOK AHEAD unless one short note is truly load-bearing.\n"
    "THESIS: omit entirely on routine turns — you remember the run and receive the "
    "thesis as input each turn. Emit it ONLY when a LOCKED fact or your archetype/route "
    "actually changes; then wrap it in literal <<<THESIS>>> / <<<END THESIS>>> lines, "
    "under 60 words. No markdown, no preamble, no restating the board."
)


def _trim_advice(advice):
    """Drop any preamble before the PICK line (in case the CLI prints a stray
    notice). If there's no PICK, keep the text as-is."""
    k = advice.find("PICK:")
    return advice[k:].strip() if k != -1 else advice.strip()


def parse_response(text):
    """Split the model output into (advice, thesis). If no markers are present,
    thesis is None (caller keeps the prior thesis)."""
    for start, end in THESIS_MARKERS:
        i = text.find(start)
        j = text.find(end)
        if i != -1 and j != -1 and j > i:
            advice = _trim_advice(text[:i])
            thesis = text[i + len(start):j].strip()
            return advice, thesis
    return _trim_advice(text), None


def slim_state(game_state, kind):
    """Drop the heaviest fields when the current decision doesn't need them.
    The `map` array and full `combat_state` dominate the payload; trimming them
    cuts prompt-processing time (and token use) without losing decision-relevant
    info. MAP screens keep the map; pre-fight keeps combat_state."""
    gs = dict(game_state)
    if kind != "prefight" and gs.get("screen_type") != "MAP":
        gs.pop("map", None)
    if kind != "prefight":
        gs.pop("combat_state", None)
    return gs


def build_user_message(kind, game_state, thesis):
    label = "PRE-FIGHT (enemy info present)" if kind == "prefight" else "DECISION SCREEN"
    gs_json = json.dumps(slim_state(game_state, kind), indent=2, default=str)
    prior = thesis if thesis else "(empty — first call)"
    return (
        f"{label}\n\n"
        f"GAME STATE (CommunicationMod JSON):\n{gs_json}\n\n"
        f"PRIOR THESIS:\n{prior}\n"
    )


def pick_model(cfg, screen_type):
    """Per-screen model routing: a fast/cheap model for routine screens, a
    stronger one for high-stakes screens (configured in model_by_screen).
    Defaults to cfg['model']."""
    return cfg.get("model_by_screen", {}).get(screen_type, cfg["model"])


def call_model_cli(cfg, system_prompt, user_message, model=None):
    """Shells out to the Claude Code CLI (`claude -p`), which uses the user's
    Max/Pro login. No API key, no per-call charge — it counts against the
    subscription's usage limits. Slower per call (~25-70s) than a raw API call
    because it spins up the agent harness each invocation.

    stdout is parsed as plain text: --output-format json is unreliable in some
    CLI versions, and --tools "" keeps the output free of tool-call noise.
    """
    import subprocess
    exe = cfg.get("claude_path", "claude")
    args = [
        exe, "-p",
        "--system-prompt", system_prompt,
        "--append-system-prompt", OUTPUT_CONTRACT,
        "--model", model or cfg["model"],
        "--tools", "",
        "--strict-mcp-config",  # load zero MCP servers — major per-call startup win
    ]
    env = dict(os.environ)
    # These markers make the CLI refuse to run when nested inside another Claude
    # Code session (e.g. during dev). The game process won't set them; strip them
    # defensively so the bridge runs from any context.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["MAX_THINKING_TOKENS"] = str(cfg.get("max_thinking_tokens", 0))  # see ClaudeSession
    proc = subprocess.run(
        args, input=user_message, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
        timeout=cfg.get("cli_timeout", 180),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exit {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
        )
    return proc.stdout


# --------------------------------------------------------------------------- #
# Persistent streaming sessions — pay the agent-harness startup ONCE per model,
# then answer every decision over the same warm process (huge latency win vs.
# re-spawning `claude` each call). If a process dies, it auto-restarts; the
# thesis file preserves run continuity across the restart.
# --------------------------------------------------------------------------- #

class ClaudeSession:
    """One long-lived `claude` process in stream-json in/out mode for a model."""

    def __init__(self, cfg, system_prompt, model):
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.model = model
        self.proc = None
        self.turns = 0
        self.restart_every = int(cfg.get("restart_session_every", 20))
        self.results = queue.Queue()
        self.lock = threading.Lock()
        self._start()

    def _args(self):
        exe = self.cfg.get("claude_path", "claude")
        contract = FAST_CONTRACT if self.cfg.get("fast_mode", True) else STREAM_CONTRACT
        return [
            exe, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt", self.system_prompt,
            "--append-system-prompt", contract,
            "--model", self.model,
            "--tools", "",
            "--strict-mcp-config",
        ]

    def _start(self):
        import subprocess
        if self.proc is not None:
            try:
                self.proc.kill()  # don't leak the previous process on restart
            except Exception:
                pass
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # THE latency lever: extended thinking was costing 30-60s/turn. Capping it
        # drops turns to ~1-3s with no real quality loss (strategy is in the system
        # prompt). Raise via config for deeper deliberation on hard decisions.
        env["MAX_THINKING_TOKENS"] = str(self.cfg.get("max_thinking_tokens", 0))
        q = queue.Queue()
        self.results = q
        self.turns = 0
        self.proc = subprocess.Popen(
            self._args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=env,
        )
        # Bind the reader to THIS process's queue. If an old process is later
        # killed, its reader drops its EOF sentinel into its own (discarded) queue
        # — never into the new session's queue.
        threading.Thread(target=self._reader, args=(self.proc, q), daemon=True).start()
        log(self.cfg, f"warm session up: model={self.model} pid={self.proc.pid}")

    def _reader(self, proc, q):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "result":
                q.put(evt.get("result", ""))
        q.put(None)  # EOF -> the process exited

    def _write(self, user_message):
        msg = {"type": "user", "message": {"role": "user",
               "content": [{"type": "text", "text": user_message}]}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def ask(self, user_message):
        timeout = self.cfg.get("cli_timeout", 180)
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            elif self.restart_every and self.turns >= self.restart_every:
                log(self.cfg, f"recycling session to bound context "
                              f"(after {self.turns} turns, {self.model})")
                self._start()
            # Drop any orphaned result from a prior aborted/errored turn, so this
            # screen can't be answered with the previous screen's result.
            while True:
                try:
                    self.results.get_nowait()
                except queue.Empty:
                    break
            try:
                self._write(user_message)
            except Exception:
                self._start()
                self._write(user_message)
            res = self.results.get(timeout=timeout)
            if res is None:  # process died mid-turn — restart and retry once
                log(self.cfg, f"warm session died ({self.model}); restart + retry")
                self._start()
                self._write(user_message)
                res = self.results.get(timeout=timeout)
                if res is None:
                    raise RuntimeError(f"stream session unrecoverable ({self.model})")
            self.turns += 1
            return res


class SessionPool:
    """Lazily holds one warm ClaudeSession per model (so per-screen model routing
    still works — each model pays startup once)."""

    def __init__(self, cfg, system_prompt):
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.sessions = {}
        self.lock = threading.Lock()

    def ensure(self, model):
        with self.lock:
            if model not in self.sessions:
                self.sessions[model] = ClaudeSession(self.cfg, self.system_prompt, model)
            return self.sessions[model]

    def ask(self, model, user_message):
        return self.ensure(model).ask(user_message)


# --------------------------------------------------------------------------- #
# Worker — runs model calls off the main loop so the heartbeat never blocks
# --------------------------------------------------------------------------- #

class Advisor:
    def __init__(self, cfg, model_fn=None):
        self.cfg = cfg
        self.system_prompt = read_system_prompt(cfg)
        self._injected = model_fn  # tests inject a stub model fn
        self.pool = None
        if model_fn is None and cfg.get("mode", "stream") == "stream":
            self.pool = SessionPool(cfg, self.system_prompt)
            try:
                self.pool.ensure(cfg["model"])  # pre-warm now (during menu time)
            except Exception:
                log(cfg, "prewarm failed:\n" + traceback.format_exc())
        self.q = queue.Queue()
        self._lock = threading.Lock()
        self._latest_sig = None
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _invoke(self, model, user_message):
        if self._injected:
            return self._injected(self.cfg, self.system_prompt, user_message, model)
        if self.pool:
            return self.pool.ask(model, user_message)
        return call_model_cli(self.cfg, self.system_prompt, user_message, model)

    def submit(self, kind, game_state, signature):
        # Record this as the screen we're now on, and immediately show a
        # "thinking" placeholder so the panel never displays stale advice for a
        # screen the player has already left.
        with self._lock:
            self._latest_sig = signature
        self._write_thinking(kind, game_state)
        self.q.put((kind, game_state, signature))

    def _is_current(self, signature):
        with self._lock:
            return signature == self._latest_sig

    def _write_thinking(self, kind, game_state):
        floor = game_state.get("floor")
        screen = game_state.get("screen_type") or kind
        eta = self.cfg.get("expected_latency_s", 30)
        header = f"== Floor {floor} | {screen} | analyzing… =="
        body = (f"⏳ Thinking about this {screen}…  (~{eta}s)\n"
                "Any recommendation shown before this was for an earlier screen.")
        write_atomic(self.cfg["latest_advice_path"], f"{header}\n{body}\n")

    def _run(self):
        while True:
            kind, game_state, signature = self.q.get()
            try:
                self._handle(kind, game_state, signature)
            except Exception:
                log(self.cfg, "WORKER ERROR:\n" + traceback.format_exc())
            finally:
                self.q.task_done()

    def _handle(self, kind, game_state, signature):
        cfg = self.cfg
        thesis = read_thesis(cfg)  # read fresh so we chain off the latest
        user_message = build_user_message(kind, game_state, thesis)
        floor = game_state.get("floor")
        screen = game_state.get("screen_type")
        model = pick_model(cfg, screen)
        log(cfg, f"-> model({model}) kind={kind} floor={floor} screen={screen}")

        text = self._invoke(model, user_message)
        advice, new_thesis = parse_response(text)

        # Always advance the thesis (continuity is cumulative). But if the player
        # has already moved on, this advice is stale — log it, don't overwrite the
        # panel, which is now showing the current screen's "thinking" placeholder.
        stale = not self._is_current(signature)

        if new_thesis is not None:
            write_atomic(cfg["thesis_path"], new_thesis)
        else:
            log(cfg, "WARN: no THESIS markers in response; prior thesis kept")

        header = (f"== Floor {floor} | {screen or kind} | "
                  f"{datetime.now().strftime('%H:%M:%S')} ==")
        block = f"{header}\n{advice}\n"
        with open(cfg["advice_log_path"], "a", encoding="utf-8") as f:
            f.write(block + "\n")
        if stale:
            log(cfg, f"(superseded, not shown) {screen} floor {floor}")
        else:
            write_atomic(cfg["latest_advice_path"], block)
        first = (advice.splitlines() or ["(empty)"])[0]
        log(cfg, f"<- {first}{'  [STALE]' if stale else ''}")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def launch_viewer(cfg):
    """Spawn the advice overlay as a detached, console-less process so it appears
    automatically when the game (and thus this bridge) starts. The overlay closes
    itself when the heartbeat below goes stale (i.e. the game quits)."""
    import subprocess
    viewer = os.path.join(HERE, "sts_viewer.py")
    if not os.path.exists(viewer):
        return
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable  # no console window
    try:
        subprocess.Popen(
            [exe, viewer], cwd=HERE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        log(cfg, "advice overlay launched")
    except Exception:
        log(cfg, "viewer launch failed:\n" + traceback.format_exc())


def _write_heartbeat(path):
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _heartbeat_loop(path, interval=0.5):
    """Stamp the heartbeat on a fast fixed timer, independent of game activity,
    so the overlay can use a short close-grace and still never false-close while
    the bridge process is alive. Dies with the process when the game quits."""
    while True:
        _write_heartbeat(path)
        time.sleep(interval)


def main(cfg):
    # Make stdout/stdin behave: utf-8, '\n' line endings, no buffering surprises.
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def send(cmd):
        sys.stdout.write(cmd + "\n")
        sys.stdout.flush()

    log(cfg, f"=== STS_ADVISOR bridge starting "
            f"(mode={cfg.get('mode', 'stream')}, model={cfg['model']}) ===")
    advisor = Advisor(cfg)

    hb_path = os.path.join(os.path.dirname(cfg["latest_advice_path"]), "heartbeat")
    # Stamp a FRESH heartbeat before launching the overlay (so it doesn't self-
    # close on a stale file), then keep it fresh on a fast background timer.
    _write_heartbeat(hb_path)
    threading.Thread(target=_heartbeat_loop, args=(hb_path,), daemon=True).start()
    if cfg.get("launch_viewer", True):
        launch_viewer(cfg)

    # Handshake: announce readiness, then the mod streams state.
    send("ready")

    last_fired_sig = None
    pending_sig = None
    pending_since = 0.0
    debounce_s = float(cfg.get("debounce_seconds", 1.5))
    was_in_game = False
    wait_frames = int(cfg.get("heartbeat_wait_frames", 20))

    for raw in sys.stdin:
        raw = raw.lstrip("﻿").strip()  # tolerate a stray UTF-8 BOM
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log(cfg, f"unparseable line: {raw[:120]}")
            continue

        in_game = bool(msg.get("in_game"))

        # New run detection: entering a game at the very start clears the thesis.
        if in_game and not was_in_game:
            gs = msg.get("game_state") or {}
            if (gs.get("floor") or 0) <= 0:
                reset_thesis(cfg)
                last_fired_sig = None
                pending_sig = None
                log(cfg, "new run detected -> thesis reset")
        was_in_game = in_game

        # Debounce: only fire once a screen has held still for debounce_s. This
        # skips transient sub-states the player clicks straight through (e.g. the
        # Neow intro / "Talk" frames) and stops stacking calls that all get
        # superseded before any can be shown.
        kind, signature, _gs = describe_moment(msg)
        now = time.monotonic()
        if not kind:
            pending_sig = None
        elif signature == last_fired_sig:
            pass  # already advised this exact screen
        elif signature != pending_sig:
            pending_sig = signature      # new candidate — (re)start the timer
            pending_since = now
        elif now - pending_since >= debounce_s:
            last_fired_sig = signature
            pending_sig = None
            if not (kind == "prefight" and not cfg.get("prefight_heads_up", False)):
                advisor.submit(kind, msg["game_state"], signature)

        # Always answer with a non-altering heartbeat so the game stays live.
        if msg.get("ready_for_command", True):
            cmd, _ = heartbeat_command(msg)
            if cmd == "wait":
                send(f"wait {wait_frames}")
            else:
                time.sleep(0.25)  # throttle STATE polling
                send("state")

    log(cfg, "stdin closed — bridge exiting")


# --------------------------------------------------------------------------- #
# Offline self-test (no game, no network)
# --------------------------------------------------------------------------- #

def selftest():
    cfg = load_config()
    # Stub model: echoes a canned advisor-shaped response, proving parse/thesis.
    calls = {"n": 0}

    def fake_model(cfg, system_prompt, user_message, model=None):
        calls["n"] += 1
        assert "GAME STATE" in user_message
        return (
            "PICK: Pommel Strike\nCONFIDENCE: high\n"
            "WHY: Cheap front-loaded damage plus a card draw.\n"
            "LOOK AHEAD: Watch AoE for Act 1 gremlins.\n"
            f"{THESIS_START}\n"
            "LOCKED:\n  - none yet\n"
            f"PLAN:\n  archetype: Ironclad, open (call {calls['n']})\n"
            f"{THESIS_END}\n"
        )

    reset_thesis(cfg)
    adv = Advisor(cfg, model_fn=fake_model)

    # 1) An owned card-reward screen fires once.
    card_msg = {
        "in_game": True, "ready_for_command": True,
        "available_commands": ["choose", "skip", "wait", "state"],
        "game_state": {
            "screen_type": "CARD_REWARD", "floor": 1, "act": 1,
            "choice_list": ["pommel strike", "thunderclap", "clash"],
            "screen_state": {"cards": [{"name": "Pommel Strike"}]},
        },
    }
    k, sig, _ = describe_moment(card_msg)
    assert k == "screen", k
    adv.submit(k, card_msg["game_state"], sig)

    # 2) Combat = no-op (unless prefight enabled). 3) Pre-fight detection works.
    combat_msg = {"in_game": True, "ready_for_command": True,
                  "available_commands": ["play", "end", "wait"],
                  "game_state": {"screen_type": "NONE", "floor": 2,
                                 "combat_state": {"turn": 1,
                                                  "monsters": [{"id": "JawWorm", "is_gone": False}]}}}
    k2, sig2, _ = describe_moment(combat_msg)
    assert k2 == "prefight", k2

    # 4) Heartbeat is always non-altering.
    assert heartbeat_command(card_msg)[0] == "wait"
    assert heartbeat_command({"available_commands": ["choose", "state"]})[0] == "state"

    adv.q.join()
    thesis = read_thesis(cfg)
    latest = open(cfg["latest_advice_path"], encoding="utf-8").read()
    assert "archetype: Ironclad" in thesis, thesis
    assert "PICK: Pommel Strike" in latest, latest

    # 5) Dedup: same screen, second time, must NOT change signature.
    k3, sig3, _ = describe_moment(card_msg)
    assert sig3 == sig, "signature should be stable for the same screen"

    print("SELFTEST PASSED")
    print("  - owned-screen detection: OK")
    print("  - pre-fight detection: OK")
    print("  - heartbeat non-altering: OK")
    print("  - thesis round-trip persisted: OK")
    print("  - advice file written: OK")
    print("  - screen dedup signature stable: OK")
    print(f"\nthesis file -> {cfg['thesis_path']}")
    print(f"advice file -> {cfg['latest_advice_path']}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        _cfg = load_config()
        try:
            main(_cfg)
        except Exception:
            log(_cfg, "FATAL:\n" + traceback.format_exc())
            raise
