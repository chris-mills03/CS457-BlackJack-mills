#!/usr/bin/env python3
"""CS457 Blackjack client (Sprint 3).

Connects to the Blackjack server over TCP, speaks the newline-delimited JSON
protocol in ``protocol_blueprint.md`` and renders the table in the terminal.

Usage:
    python3 client.py -h
    python3 client.py -i server.mills.edu -p 5457
    python3 client.py -i localhost -p 5457 -n Alice

Commands while playing:
    <number> | bet <number>   place a bet (BETTING phase)
    h | hit                   take a card (your turn)
    s | stand                 end your turn
    help                      show the commands
    q | quit                  leave the game (forfeits a game in progress)

Author: Christopher Aidan Mills -- CS 457 Computer Networks
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

MAX_FRAME = 65536  # bytes, including the terminating b"\n"
RECV_SIZE = 4096
CONNECT_TIMEOUT = 10  # seconds
ALIAS_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
WIDTH = 64

HELP_TEXT = """Commands:
  <number> or bet <number>  place your bet during BETTING
  h / hit                   take another card on your turn
  s / stand                 end your turn
  help                      show this help
  q / quit                  leave the game (forfeits a game in progress)"""


# --------------------------------------------------------------------------- #
# Framing: newline-delimited JSON (protocol_blueprint.md section 2)
# --------------------------------------------------------------------------- #
class FrameTooLarge(Exception):
    """Raised when the server sends more than MAX_FRAME bytes without \\n."""


def encode_frame(msg):
    """Compact JSON + exactly one b"\\n" (no raw 0x0A can appear inside)."""
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def extract_frames(buffer):
    """Remove every complete frame from ``buffer`` (bytearray) in place.

    Partial bytes after the last newline stay buffered, so messages that TCP
    splits (fragmentation) or merges (coalescing) are both handled.
    Returns parsed dicts; ``None`` marks a frame that is not valid JSON.
    """
    messages = []
    while True:
        idx = buffer.find(b"\n")
        if idx == -1:
            break
        if idx + 1 > MAX_FRAME:
            raise FrameTooLarge()
        line = bytes(buffer[:idx])
        del buffer[: idx + 1]
        if not line.strip():
            continue
        try:
            messages.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            messages.append(None)
    if len(buffer) >= MAX_FRAME:
        raise FrameTooLarge()
    return messages


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# ASCII card art
# --------------------------------------------------------------------------- #
def _supports(text):
    """True if stdout can print ``text`` (e.g. Unicode suits and borders)."""
    try:
        text.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


UNICODE_CARDS = _supports(
    "\u250c\u2500\u2510\u2502\u2514\u2518\u2591" "\u2660\u2665\u2666\u2663"
)
if UNICODE_CARDS:
    SUITS = {"S": "\u2660", "H": "\u2665", "D": "\u2666", "C": "\u2663"}
    TL, TR, BL, BR, HZ, VT, BACK = (
        "\u250c",
        "\u2510",
        "\u2514",
        "\u2518",
        "\u2500",
        "\u2502",
        "\u2591",
    )
else:  # plain-ASCII fallback
    SUITS = {"S": "S", "H": "H", "D": "D", "C": "C"}
    TL, TR, BL, BR, HZ, VT, BACK = "+", "+", "+", "+", "-", "|", "#"

# Red hearts/diamonds on real terminals; NO_COLOR (no-color.org) disables.
USE_COLOR = (
    sys.stdout.isatty()
    and "NO_COLOR" not in os.environ
    and (os.name != "nt" or "WT_SESSION" in os.environ)
)
RED, RESET = ("\033[31m", "\033[0m") if USE_COLOR else ("", "")
CARD_ROWS = 5


def card_art(card):
    """Return the 5 text rows of one 7-column card, e.g. "10H" or "??"."""
    top = TL + HZ * 5 + TR
    bottom = BL + HZ * 5 + BR
    if card == "??":  # dealer's face-down hole card
        return [top] + [VT + BACK * 5 + VT] * 3 + [bottom]
    rank, suit = card[:-1], card[-1]
    paint = RED if suit in "HD" else ""
    end = RESET if paint else ""
    pip = SUITS.get(suit, suit)
    return [
        top,
        f"{VT}{paint}{rank:<2}{end}   {VT}",
        f"{VT}  {paint}{pip}{end}  {VT}",
        f"{VT}   {paint}{rank:>2}{end}{VT}",
        bottom,
    ]


def hand_art(cards, indent="   "):
    """Lay a hand's cards side by side; returns a list of text rows."""
    if not cards:
        return [f"{indent}(no cards yet)"]
    arts = [card_art(c) for c in cards]
    return [
        indent + " ".join(a[row] for a in arts) for row in range(CARD_ROWS)
    ]


