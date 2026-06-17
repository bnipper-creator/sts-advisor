#!/usr/bin/env python3
"""
STS_ADVISOR viewer — a small always-on-top window that tails the latest-advice
file and shows the current recommendation beside the game. Run this SEPARATELY
from the game (just double-click / `python sts_viewer.py`); it is independent of
the CommunicationMod bridge and only reads a file.
"""
import json
import os
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)
_advice = CFG["latest_advice_path"]
ADVICE_PATH = _advice if os.path.isabs(_advice) else os.path.join(HERE, _advice)
HEARTBEAT_PATH = os.path.join(os.path.dirname(ADVICE_PATH), "heartbeat")
# Close grace: the bridge stamps the heartbeat every ~0.5s while alive, so a few
# seconds is plenty of margin and the overlay vanishes quickly when the game quits.
HEARTBEAT_GRACE = float(CFG.get("viewer_close_grace_s", 2.5))
POLL_MS = 300


class Viewer:
    def __init__(self, root):
        self.root = root
        root.title("StS Advisor")
        root.attributes("-topmost", True)
        root.geometry("440x300+40+40")
        root.configure(bg="#12141c")
        self.text = tk.Text(
            root, wrap="word", bg="#12141c", fg="#e6e6e6",
            font=("Consolas", 12), bd=0, padx=12, pady=10,
            insertbackground="#e6e6e6",
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("hdr", foreground="#7fd1b9",
                                font=("Consolas", 11, "bold"))
        self._mtime = None
        self._seen_hb = False  # only auto-close once a live bridge has been seen
        self._show("Waiting for the first decision screen…\n"
                   "(Start a run with CommunicationMod enabled.)")
        self._poll()

    def _show(self, content):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        lines = content.splitlines()
        if lines and lines[0].startswith("=="):
            self.text.insert("end", lines[0] + "\n", "hdr")
            self.text.insert("end", "\n".join(lines[1:]))
        else:
            self.text.insert("end", content)
        self.text.config(state="disabled")

    def _poll(self):
        # Close automatically once the bridge stops updating its heartbeat — i.e.
        # the game quit. Only arms after we've seen a LIVE (fresh) heartbeat, so a
        # stale file from a previous session can't close a freshly-opened overlay,
        # and a manually-opened overlay with no game running stays put.
        try:
            age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
            if age <= HEARTBEAT_GRACE:
                self._seen_hb = True          # bridge is live
            elif self._seen_hb:
                self.root.destroy()           # was live, now stale -> game quit
                return
        except FileNotFoundError:
            pass

        try:
            m = os.path.getmtime(ADVICE_PATH)
            if m != self._mtime:
                self._mtime = m
                with open(ADVICE_PATH, encoding="utf-8") as f:
                    self._show(f.read())
        except FileNotFoundError:
            pass
        self.root.after(POLL_MS, self._poll)


if __name__ == "__main__":
    root = tk.Tk()
    Viewer(root)
    root.mainloop()
