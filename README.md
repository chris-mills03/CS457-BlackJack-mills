# CS457-BlackJack-mills

Two-player networked Blackjack for CS 457 (Computer Networks). The server is
the dealer, and two TCP clients each play a seat.

**Author:** Christopher Aidan Mills

| Document | Contents |
|---|---|
| [`protocol_blueprint.md`](protocol_blueprint.md) | Sprint 1: message types, newline-delimited JSON framing, error codes |
| [`fsm_specification.md`](fsm_specification.md) | Sprint 1: server game state machine |
| [`concurrency_design.md`](concurrency_design.md) | Sprint 2: `selectors` event loop, turn enforcement, rejoin |
| [`server.py`](server.py) / [`client.py`](client.py) | Sprint 3: implementation (Python 3.10+, standard library only) |

## Running

Start the server (binds to `0.0.0.0`):

```bash
python3 server.py -h
python3 server.py -p 5457
```

Start two clients, in two other terminals:

```bash
python3 client.py -h
python3 client.py -i server.mills.edu -p 5457            # prompts for a name
python3 client.py -i localhost -p 5457 -n Alice          # local testing
```

`-i` is a DNS hostname resolved with `socket.gethostbyname()`. If it can't be
resolved, the client prints a DNS error and exits with status `1` (no
traceback). `-n` is optional; without it the client asks for a player name.

### Client commands

| Command | Message sent | When |
|---|---|---|
| `50` or `bet 50` | `BET` | BETTING phase, once per round |
| `h` / `hit` | `MOVE HIT` | your turn |
| `s` / `stand` | `MOVE STAND` | your turn |
| `help` | (none) | any time |
| `q` / `quit` | `DISCONNECT` | any time (forfeits a game in progress) |

The server validates every command. An invalid or out-of-turn command gets an
`ERROR` back and the game state doesn't change.

## Implementation notes

- **Framing:** each message is compact JSON plus one `\n`. Every socket has its
  own `bytearray` buffer, and `extract_frames()` pulls out every complete line
  after each `recv()`. A frame split across several `recv()` calls
  (fragmentation) or several frames in one `recv()` (coalescing) are both
  handled. A frame larger than 65,536 bytes (including the `\n`) gets
  `ERROR MESSAGE_TOO_LARGE` and the connection is closed.
- **Concurrency:** a single-threaded `selectors` loop with non-blocking
  sockets and per-client output buffers (`EVENT_WRITE` while there is unsent
  data), so a slow client never blocks the other one.
- **FSM:** `BlackjackGame` implements the states in `fsm_specification.md`,
  and every transition is logged (`FSM BETTING -> DEALING ...`).
- **Disconnects:** `DISCONNECT` forfeits right away. A dropped connection
  (EOF/RST) holds the seat for 60 s, and the player rejoins by connecting
  again with the same name.
- One addition to the spec: a balance below `min_bet` (not only `0`) ends the
  game as `PLAYER_BROKE`, because that player can't place another bet.

## Tests and demo log

```bash
python3 -m unittest discover -s tests -v     # framing, FSM, real-socket tests
python3 tools/demo_session.py -p 5457        # scripted 2-client game -> demo/
```

`tools/demo_session.py` starts the server and two clients, types commands into
them (including one invalid bet and one out-of-turn move), and saves every
console to `demo/`.