class Display:
    """Serializes terminal output from the network and keyboard threads."""

    def __init__(self):
        self._lock = threading.Lock()

    def show(self, text=""):
        with self._lock:
            print(text, flush=True)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class BlackjackClient:
    """Network reader on the main thread; keyboard on a daemon thread."""

    def __init__(self, sock, alias):
        self.sock = sock
        self.alias = alias
        self.out = Display()
        self.send_lock = threading.Lock()
        self.inbuf = bytearray()
        self.seat = None
        self.rules = {}
        self.state = None  # latest STATE_UPDATE payload
        self.quitting = False
        self.done = False
        self.join_failed = False

    # ----- sending -------------------------------------------------------- #
    def send(self, msg_type, payload=None):
        msg = {
            "msg_type": msg_type,
            "player_id": self.alias,
            "timestamp": int(time.time()),
            "payload": payload or {},
        }
        try:
            with self.send_lock:
                self.sock.sendall(encode_frame(msg))
            return True
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            OSError,
        ):
            if not self.quitting and not self.done:
                self.out.show("[ERROR] Connection to server lost.")
            return False

    def quit(self, reason="user quit"):
        """Send DISCONNECT, then close the socket (protocol section 5.5)."""
        if self.quitting or self.done:
            return
        self.quitting = True
        self.send("DISCONNECT", {"reason": reason})
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    # ----- keyboard thread ------------------------------------------------ #
    def input_loop(self):
        """Read stdin commands and turn them into protocol messages."""
        while not self.done and not self.quitting:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:  # stdin closed (EOF)
                return
            self.handle_command(line.strip())

    def handle_command(self, text):
        words = text.lower().split()
        if not words:
            return
        cmd = words[0]
        if cmd in ("q", "quit", "exit"):
            self.out.show("Leaving the table...")
            self.quit()
        elif cmd in ("h", "hit"):
            self.send("MOVE", {"action": "HIT"})
        elif cmd in ("s", "stand"):
            self.send("MOVE", {"action": "STAND"})
        elif cmd == "help":
            self.out.show(HELP_TEXT)
        elif cmd == "bet" and len(words) == 2 and words[1].isdigit():
            self.send("BET", {"amount": int(words[1])})
        elif cmd.isdigit() and len(words) == 1:
            self.send("BET", {"amount": int(cmd)})
        else:
            self.out.show(f"Unknown command '{text}'. Type 'help'.")

    # ----- network (main) thread ------------------------------------------ #
    def run(self):
        """Receive loop. Returns the process exit code."""
        self.send("CONNECT")
        threading.Thread(target=self.input_loop, daemon=True).start()
        try:
            while not self.done:
                try:
                    data = self.sock.recv(RECV_SIZE)
                except (
                    ConnectionResetError,
                    ConnectionAbortedError,
                    TimeoutError,
                    OSError,
                ):
                    data = b""
                if not data:  # EOF or reset: stop reading
                    break
                self.inbuf.extend(data)
                try:
                    frames = extract_frames(self.inbuf)
                except FrameTooLarge:
                    self.out.show("[ERROR] Server sent an oversized message.")
                    break
                for msg in frames:
                    self.handle_message(msg)
                    if self.done:
                        break
        except KeyboardInterrupt:
            self.out.show("\nCtrl+C pressed, leaving the table...")
            self.quit("ctrl+c")
            return 0
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.join_failed:
            return 1
        if self.quitting:
            self.out.show("You left the game. Goodbye!")
            return 0
        if self.done:
            return 0
        self.out.show("[ERROR] Connection to server lost.")
        return 1

    def handle_message(self, msg):
        if not isinstance(msg, dict) or not isinstance(
            msg.get("payload"), dict
        ):
            self.out.show("[WARN] Ignoring a malformed message from server")
            return
        mtype, payload = msg.get("msg_type"), msg["payload"]
        if mtype == "LOBBY_WAIT":
            self.out.show(
                f"Connected as {self.alias}. "
                f"{payload.get('message', 'Waiting for Player 2')} "
                f"({payload.get('players_connected')}/"
                f"{payload.get('players_required')} players)..."
            )
        elif mtype == "GAME_START":
            self.on_game_start(payload)
        elif mtype == "STATE_UPDATE":
            self.state = payload
            self.render_state(payload)
        elif mtype == "ERROR":
            self.on_error(payload)
        elif mtype == "GAME_OVER":
            self.render_game_over(payload)
            self.done = True
        else:
            self.out.show(f"[WARN] Unexpected message type {mtype!r}")

    # ----- message handlers / rendering ----------------------------------- #
    def on_game_start(self, payload):
        self.seat = payload.get("seat")
        self.rules = payload
        players = payload.get("players", {})
        self.out.show("=" * WIDTH)
        self.out.show("GAME START".center(WIDTH))
        self.out.show(f"You are {self.seat} ({self.alias})".center(WIDTH))
        self.out.show(
            f"PLAYER_1: {players.get('PLAYER_1')}   "
            f"PLAYER_2: {players.get('PLAYER_2')}".center(WIDTH)
        )
        self.out.show(
            f"Balance {payload.get('starting_balance')}  |  "
            f"{payload.get('max_rounds')} rounds  |  bets "
            f"{payload.get('min_bet')}-{payload.get('max_bet')}".center(WIDTH)
        )
        self.out.show("=" * WIDTH)
        self.out.show("Type 'help' for commands.")

    def on_error(self, payload):
        code = payload.get("code")
        self.out.show(f"[SERVER ERROR] {code}: {payload.get('message')}")
        if code in ("NAME_TAKEN", "LOBBY_FULL") or (
            code == "MALFORMED_MESSAGE"
            and payload.get("ref_msg_type") == "CONNECT"
        ):
            self.out.show(
                "Could not join the table. Try again later or "
                "pick another name with -n."
            )
            self.join_failed = True
            self.done = True

    def render_state(self, st):
        players = st.get("players", {})
        dealer = st.get("dealer", {})
        max_rounds = self.rules.get("max_rounds", "?")
        lines = [
            "",
            "=" * WIDTH,
            f" ROUND {st.get('round')}/{max_rounds}   "
            f"PHASE: {st.get('phase')}",
            "-" * WIDTH,
        ]
        dval = dealer.get("value")
        lines.append(
            f"  {'DEALER':<22} {'':<20}"
            f" value {'-' if not dealer.get('cards') else dval or '?'}"
        )
        lines.extend(hand_art(dealer.get("cards")))
        lines.append("-" * WIDTH)
        for seat in ("PLAYER_1", "PLAYER_2"):
            p = players.get(seat)
            if not p:
                continue
            marker = ">" if st.get("active_player") == seat else " "
            you = " (you)" if seat == self.seat else ""
            name = f"{p.get('alias')}{you}"
            lines.append(
                f"{marker} {name:<22} {seat:<20}"
                f" value {p.get('value') if p.get('cards') else '-'}"
            )
            lines.extend(hand_art(p.get("cards")))
            extra = "" if p.get("connected", True) else "  ** DISCONNECTED **"
            result = f"  => {p['result']}" if p.get("result") else ""
            lines.append(
                f"   bet {p.get('bet'):<4} balance "
                f"{p.get('balance'):<5} {p.get('status')}"
                f"{result}{extra}"
            )
        lines.append("-" * WIDTH)
        lines.append(f" Last event: {st.get('last_event', '')}")
        if st.get("phase") == "ROUND_END":
            lines.append(
                " Scoreboard: "
                + "  |  ".join(
                    f"{p.get('alias')} {p.get('balance')}"
                    for p in sorted(
                        players.values(), key=lambda p: -p.get("balance", 0)
                    )
                )
            )
        lines.append("=" * WIDTH)
        lines.append(self.turn_status(st))  # last line: what to do now
        self.out.show("\n".join(lines))

    def turn_status(self, st):
        """One clear line telling the user what to do (or who to wait for)."""
        players = st.get("players", {})
        me = players.get(self.seat, {})
        others = [p for s, p in players.items() if s != self.seat]
        other = others[0] if others else {}
        other_name = other.get("alias", "the other player")
        phase = st.get("phase")
        if phase == "BETTING":
            if me.get("bet", 0) == 0:
                hi = min(self.rules.get("max_bet", 500), me.get("balance", 0))
                return (
                    f" >>> YOUR TURN: place your bet "
                    f"({self.rules.get('min_bet', 10)}-{hi}), e.g. 50"
                )
            return f" Waiting for {other_name} to bet..."
        if phase == "PLAYER_TURN":
            if st.get("active_player") == self.seat:
                return " >>> YOUR TURN: type 'hit' (h) or 'stand' (s)"
            active = players.get(st.get("active_player"), {})
            return (
                f" Waiting for {active.get('alias', 'Player 2')} "
                f"({st.get('active_player')})..."
            )
        if phase == "DEALER_TURN":
            return " Dealer is playing..."
        return " Round complete."

    def render_game_over(self, payload):
        lines = [
            "",
            "#" * WIDTH,
            "GAME OVER".center(WIDTH),
            f"Reason: {payload.get('reason')}   Rounds played: "
            f"{payload.get('rounds_played')}".center(WIDTH),
            "-" * WIDTH,
        ]
        results = payload.get("results", {})
        for seat, r in sorted(
            results.items(), key=lambda kv: -kv[1].get("final_balance", 0)
        ):
            you = " (you)" if seat == self.seat else ""
            lines.append(
                f"  {seat}  {r.get('alias', '') + you:<20} final "
                f"{r.get('final_balance'):<5} net {r.get('net'):+d}"
                f"   {r.get('outcome')}"
            )
        mine = results.get(self.seat, {}).get("outcome")
        if mine:
            lines.append("-" * WIDTH)
            lines.append(f"Your result: {mine}".center(WIDTH))
        lines.append("#" * WIDTH)
        self.out.show("\n".join(lines))


