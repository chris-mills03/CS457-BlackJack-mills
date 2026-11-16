#!/usr/bin/env python3
"""CS457 Blackjack game server (Sprint 3).

Implements the protocol in ``protocol_blueprint.md``, the game state machine in
``fsm_specification.md`` and the single-threaded ``selectors``
concurrency model in ``concurrency_design.md``.

Usage:
    python3 server.py -h
    python3 server.py -p 5457

Author: Christopher Aidan Mills -- CS 457 Computer Networks
"""

import argparse
import json
import logging
import random
import re
import selectors
import socket
import sys
import time
from enum import Enum

# --------------------------------------------------------------------------- #
# Configuration (fsm_specification.md, INIT state)
# --------------------------------------------------------------------------- #
LISTEN_HOST = "0.0.0.0"
MAX_FRAME = 65536  # bytes, including the terminating b"\n"
RECV_SIZE = 4096
STARTING_BALANCE = 500
MAX_ROUNDS = 5
MIN_BET = 10
MAX_BET = 500
REJOIN_TIMEOUT = 60  # seconds a dropped player's seat is held
SELECT_TIMEOUT = 1.0  # seconds; lets the loop check rejoin timers

SEATS = ("PLAYER_1", "PLAYER_2")
ALIAS_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
CLIENT_MSG_TYPES = {"CONNECT", "BET", "MOVE", "DISCONNECT"}
ENVELOPE_TYPES = {
    "msg_type": str,
    "player_id": str,
    "timestamp": int,
    "payload": dict,
}

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["S", "H", "D", "C"]

log = logging.getLogger("blackjack.server")


# --------------------------------------------------------------------------- #
# Framing: newline-delimited JSON (protocol_blueprint.md section 2)
# --------------------------------------------------------------------------- #
class FrameTooLarge(Exception):
    """Raised when a peer sends more than MAX_FRAME bytes without a newline."""


def encode_frame(msg):
    """Serialize one message as compact JSON + a single b"\\n" terminator.

    Compact json.dumps never emits a raw 0x0A byte (newlines inside strings
    are escaped), so b"\\n" on the wire always marks the end of a frame.
    """
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def extract_frames(buffer):
    """Remove every complete frame from ``buffer`` (a bytearray) in place.

    Handles TCP coalescing (several frames in one recv) and fragmentation
    (one frame split across recvs): partial bytes after the last newline stay
    in the buffer for the next call.

    Returns a list of parsed dicts; ``None`` marks a frame that is not valid
    UTF-8 JSON (the caller replies MALFORMED_MESSAGE).
    Raises FrameTooLarge if a frame is, or is growing, past MAX_FRAME.
    """
    messages = []
    while True:
        idx = buffer.find(b"\n")
        if idx == -1:
            break  # partial frame: wait for more
        if idx + 1 > MAX_FRAME:
            raise FrameTooLarge()
        line = bytes(buffer[:idx])
        del buffer[: idx + 1]  # consume the frame and its \n
        if not line.strip():
            continue  # ignore empty lines
        try:
            messages.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            messages.append(None)
    if len(buffer) >= MAX_FRAME:  # no room left for the \n
        raise FrameTooLarge()
    return messages


def make_msg(msg_type, payload):
    """Build a server -> client message with the common envelope (sec 3)."""
    return {
        "msg_type": msg_type,
        "player_id": "SERVER",
        "timestamp": int(time.time()),
        "payload": payload,
    }


def make_error(code, message, ref_msg_type=None):
    """Build an ERROR message (section 4.7)."""
    return make_msg(
        "ERROR",
        {"code": code, "message": message, "ref_msg_type": ref_msg_type},
    )


