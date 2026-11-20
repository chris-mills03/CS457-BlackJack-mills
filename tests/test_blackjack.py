"""Tests for the CS457 Blackjack server and client (standard library only).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client  # noqa: E402
import server  # noqa: E402
from server import (  # noqa: E402
    MAX_FRAME,
    BlackjackGame,
    ClientConn,
    FrameTooLarge,
    State,
    encode_frame,
    extract_frames,
    hand_value,
)


def msg(mtype, alias, payload=None):
    return {
        "msg_type": mtype,
        "player_id": alias,
        "timestamp": 1759600000,
        "payload": payload or {},
    }


# --------------------------------------------------------------------------- #
# Framing (protocol_blueprint.md section 2)
# --------------------------------------------------------------------------- #
class FramingTests(unittest.TestCase):
    def test_coalesced_frames_in_one_chunk(self):
        buf = bytearray(
            b"".join(
                encode_frame(msg("BET", "A", {"amount": n}))
                for n in (10, 20, 30)
            )
        )
        frames = extract_frames(buf)
        self.assertEqual(
            [f["payload"]["amount"] for f in frames], [10, 20, 30]
        )
        self.assertEqual(buf, bytearray())

    def test_fragmented_frame_byte_by_byte(self):
        wire = encode_frame(msg("MOVE", "Bob", {"action": "HIT"}))
        buf = bytearray()
        for i, byte in enumerate(wire):
            buf.append(byte)
            frames = extract_frames(buf)
            if i < len(wire) - 1:
                self.assertEqual(frames, [])  # nothing parsed yet
        self.assertEqual(frames[0]["payload"]["action"], "HIT")
        self.assertEqual(buf, bytearray())

    def test_blueprint_split_example(self):
        buf = bytearray(b'{"msg_type":"MOVE","player_id":"Bob","times')
        self.assertEqual(extract_frames(buf), [])
        buf.extend(b'tamp":1759600040,"payload":{"action":"HIT"}}\n')
        self.assertEqual(extract_frames(buf)[0]["msg_type"], "MOVE")

    def test_mixed_end_full_and_start(self):
        a, b, c = (
            encode_frame(msg("BET", "A", {"amount": n})) for n in (1, 2, 3)
        )
        buf = bytearray(a + b + c[:7])
        self.assertEqual(len(extract_frames(buf)), 2)
        self.assertEqual(bytes(buf), c[:7])

    def test_newline_inside_string_is_one_frame(self):
        m = msg("DISCONNECT", "A", {"reason": "line1\nline2"})
        wire = encode_frame(m)
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertEqual(extract_frames(bytearray(wire)), [m])

    def test_invalid_json_and_empty_lines(self):
        buf = bytearray(b"\n\nnot json\n")
        self.assertEqual(extract_frames(buf), [None])

    def test_max_size_frame_is_accepted(self):
        m = msg("DISCONNECT", "A", {"reason": ""})
        pad = MAX_FRAME - len(encode_frame(m))
        m["payload"]["reason"] = "x" * pad
        wire = encode_frame(m)
        self.assertEqual(len(wire), MAX_FRAME)
        self.assertEqual(len(extract_frames(bytearray(wire))), 1)

    def test_oversized_without_newline_raises(self):
        with self.assertRaises(FrameTooLarge):
            extract_frames(bytearray(b"x" * MAX_FRAME))

    def test_oversized_with_newline_raises(self):
        with self.assertRaises(FrameTooLarge):
            extract_frames(bytearray(b"x" * MAX_FRAME + b"\n"))

    def test_client_framing_matches_server(self):
        m = msg("STATE_UPDATE", "SERVER", {"x": 1})
        self.assertEqual(client.encode_frame(m), encode_frame(m))
        self.assertEqual(
            client.extract_frames(bytearray(encode_frame(m))), [m]
        )


class CardTests(unittest.TestCase):
    def test_hand_values(self):
        self.assertEqual(hand_value(["AS", "KD"]), 21)
        self.assertEqual(hand_value(["AS", "AD", "9C"]), 21)
        self.assertEqual(hand_value(["10H", "9C", "5D"]), 24)
        self.assertEqual(hand_value(["AS", "5D", "KD"]), 16)


# --------------------------------------------------------------------------- #
# FSM (fsm_specification.md) with fake sockets
# --------------------------------------------------------------------------- #
class FakeConn(ClientConn):
    def __init__(self, name):
        super().__init__(sock=None, addr=("127.0.0.1", 0))
        self.name = name
        self.inbox = []


class FsmTests(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.game = BlackjackGame(
            send=lambda c, m: c.inbox.append(m),
            close=lambda c, after_flush=False: self.closed.append(c),
        )
        self.a, self.b = FakeConn("A"), FakeConn("B")

    def stack(self, cards):
        """Next cards popped from the deck, in deal order."""
        self.game.deck = self.game.deck + list(reversed(cards))

    def last(self, conn, mtype=None):
        for m in reversed(conn.inbox):
            if mtype is None or m["msg_type"] == mtype:
                return m
        return None

    def join_both(self):
        self.game.handle_message(self.a, msg("CONNECT", "Alice"))
        self.game.handle_message(self.b, msg("CONNECT", "Bob"))

    def bet_both(self, amount=50):
        self.game.handle_message(
            self.a, msg("BET", "Alice", {"amount": amount})
        )
        self.game.handle_message(self.b, msg("BET", "Bob", {"amount": amount}))

    def test_lobby_wait_then_game_start(self):
        self.game.handle_message(self.a, msg("CONNECT", "Alice"))
        self.assertEqual(self.a.inbox[0]["msg_type"], "LOBBY_WAIT")
        self.game.handle_message(self.b, msg("CONNECT", "Bob"))
        self.assertEqual(
            self.last(self.a, "GAME_START")["payload"]["seat"], "PLAYER_1"
        )
        self.assertEqual(
            self.last(self.b, "GAME_START")["payload"]["seat"], "PLAYER_2"
        )
        self.assertEqual(self.game.state, State.BETTING)

    def test_name_taken_and_bad_alias(self):
        self.game.handle_message(self.a, msg("CONNECT", "Alice"))
        self.game.handle_message(self.b, msg("CONNECT", "Alice"))
        self.assertEqual(self.last(self.b)["payload"]["code"], "NAME_TAKEN")
        self.game.handle_message(self.b, msg("CONNECT", "bad name!"))
        self.assertEqual(
            self.last(self.b)["payload"]["code"], "MALFORMED_MESSAGE"
        )

    def test_lobby_full(self):
        self.join_both()
        c = FakeConn("C")
        self.game.handle_message(c, msg("CONNECT", "Carol"))
        self.assertEqual(self.last(c)["payload"]["code"], "LOBBY_FULL")
        self.assertIn(c, self.closed)

    def test_envelope_errors(self):
        self.game.handle_message(self.a, {"msg_type": "CONNECT"})
        self.assertEqual(
            self.last(self.a)["payload"]["code"], "MALFORMED_MESSAGE"
        )
        self.game.handle_message(self.a, msg("LOBBY_WAIT", "Alice"))
        self.assertEqual(
            self.last(self.a)["payload"]["code"], "UNKNOWN_MSG_TYPE"
        )
        self.game.handle_message(self.a, msg("BET", "Alice", {"amount": 10}))
        self.assertEqual(self.last(self.a)["payload"]["code"], "WRONG_PHASE")

    def test_bet_validation(self):
        self.join_both()
        g = self.game
        for amount, code in (
            (5, "INVALID_BET"),
            ("50", "INVALID_BET"),
            (True, "INVALID_BET"),
            (501, "INVALID_BET"),
        ):
            g.handle_message(self.a, msg("BET", "Alice", {"amount": amount}))
            self.assertEqual(self.last(self.a)["payload"]["code"], code)
        g.players["PLAYER_1"].balance = 40
        g.handle_message(self.a, msg("BET", "Alice", {"amount": 50}))
        self.assertEqual(
            self.last(self.a)["payload"]["code"], "INSUFFICIENT_FUNDS"
        )
        g.handle_message(self.a, msg("BET", "Alice", {"amount": 20}))
        g.handle_message(self.a, msg("BET", "Alice", {"amount": 20}))
        self.assertEqual(self.last(self.a)["payload"]["code"], "WRONG_PHASE")
        g.handle_message(self.a, msg("BET", "Bob", {"amount": 20}))
        self.assertEqual(
            self.last(self.a)["payload"]["code"], "MALFORMED_MESSAGE"
        )  # alias mismatch

    def test_full_round_turns_and_settlement(self):
        self.join_both()
        # deal order: P1, P2, dealer, P1, P2, dealer, then hits
        self.stack(["9H", "QH", "10S", "5C", "7S", "7D", "4D"])
        self.bet_both(50)
        g = self.game
        self.assertEqual(g.state, State.PLAYER_TURN)
        self.assertEqual(g.active_player, "PLAYER_1")
        upd = self.last(self.b, "STATE_UPDATE")["payload"]
        self.assertEqual(upd["dealer"]["cards"], ["10S", "??"])

        # Bob out of turn
        g.handle_message(self.b, msg("MOVE", "Bob", {"action": "HIT"}))
        self.assertEqual(self.last(self.b)["payload"]["code"], "NOT_YOUR_TURN")
        g.handle_message(self.a, msg("MOVE", "Alice", {"action": "FOLD"}))
        self.assertEqual(
            self.last(self.a)["payload"]["code"], "INVALID_ACTION"
        )

        g.handle_message(self.a, msg("MOVE", "Alice", {"action": "HIT"}))  # 18
        self.assertEqual(g.active_player, "PLAYER_1")
        g.handle_message(self.a, msg("MOVE", "Alice", {"action": "STAND"}))
        self.assertEqual(g.active_player, "PLAYER_2")
        g.handle_message(self.b, msg("MOVE", "Bob", {"action": "STAND"}))

        # Dealer 17 stands. Alice 18 wins, Bob 17 pushes; next round begins.
        ends = [
            m["payload"]
            for m in self.a.inbox
            if m["msg_type"] == "STATE_UPDATE"
            and m["payload"]["phase"] == "ROUND_END"
        ]
        self.assertEqual(ends[-1]["players"]["PLAYER_1"]["result"], "WIN")
        self.assertEqual(ends[-1]["players"]["PLAYER_2"]["result"], "PUSH")
        self.assertEqual(g.players["PLAYER_1"].balance, 550)
        self.assertEqual(g.players["PLAYER_2"].balance, 500)
        self.assertEqual(g.state, State.BETTING)
        self.assertEqual(g.round, 2)

    def test_bust_passes_turn_and_blackjack_pays_3_to_2(self):
        self.join_both()
        self.stack(["10H", "AS", "9C", "6C", "KD", "8D", "9S"])
        self.bet_both(50)
        g = self.game
        self.assertEqual(g.players["PLAYER_2"].status, "BLACKJACK")
        g.handle_message(self.a, msg("MOVE", "Alice", {"action": "HIT"}))
        # Alice 25 bust, Bob has blackjack -> straight to dealer and round end
        self.assertEqual(g.round, 2)
        self.assertEqual(g.players["PLAYER_1"].balance, 450)
        self.assertEqual(g.players["PLAYER_2"].balance, 575)

    def test_disconnect_message_forfeits(self):
        self.join_both()
        self.game.handle_message(self.a, msg("BET", "Alice", {"amount": 100}))
        self.game.handle_message(
            self.b, msg("DISCONNECT", "Bob", {"reason": "user quit"})
        )
        over = self.last(self.a, "GAME_OVER")["payload"]
        self.assertEqual(over["reason"], "FORFEIT")
        self.assertEqual(over["forfeited_by"], "PLAYER_2")
        self.assertEqual(
            over["results"]["PLAYER_1"]["outcome"], "WIN_BY_FORFEIT"
        )
        self.assertEqual(over["results"]["PLAYER_1"]["final_balance"], 500)
        self.assertEqual(self.game.state, State.WAITING_FOR_PLAYERS)

    def test_drop_holds_seat_then_rejoin(self):
        self.join_both()
        self.game.handle_drop(self.b)
        upd = self.last(self.a, "STATE_UPDATE")["payload"]
        self.assertFalse(upd["players"]["PLAYER_2"]["connected"])
        b2 = FakeConn("B2")
        self.game.handle_message(b2, msg("CONNECT", "Bob"))
        self.assertEqual(b2.inbox[0]["msg_type"], "GAME_START")
        self.assertEqual(b2.inbox[0]["payload"]["seat"], "PLAYER_2")
        self.assertEqual(b2.inbox[1]["msg_type"], "STATE_UPDATE")
        self.assertTrue(self.game.players["PLAYER_2"].connected)

    def test_rejoin_timeout_forfeits(self):
        self.join_both()
        self.game.handle_drop(self.b)
        self.game.players["PLAYER_2"].disconnected_at -= server.REJOIN_TIMEOUT
        self.game.tick()
        over = self.last(self.a, "GAME_OVER")["payload"]
        self.assertEqual(over["results"]["PLAYER_2"]["outcome"], "FORFEIT")

    def test_lobby_drop_frees_seat(self):
        self.game.handle_message(self.a, msg("CONNECT", "Alice"))
        self.game.handle_drop(self.a)
        self.game.handle_message(self.b, msg("CONNECT", "Bob"))
        self.assertEqual(self.b.seat, "PLAYER_1")


# --------------------------------------------------------------------------- #
# Real sockets: fragmentation, coalescing, oversized frame over TCP
# --------------------------------------------------------------------------- #
class Peer:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buf = bytearray()
        self.msgs = []

    def close(self):
        self.sock.close()

    def send_raw(self, data):
        self.sock.sendall(data)

    def wait_for(self, pred, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            for m in self.msgs:
                if pred(m):
                    return m
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            self.buf.extend(data)
            self.msgs.extend(extract_frames(self.buf))
        for m in self.msgs:
            if pred(m):
                return m
        raise AssertionError(f"message not received; got {self.msgs}")


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.srv = server.BlackjackServer(0)
        self.srv.start()
        self.port = self.srv.listener.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        # Stop accepting; the daemon thread dies with the test process.
        try:
            self.srv.listener.close()
        except OSError:
            pass

    def test_fragmented_connect_and_coalesced_bets(self):
        a, b = Peer(self.port), Peer(self.port)
        for byte in encode_frame(msg("CONNECT", "Alice")):
            a.send_raw(bytes([byte]))  # one byte per TCP segment
            time.sleep(0.001)
        a.wait_for(lambda m: m["msg_type"] == "LOBBY_WAIT")
        b.send_raw(encode_frame(msg("CONNECT", "Bob")))
        b.wait_for(lambda m: m["msg_type"] == "GAME_START")
        # Two frames in one send(): bad bet then good bet.
        a.send_raw(
            encode_frame(msg("BET", "Alice", {"amount": 1}))
            + encode_frame(msg("BET", "Alice", {"amount": 50}))
        )
        a.wait_for(
            lambda m: m["msg_type"] == "ERROR"
            and m["payload"]["code"] == "INVALID_BET"
        )
        b.wait_for(
            lambda m: m["msg_type"] == "STATE_UPDATE"
            and m["payload"]["players"]["PLAYER_1"]["bet"] == 50
        )
        a.close()
        b.close()

    def test_oversized_frame_closes_connection(self):
        a = Peer(self.port)
        a.send_raw(b"x" * (MAX_FRAME + 10))
        a.wait_for(
            lambda m: m
            and m["msg_type"] == "ERROR"
            and m["payload"]["code"] == "MESSAGE_TOO_LARGE"
        )
        # Server closed the socket. Unread junk in its receive buffer makes
        # the kernel send RST instead of FIN, so accept either.
        try:
            self.assertEqual(a.sock.recv(10), b"")
        except ConnectionResetError:
            pass
        a.close()

    def test_invalid_json_keeps_connection_open(self):
        a = Peer(self.port)
        a.send_raw(b"{not json}\n")
        a.wait_for(lambda m: m["payload"].get("code") == "MALFORMED_MESSAGE")
        a.send_raw(encode_frame(msg("CONNECT", "Alice")))
        a.wait_for(lambda m: m["msg_type"] == "LOBBY_WAIT")
        a.close()


class CardArtTests(unittest.TestCase):
    def test_card_shape_and_rank(self):
        art = client.card_art("10H")
        self.assertEqual(len(art), client.CARD_ROWS)
        self.assertIn("10", art[1])
        self.assertIn("10", art[3])
        art = client.card_art("QS")
        self.assertTrue(all(len(row) == 7 for row in art))

    def test_hidden_card_has_no_rank(self):
        self.assertNotIn("?", "".join(client.card_art("??")))

    def test_hand_side_by_side(self):
        rows = client.hand_art(["AS", "KD", "??"], indent="")
        self.assertEqual(len(rows), client.CARD_ROWS)
        self.assertEqual(len(rows[0]), 3 * 7 + 2)
        self.assertIn("no cards", client.hand_art([])[0])


class ClientCliTests(unittest.TestCase):
    def test_dns_failure_exits_1(self):
        with self.assertRaises(SystemExit) as ctx:
            client.resolve_server("no-such-host.invalid")
        self.assertEqual(ctx.exception.code, 1)

    def test_args(self):
        args = client.parse_args(["-i", "server.mills.edu", "-p", "5457"])
        self.assertEqual(
            (args.hostname, args.port), ("server.mills.edu", 5457)
        )
        self.assertEqual(server.parse_args(["-p", "8080"]).port, 8080)
        with self.assertRaises(SystemExit):
            client.parse_args(["-i", "x"])  # -p is required


if __name__ == "__main__":
    unittest.main()