# --------------------------------------------------------------------------- #
# CLI, DNS resolution, connection
# --------------------------------------------------------------------------- #
def port_number(text):
    """argparse type: an integer TCP port in 1-65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="CS457 two-player Blackjack client.",
        epilog="Example: python3 client.py -i server.mills.edu -p 5457",
    )
    parser.add_argument(
        "-i",
        dest="hostname",
        required=True,
        help="server DNS hostname (e.g. server.mills.edu)",
    )
    parser.add_argument(
        "-p", "--port", type=port_number, required=True, help="server TCP port"
    )
    parser.add_argument(
        "-n",
        "--name",
        dest="alias",
        help="player alias, 1-16 letters/digits/_ "
        "(prompted for if omitted)",
    )
    return parser.parse_args(argv)


def resolve_server(hostname):
    """Resolve the server hostname via DNS; exit(1) cleanly on failure."""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print(
            "Please check your Cisco router DNS configuration "
            "('ip dns server' / 'ip host') or verify domain spelling."
        )
        sys.exit(1)
    except (UnicodeError, OSError) as exc:
        # e.g. an over-long label, or the resolver itself is unreachable.
        print(f"[ERROR] DNS lookup for '{hostname}' failed: {exc}.")
        print(
            "Please check your Cisco router DNS configuration "
            "('ip dns server' / 'ip host') or verify domain spelling."
        )
        sys.exit(1)


def ask_alias(initial):
    """Return a valid alias, prompting until the user gives one."""
    alias = initial
    while alias is None or not ALIAS_RE.match(alias) or alias == "SERVER":
        if alias is not None:
            print("Alias must be 1-16 letters, digits or underscores.")
        try:
            alias = input("Enter your player name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
    return alias


def connect(ip, port, hostname):
    """Open the TCP connection; exit(1) with a clear message on failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect((ip, port))
    except (
        ConnectionRefusedError,
        TimeoutError,
        socket.timeout,
        OSError,
    ) as exc:
        sock.close()
        print(
            f"[ERROR] Could not connect to {hostname} ({ip}) port {port}: "
            f"{exc}"
        )
        print("Is the server running and reachable on that port?")
        sys.exit(1)
    sock.settimeout(None)
    return sock


def main(argv=None):
    args = parse_args(argv)
    server_ip = resolve_server(args.hostname)
    alias = ask_alias(args.alias)
    print(
        f"Resolved {args.hostname} -> {server_ip}. Connecting to port "
        f"{args.port}..."
    )
    sock = connect(server_ip, args.port, args.hostname)
    return BlackjackClient(sock, alias).run()


if __name__ == "__main__":
    sys.exit(main())