def validate_envelope(msg):
    """Check the four envelope keys and their types.

    Returns ``(code, text)`` describing the problem, or ``None`` if valid.
    """
    if not isinstance(msg, dict):
        return "MALFORMED_MESSAGE", "Message must be a JSON object"
    for key, expected in ENVELOPE_TYPES.items():
        if key not in msg:
            return "MALFORMED_MESSAGE", f"Missing required key '{key}'"
        value = msg[key]
        # bool is a subclass of int in Python; reject it for timestamp.
        if not isinstance(value, expected) or isinstance(value, bool):
            return "MALFORMED_MESSAGE", f"Key '{key}' has the wrong type"
    if msg["msg_type"] not in CLIENT_MSG_TYPES:
        return "UNKNOWN_MSG_TYPE", f"Unknown msg_type '{msg['msg_type']}'"
    return None


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def new_deck(rng):
    """Return a freshly shuffled 52-card deck of '<rank><suit>' strings."""
    deck = [rank + suit for suit in SUITS for rank in RANKS]
    rng.shuffle(deck)
    return deck


def hand_value(cards):
    """Best blackjack value: Aces count 11 unless that would bust."""
    total, aces = 0, 0
    for card in cards:
        rank = card[:-1]
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


# --------------------------------------------------------------------------- #
# Server-side records
# --------------------------------------------------------------------------- #
class State(Enum):
    """FSM states, exactly as listed in fsm_specification.md."""

    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    BETTING = "BETTING"
    DEALING = "DEALING"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    DEALER_TURN = "DEALER_TURN"
    ROUND_END = "ROUND_END"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"


# States in which a game is running and a seat may be held for a rejoin.
IN_GAME_STATES = {State.BETTING, State.PLAYER_TURN, State.ROUND_END}


