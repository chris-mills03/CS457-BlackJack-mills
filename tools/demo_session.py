#!/usr/bin/env python3
"""Run a scripted 2-player session and capture every terminal to log files.

Starts ``server.py`` and two ``client.py`` processes (Alice and Bob) exactly as
a user would from three terminals, types commands into the clients' stdin,
and writes the console output to ``demo/``. Used to produce the Sprint 3
"Intermediate Gameplay Execution Log".

Usage (from the repo root):
    python3 tools/demo_session.py [-i localhost] [-p 5457]

Player policy: bet 50 each round, hit below 15, otherwise stand. Alice first
tries an invalid bet (5) and Bob once tries to move out of turn, so the log
also shows the server's ERROR handling.
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "demo")
PY = sys.executable


class Transcript:
    """Merged, time-ordered log of every process plus one file per process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.per_proc = {}

    def add(self, who, text):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.lines.append(f"{stamp} [{who:<6}] {text}")
            self.per_proc.setdefault(who, []).append(text)

    def write(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(
            os.path.join(OUT_DIR, "sprint3_gameplay_log.txt"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("\n".join(self.lines) + "\n")
        for who, lines in self.per_proc.items():
            name = f"{who.lower()}_console.txt"
            with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


class Player:
    """Drives one client process by watching its output for prompts."""

    def __init__(self, name, cmd, log, bad_bet=False, out_of_turn=False):
        self.name, self.log = name, log
        self.proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.bad_bet = bad_bet
        self.out_of_turn = out_of_turn
        self.round = 0
        self.bet_rounds = set()
        self.my_value = 0
        self.finished = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def type(self, text, delay=0.4):
        """Send one line to the client, echoing it as the user's input."""
        time.sleep(delay)  # human-ish pacing
        self.log.add(self.name, f"> {text}")
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            self.log.add(self.name, line)
            self._react(line)
        self.finished.set()

    def _react(self, line):
        m = re.match(r" ROUND (\d+)/", line)
        if m:
            self.round = int(m.group(1))
        if "(you)" in line and " value " in line:
            tail = line.rsplit("value", 1)[1].strip()
            self.my_value = int(tail) if tail.isdigit() else 0
        if ">>> YOUR TURN: place your bet" in line:
            if self.round not in self.bet_rounds:
                self.bet_rounds.add(self.round)
                if self.bad_bet:
                    self.bad_bet = False
                    self.type("5")
                else:
                    self.type("50")
        elif "[SERVER ERROR] INVALID_BET" in line:
            self.type("50")
        elif ">>> YOUR TURN: type 'hit'" in line:
            self.type("hit" if self.my_value < 15 else "stand")
        elif (
            self.out_of_turn
            and line.startswith(" Waiting for")
            and "PLAYER_1" in line
        ):
            self.out_of_turn = False
            self.type("hit", delay=0)  # deliberately out of turn


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-i", default="localhost", help="server hostname")
    parser.add_argument("-p", type=int, default=5457, help="server port")
    args = parser.parse_args()

    log = Transcript()
    srv = subprocess.Popen(
        [PY, "-u", "server.py", "-p", str(args.p)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def pump_server():
        for raw in srv.stdout:
            log.add("SERVER", raw.rstrip("\n"))

    threading.Thread(target=pump_server, daemon=True).start()
    log.add("SERVER", f"$ python3 server.py -p {args.p}")
    time.sleep(1.0)

    players = []
    for name, extra in (
        ("ALICE", {"bad_bet": True}),
        ("BOB", {"out_of_turn": True}),
    ):
        alias = name.capitalize()
        log.add(
            name, f"$ python3 client.py -i {args.i} -p {args.p} " f"-n {alias}"
        )
        players.append(
            Player(
                name,
                [
                    PY,
                    "-u",
                    "client.py",
                    "-i",
                    args.i,
                    "-p",
                    str(args.p),
                    "-n",
                    alias,
                ],
                log,
                **extra,
            )
        )
        time.sleep(1.0)

    ok = all(p.finished.wait(timeout=120) for p in players)
    for p in players:
        p.proc.wait(timeout=5)
        log.add(p.name, f"[process exited with status {p.proc.returncode}]")
    time.sleep(0.5)
    srv.terminate()
    srv.wait(timeout=5)
    log.write()
    print(f"{'Done' if ok else 'TIMED OUT'}: logs written to {OUT_DIR}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