class ClientConn:
    """One TCP connection: socket plus its per-socket input/output buffers."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.seat = None  # set once a CONNECT is accepted
        self.alias = None
        self.closing = False  # close as soon as outbuf is flushed

    def __repr__(self):
        return f"<{self.alias or '?'}@{self.addr[0]}:{self.addr[1]}>"


class Player:
    """Everything the server keeps about one seat (survives a disconnect)."""

    def __init__(self, seat, alias, conn):
        self.seat = seat
        self.alias = alias
        self.conn = conn
        self.connected = True
        self.disconnected_at = None
        self.balance = STARTING_BALANCE
        self.wins = self.losses = self.pushes = 0
        self.reset_round()

    def reset_round(self):
        self.cards = []
        self.bet = 0
        self.status = "BETTING"
        self.result = None

    def to_payload(self):
        """Player object for STATE_UPDATE (protocol_blueprint.md 4.6)."""
        return {
            "alias": self.alias,
            "cards": list(self.cards),
            "value": hand_value(self.cards),
            "balance": self.balance,
            "bet": self.bet,
            "status": self.status,
            "connected": self.connected,
            "result": self.result,
        }


# --------------------------------------------------------------------------- #
# Game state machine (fsm_specification.md)
# --------------------------------------------------------------------------- #
class BlackjackGame:
    """Owns all game state. Has no socket code: it hands outgoing messages to
    ``send(conn, msg)`` and asks ``close(conn)`` to drop a connection, both of
    which are supplied by the network layer.
    """

    def __init__(self, send, close, rng=None):
        self._send = send
        self._close = close
        self.rng = rng or random.Random()
        self.state = State.INIT
        self.players = {}
        self._reset_table()
        self._transition(
            State.WAITING_FOR_PLAYERS, "socket bound and listening"
        )

    # ----- helpers -------------------------------------------------------- #
    def _transition(self, new_state, why):
        log.info("FSM %s -> %s (%s)", self.state.value, new_state.value, why)
        self.state = new_state

    def _reset_table(self):
        self.players = {}  # seat -> Player
        self.deck = []
        self.dealer_cards = []
        self.round = 0
        self.rounds_completed = 0
        self.active_player = None
        self.last_event = ""

    def _player_by_alias(self, alias):
        for player in self.players.values():
            if player.alias == alias:
                return player
        return None

    def _send_to(self, player, msg):
        if player.connected and player.conn is not None:
            self._send(player.conn, msg)

    def _error(self, conn, code, text, ref):
        log.info("ERROR %s -> %r: %s", code, conn, text)
        self._send(conn, make_error(code, text, ref))

    def _dealer_payload(self):
        hidden = self.state in (
            State.BETTING,
            State.DEALING,
            State.PLAYER_TURN,
            State.EVALUATE_MOVE,
        )
        if hidden and len(self.dealer_cards) == 2:
            return {"cards": [self.dealer_cards[0], "??"], "value": None}
        return {
            "cards": list(self.dealer_cards),
            "value": (
                hand_value(self.dealer_cards) if self.dealer_cards else None
            ),
        }

    def _phase(self):
        if self.state in (
            State.PLAYER_TURN,
            State.EVALUATE_MOVE,
            State.DEALING,
        ):
            return "PLAYER_TURN"
        if self.state == State.DEALER_TURN:
            return "DEALER_TURN"
        if self.state in (State.ROUND_END, State.GAME_OVER):
            return "ROUND_END"
        return "BETTING"

    def broadcast_state(self, event):
        """Send one identical full-table snapshot to both players."""
        self.last_event = event
        payload = {
            "phase": self._phase(),
            "round": self.round,
            "active_player": self.active_player,
            "dealer": self._dealer_payload(),
            "players": {s: p.to_payload() for s, p in self.players.items()},
            "last_event": event,
        }
        msg = make_msg("STATE_UPDATE", payload)
        log.info(
            "STATE_UPDATE [%s r%d] %s", payload["phase"], self.round, event
        )
        for player in self.players.values():
            self._send_to(player, msg)

    # ----- network entry points ------------------------------------------ #
    def handle_message(self, conn, msg):
        """Validate one parsed client message and dispatch it to the FSM."""
        problem = validate_envelope(msg)
        if problem:
            ref = msg.get("msg_type") if isinstance(msg, dict) else None
            ref = ref if isinstance(ref, str) else None
            self._error(conn, problem[0], problem[1], ref)
            return
        mtype = msg["msg_type"]
        log.info("RECV %s from %r", mtype, conn)

        if mtype == "CONNECT":
            self._on_connect(conn, msg)
            return
        if conn.seat is None:
            if mtype == "DISCONNECT":
                self._close(conn)
            else:
                self._error(
                    conn,
                    "WRONG_PHASE",
                    "Send CONNECT before any other message",
                    mtype,
                )
            return
        if msg["player_id"] != conn.alias:
            self._error(
                conn,
                "MALFORMED_MESSAGE",
                "player_id does not match this connection's alias",
                mtype,
            )
            return
        if mtype == "BET":
            self._on_bet(conn, msg)
        elif mtype == "MOVE":
            self._on_move(conn, msg)
        elif mtype == "DISCONNECT":
            self._on_quit(conn)

    def handle_drop(self, conn):
        """CLIENT_DISCONNECTED: socket hit EOF / reset / broken pipe."""
        seat = conn.seat
        if seat is None or seat not in self.players:
            return
        player = self.players[seat]
        if player.conn is not conn:
            return  # stale socket for this seat
        if self.state == State.WAITING_FOR_PLAYERS:
            log.info("%s (%s) left the lobby; seat freed", player.alias, seat)
            del self.players[seat]
            return
        if self.state not in IN_GAME_STATES:
            return  # already ending
        player.conn = None
        player.connected = False
        player.disconnected_at = time.monotonic()
        self.broadcast_state(
            f"{player.alias} disconnected, waiting "
            f"{REJOIN_TIMEOUT}s for them to rejoin"
        )

    def tick(self):
        """Called about once a second: fire REJOIN_TIMEOUT events."""
        if self.state not in IN_GAME_STATES:
            return
        now = time.monotonic()
        for seat, player in list(self.players.items()):
            if (
                not player.connected
                and player.disconnected_at is not None
                and now - player.disconnected_at >= REJOIN_TIMEOUT
            ):
                log.info("REJOIN_TIMEOUT for %s (%s)", player.alias, seat)
                self._forfeit(seat)
                return

    # ----- CONNECT / lobby ------------------------------------------------ #
    def _on_connect(self, conn, msg):
        alias = msg["player_id"]
        if not ALIAS_RE.match(alias) or alias == "SERVER":
            self._error(
                conn,
                "MALFORMED_MESSAGE",
                "Alias must be 1-16 letters, digits or _",
                "CONNECT",
            )
            return
        if conn.seat is not None:
            self._error(conn, "WRONG_PHASE", "Already connected", "CONNECT")
            return

        if self.state == State.WAITING_FOR_PLAYERS:
            if self._player_by_alias(alias):
                self._error(
                    conn,
                    "NAME_TAKEN",
                    f"Alias '{alias}' is already in use",
                    "CONNECT",
                )
                return
            seat = "PLAYER_1" if "PLAYER_1" not in self.players else "PLAYER_2"
            self.players[seat] = Player(seat, alias, conn)
            conn.seat, conn.alias = seat, alias
            log.info("%s seated as %s", alias, seat)
            if len(self.players) < 2:
                self._send(
                    conn,
                    make_msg(
                        "LOBBY_WAIT",
                        {
                            "message": "Waiting for Player 2",
                            "players_connected": len(self.players),
                            "players_required": 2,
                        },
                    ),
                )
            else:
                self._start_game()
            return

        # A game is in progress: only a held seat can be reclaimed.
        held = self._player_by_alias(alias)
        if held is not None and not held.connected:
            self._rejoin(conn, held)
        elif held is not None:
            self._error(
                conn,
                "NAME_TAKEN",
                f"Alias '{alias}' is already in use",
                "CONNECT",
            )
        else:
            self._error(
                conn, "LOBBY_FULL", "Two players are already seated", "CONNECT"
            )
            self._close(conn, after_flush=True)

    def _game_start_msg(self, seat):
        return make_msg(
            "GAME_START",
            {
                "seat": seat,
                "players": {s: p.alias for s, p in self.players.items()},
                "starting_balance": STARTING_BALANCE,
                "max_rounds": MAX_ROUNDS,
                "min_bet": MIN_BET,
                "max_bet": MAX_BET,
            },
        )

    def _start_game(self):
        """GAME_START (transient): assign roles, then enter BETTING."""
        self._transition(State.GAME_START, "2nd valid CONNECT")
        for seat, player in self.players.items():
            player.balance = STARTING_BALANCE
            self._send_to(player, self._game_start_msg(seat))
        self.round = 1
        self._enter_betting("Game started! Round 1: place your bets")

    def _rejoin(self, conn, player):
        conn.seat, conn.alias = player.seat, player.alias
        player.conn = conn
        player.connected = True
        player.disconnected_at = None
        log.info("%s rejoined as %s", player.alias, player.seat)
        self._send(conn, self._game_start_msg(player.seat))
        self.broadcast_state(f"{player.alias} rejoined")

    # ----- BETTING -------------------------------------------------------- #
    def _enter_betting(self, event):
        self._transition(State.BETTING, event)
        for player in self.players.values():
            player.reset_round()
        self.dealer_cards = []
        self.active_player = None
        self.deck = new_deck(self.rng)
        self.broadcast_state(event)

    def _on_bet(self, conn, msg):
        player = self.players[conn.seat]
        if self.state != State.BETTING or player.bet:
            self._error(conn, "WRONG_PHASE", "You cannot bet right now", "BET")
            return
        if "amount" not in msg["payload"]:
            self._error(
                conn, "MALFORMED_MESSAGE", "BET requires payload.amount", "BET"
            )
            return
        amount = msg["payload"]["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool):
            self._error(
                conn, "INVALID_BET", "Bet must be a whole number", "BET"
            )
            return
        if amount < MIN_BET or amount > MAX_BET:
            self._error(
                conn,
                "INVALID_BET",
                f"Bet must be between {MIN_BET} and {MAX_BET}",
                "BET",
            )
            return
        if amount > player.balance:
            self._error(
                conn,
                "INSUFFICIENT_FUNDS",
                f"You only have {player.balance}",
                "BET",
            )
            return

        player.bet = amount
        player.balance -= amount
        player.status = "WAITING"
        self.broadcast_state(f"{player.alias} bets {amount}")
        if all(p.bet for p in self.players.values()):
            self._deal()

    # ----- DEALING / PLAYER_TURN / EVALUATE_MOVE ------------------------- #
    def _deal(self):
        self._transition(State.DEALING, "both bets placed")
        for _ in range(2):
            for seat in SEATS:
                self.players[seat].cards.append(self.deck.pop())
            self.dealer_cards.append(self.deck.pop())
        for player in self.players.values():
            if hand_value(player.cards) == 21:
                player.status = "BLACKJACK"
        self._next_turn("Cards dealt")

    def _next_turn(self, event):
        """Give the turn to the first seat still able to act, else dealer."""
        for seat in SEATS:
            player = self.players[seat]
            if player.status in ("WAITING", "PLAYING"):
                self.active_player = seat
                player.status = "PLAYING"
                self._transition(State.PLAYER_TURN, f"{seat} to act")
                self.broadcast_state(f"{event}. {player.alias}'s turn")
                return
        self.active_player = None
        self._dealer_turn(event)

    def _on_move(self, conn, msg):
        # Checks in the order given by concurrency_design.md section 4.
        if self.state != State.PLAYER_TURN:
            self._error(
                conn, "WRONG_PHASE", "It is not the playing phase", "MOVE"
            )
            return
        if conn.seat != self.active_player:
            self._error(
                conn,
                "NOT_YOUR_TURN",
                f"It is {self.active_player}'s turn",
                "MOVE",
            )
            return
        if "action" not in msg["payload"]:
            self._error(
                conn,
                "MALFORMED_MESSAGE",
                "MOVE requires payload.action",
                "MOVE",
            )
            return
        action = msg["payload"]["action"]
        if action not in ("HIT", "STAND"):
            self._error(
                conn, "INVALID_ACTION", "Action must be HIT or STAND", "MOVE"
            )
            return
        self._evaluate_move(self.players[conn.seat], action)

    def _evaluate_move(self, player, action):
        self._transition(State.EVALUATE_MOVE, f"{player.alias} {action}")
        if action == "HIT":
            card = self.deck.pop()
            player.cards.append(card)
            value = hand_value(player.cards)
            event = f"{player.alias} hits: {card} ({value})"
            if value > 21:
                player.status = "BUST"
                event += " BUST"
            elif value == 21:
                player.status = "STAND"  # 21 by hitting auto-stands
                event += ", auto-stand on 21"
            else:
                self._transition(State.PLAYER_TURN, "same player continues")
                self.broadcast_state(event)
                return
        else:
            player.status = "STAND"
            event = f"{player.alias} stands on {hand_value(player.cards)}"
        self._next_turn(event)

    # ----- DEALER_TURN / ROUND_END --------------------------------------- #
    def _dealer_turn(self, event):
        self._transition(State.DEALER_TURN, "no players left to act")
        self.broadcast_state(
            f"{event}. Dealer reveals " f"{self.dealer_cards[1]}"
        )
        if any(p.status != "BUST" for p in self.players.values()):
            while hand_value(self.dealer_cards) < 17:
                card = self.deck.pop()
                self.dealer_cards.append(card)
                self.broadcast_state(
                    f"Dealer hits: {card} "
                    f"({hand_value(self.dealer_cards)})"
                )
        self._round_end()

    def _settle(self, player):
        """Apply fsm_specification.md section 2.2 to one player."""
        dealer = hand_value(self.dealer_cards)
        dealer_bj = dealer == 21 and len(self.dealer_cards) == 2
        value = hand_value(player.cards)
        if player.status == "BUST":
            result, payout = "LOSS", 0
        elif player.status == "BLACKJACK" and not dealer_bj:
            result, payout = "WIN", player.bet + (3 * player.bet) // 2
        elif dealer > 21 or value > dealer:
            result, payout = "WIN", 2 * player.bet
        elif value == dealer:
            result, payout = "PUSH", player.bet
        else:
            result, payout = "LOSS", 0
        player.balance += payout
        player.result = result
        if result == "WIN":
            player.wins += 1
        elif result == "LOSS":
            player.losses += 1
        else:
            player.pushes += 1
        return f"{player.alias} {result}"

    def _round_end(self):
        self._transition(State.ROUND_END, "dealer stands")
        summary = ", ".join(self._settle(p) for p in self.players.values())
        dealer = hand_value(self.dealer_cards)
        self.rounds_completed = self.round
        self.broadcast_state(
            f"Round {self.round} over. Dealer has {dealer}"
            f"{' (BUST)' if dealer > 21 else ''}. {summary}"
        )
        # A balance below MIN_BET can never place another bet, so it counts
        # as broke (balance 0 is the common case).
        if any(p.balance < MIN_BET for p in self.players.values()):
            self._game_over("PLAYER_BROKE")
        elif self.round >= MAX_ROUNDS:
            self._game_over("ROUNDS_COMPLETE")
        else:
            self.round += 1
            self._enter_betting(f"Round {self.round}: place your bets")

    # ----- quitting / GAME_OVER / CLEANUP -------------------------------- #
    def _on_quit(self, conn):
        """PLAYER_QUIT: an intentional DISCONNECT message."""
        seat = conn.seat
        if self.state == State.WAITING_FOR_PLAYERS:
            log.info("%s quit the lobby; seat freed", conn.alias)
            del self.players[seat]
            self._close(conn)
        elif self.state in IN_GAME_STATES:
            self._forfeit(seat)
        else:
            self._close(conn)

    def _forfeit(self, seat):
        """The leaving seat loses its bet; the other player wins by forfeit."""
        leaver = self.players[seat]
        for player in self.players.values():
            if player is not leaver:
                player.balance += player.bet  # their wager is returned
                player.bet = 0
        leaver.bet = 0  # already deducted: it is lost
        self._game_over("FORFEIT", forfeited_by=seat)

    def _game_over(self, reason, forfeited_by=None):
        self._transition(State.GAME_OVER, reason)
        results = {}
        for seat, player in self.players.items():
            net = player.balance - STARTING_BALANCE
            if forfeited_by:
                outcome = (
                    "FORFEIT" if seat == forfeited_by else "WIN_BY_FORFEIT"
                )
            elif net > 0:
                outcome = "WIN"
            elif net < 0:
                outcome = "LOSS"
            else:
                outcome = "DRAW"
            results[seat] = {
                "alias": player.alias,
                "final_balance": player.balance,
                "net": net,
                "outcome": outcome,
            }
        msg = make_msg(
            "GAME_OVER",
            {
                "reason": reason,
                "rounds_played": self.rounds_completed,
                "forfeited_by": forfeited_by,
                "results": results,
            },
        )
        log.info("GAME_OVER %s %s", reason, json.dumps(results))
        for player in self.players.values():
            self._send_to(player, msg)
        self._cleanup()

    def _cleanup(self):
        self._transition(State.CLEANUP, "GAME_OVER sent")
        for player in self.players.values():
            if player.conn is not None:
                player.conn.seat = player.conn.alias = None
                self._close(player.conn, after_flush=True)
        self._reset_table()
        self._transition(
            State.WAITING_FOR_PLAYERS, "table reset for next game"
        )


# --------------------------------------------------------------------------- #
# Network layer: single-threaded selectors event loop (concurrency_design.md)
# --------------------------------------------------------------------------- #
class BlackjackServer:
    """Accepts connections and moves bytes; all game logic is in the FSM."""

    def __init__(self, port):
        self.port = port
        self.sel = selectors.DefaultSelector()
        self.listener = None
        self.pending_drops = []  # sends that failed mid-handler
        self.game = BlackjackGame(send=self.queue_send, close=self.close_conn)

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((LISTEN_HOST, self.port))
        self.listener.listen()
        self.listener.setblocking(False)
        self.sel.register(self.listener, selectors.EVENT_READ, data=None)
        log.info("Blackjack server listening on %s:%d", LISTEN_HOST, self.port)

    def serve_forever(self):
        try:
            while True:
                for key, mask in self.sel.select(timeout=SELECT_TIMEOUT):
                    if key.data is None:
                        self._accept()
                        continue
                    conn = key.data
                    if conn.sock.fileno() == -1:
                        continue  # closed earlier this pass
                    if mask & selectors.EVENT_READ:
                        self._read(conn)
                    if (
                        mask & selectors.EVENT_WRITE
                        and conn.sock.fileno() != -1
                    ):
                        self._write(conn)
                    self._process_pending_drops()
                self.game.tick()
                self._process_pending_drops()
        finally:
            self.sel.close()
            self.listener.close()

    # ----- accept / read / write ----------------------------------------- #
    def _accept(self):
        try:
            sock, addr = self.listener.accept()
        except BlockingIOError:
            return
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn = ClientConn(sock, addr)
        self.sel.register(sock, selectors.EVENT_READ, data=conn)
        log.info("Accepted connection from %s:%d", *addr)

    def _read(self, conn):
        try:
            data = conn.sock.recv(RECV_SIZE)
        except BlockingIOError:
            return
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ) as exc:
            log.warning("%r connection lost abruptly: %s", conn, exc)
            self._drop(conn)
            return
        if not data:  # EOF: peer sent FIN
            log.info("%r closed the connection (EOF)", conn)
            self._drop(conn)
            return
        if conn.closing:
            return  # ignore input while closing
        conn.inbuf.extend(data)
        try:
            frames = extract_frames(conn.inbuf)
        except FrameTooLarge:
            log.warning("%r sent an oversized frame", conn)
            self.queue_send(
                conn,
                make_error(
                    "MESSAGE_TOO_LARGE",
                    f"Frame exceeded {MAX_FRAME} bytes",
                    None,
                ),
            )
            self.game.handle_drop(conn)
            self.close_conn(conn, after_flush=True)
            return
        for msg in frames:
            if conn.closing or conn.sock.fileno() == -1:
                break
            if msg is None:
                self.queue_send(
                    conn,
                    make_error(
                        "MALFORMED_MESSAGE",
                        "Frame is not valid UTF-8 JSON",
                        None,
                    ),
                )
            else:
                self.game.handle_message(conn, msg)

    def queue_send(self, conn, msg):
        """Append a frame to the client's output buffer and try to flush."""
        if conn.sock.fileno() == -1:
            return
        conn.outbuf.extend(encode_frame(msg))
        self._write(conn)

    def _write(self, conn):
        try:
            while conn.outbuf:
                sent = conn.sock.send(conn.outbuf)
                del conn.outbuf[:sent]
        except BlockingIOError:
            pass  # kernel buffer full
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ) as exc:
            # Never re-enter the FSM from inside one of its own handlers:
            # close now, report CLIENT_DISCONNECTED after the event finishes.
            log.warning("%r send failed: %s", conn, exc)
            self._really_close(conn)
            self.pending_drops.append(conn)
            return
        if conn.outbuf:
            self._set_events(
                conn, selectors.EVENT_READ | selectors.EVENT_WRITE
            )
        elif conn.closing:
            self._really_close(conn)
        else:
            self._set_events(conn, selectors.EVENT_READ)

    def _set_events(self, conn, events):
        try:
            if self.sel.get_key(conn.sock).events != events:
                self.sel.modify(conn.sock, events, data=conn)
        except (KeyError, ValueError):
            pass

    # ----- closing --------------------------------------------------------- #
    def _drop(self, conn):
        """Unexpected drop: close the socket; the FSM holds the seat."""
        self._really_close(conn)
        self.game.handle_drop(conn)

    def _process_pending_drops(self):
        while self.pending_drops:
            self.game.handle_drop(self.pending_drops.pop(0))

    def close_conn(self, conn, after_flush=False):
        """Close now, or once everything queued for this client is sent."""
        conn.closing = True
        if not after_flush or not conn.outbuf:
            self._really_close(conn)

    def _really_close(self, conn):
        if conn.sock.fileno() == -1:
            return
        try:
            self.sel.unregister(conn.sock)  # unregister BEFORE close
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass
        log.info("Closed connection %r", conn)


# --------------------------------------------------------------------------- #
# CLI
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
        description="CS457 two-player Blackjack server. Binds to 0.0.0.0 "
        "and deals for exactly two TCP clients.",
        epilog="Example: python3 server.py -p 5457",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=port_number,
        required=True,
        help="TCP port to listen on (binds to 0.0.0.0)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    server = BlackjackServer(args.port)
    try:
        server.start()
    except OSError as exc:
        log.error("Cannot listen on port %d: %s", args.port, exc)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server shutting down (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
