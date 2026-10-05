# AI Prompting & Constraint Strategy — CS457 Blackjack

**Author:** Christopher Aidan Mills
**Course:** CS 457 — Computer Networks

AI coding assistants tend to produce generic socket boilerplate: a single `recv(1024)` treated as one message, ad-hoc string commands, and no EOF handling. This document records the prompts I use to stop that and force the AI to implement **my** protocol ([`protocol_blueprint.md`](protocol_blueprint.md)) and **my** state machine ([`fsm_specification.md`](fsm_specification.md)) exactly.

## 1. Constraint Strategy

1. **Spec as context, not a summary.** The full text of `protocol_blueprint.md` and `fsm_specification.md` is pasted into every session. The AI is told those documents override its own defaults.
2. **Hard rules in the system prompt.** Non-negotiable rules (framing, envelope, enums, EOF, exceptions) are listed explicitly, along with what is forbidden.
3. **One small unit per prompt.** I ask for one function or module at a time (framing → validation → FSM → server loop), never "write a blackjack server".
4. **Fixed signatures.** I give the exact function names, parameters and return types, so the generated code plugs into my design instead of inventing its own.
5. **Tests first.** Every prompt asks for `pytest` tests built from the wire examples in the blueprint, including coalesced and fragmented streams.
6. **Self-check.** The AI must end its answer with a checklist that maps each rule to the line of code that satisfies it.
7. **Human review.** I reject any output that adds message types, fields or states that are not in the spec, or that skips the EOF/exception paths. Then I re-prompt with the specific rule it broke.

## 2. System Prompt (used for every coding session)

```text
You are implementing a networked 2-player Blackjack game in Python 3.11 using only
the standard library (socket, selectors or threading, json, struct, logging,
random, dataclasses, typing). The attached documents protocol_blueprint.md and
fsm_specification.md are the AUTHORITATIVE specification. If anything in your
training or habits conflicts with them, the documents win. Do not add, rename, or
remove message types, fields, enum values, error codes, or FSM states.

HARD RULES:
1. Transport is TCP. Framing is newline-delimited JSON: each message is
   json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n", sent with sendall().
2. Never assume one recv() equals one message. Keep a bytearray buffer per socket,
   append each chunk, and extract EVERY complete \n-terminated frame in a loop.
   Keep partial bytes. If the buffer exceeds 65536 bytes with no \n, send ERROR
   MESSAGE_TOO_LARGE and close the socket.
3. Every message has exactly the envelope keys: msg_type (str), player_id (str),
   timestamp (int, epoch seconds), payload (object). Server messages use
   player_id "SERVER".
4. The only msg_type values are: CONNECT, LOBBY_WAIT, GAME_START, BET, MOVE,
   STATE_UPDATE, ERROR, DISCONNECT, GAME_OVER. Clients may only send CONNECT, BET,
   MOVE, DISCONNECT.
5. Validate every client message against the schema (types, enums, ranges) BEFORE
   it reaches the FSM. Invalid input -> send an ERROR with the exact code from the
   spec to that client only. Never raise out of the receive loop, never close the
   connection (except MESSAGE_TOO_LARGE / LOBBY_FULL), never change state.
6. recv() returning b"" is EOF: stop reading immediately (never loop on it).
7. Catch ConnectionResetError, ConnectionAbortedError, BrokenPipeError and
   TimeoutError on every recv/send. EOF, these exceptions, and a DISCONNECT message
   must ALL route to the single FSM event CLIENT_DISCONNECTED(seat).
8. Money is int only. Cards are strings "<rank><suit>" (e.g. "10H", "AS"); the
   dealer hole card is "??" until DEALER_TURN.
9. The FSM must use exactly these states: INIT, WAITING_FOR_PLAYERS, GAME_START,
   BETTING, DEALING, PLAYER_TURN, EVALUATE_MOVE, DEALER_TURN, ROUND_END, GAME_OVER,
   CLEANUP, and only the transitions in the diagram.

FORBIDDEN: plain-text commands like "hit\n"; recv(1024) treated as a full message;
pickle; eval; bare except:; new message types or fields; global mutable state
without a lock; printing instead of the logging module on the server.

OUTPUT FORMAT: (1) the code, (2) pytest tests, (3) a checklist mapping each
HARD RULE number to the function/line that satisfies it. If the spec is ambiguous,
ask me instead of guessing.
```

## 3. Task Prompts

### 3.1 Framing layer (`protocol/framing.py`)

```text
Using the system rules and protocol_blueprint.md §2, implement protocol/framing.py
with EXACTLY these public functions:

  MAX_FRAME: int = 65536
  class FrameTooLarge(Exception): ...
  def encode_frame(msg: dict) -> bytes
  def extract_frames(buffer: bytearray) -> list[dict | None]
      # removes every complete \n frame from buffer in place; returns parsed dicts,
      # None for a frame that is not valid UTF-8 JSON; skips empty lines;
      # raises FrameTooLarge if len(buffer) > MAX_FRAME after extraction
  def send_msg(sock: socket.socket, msg: dict) -> None   # sendall(encode_frame(msg))

Write pytest tests that use the exact wire streams in §2.3:
 - three coalesced server messages in one chunk -> 3 dicts, buffer empty
 - the MOVE split across two chunks -> [] after chunk 1, [MOVE] after chunk 2
 - a string payload containing "\n" round-trips through encode_frame/extract_frames
   as ONE frame
 - invalid JSON line -> [None]; 65537 bytes with no \n -> FrameTooLarge
Do not write any socket server code in this step.
```

### 3.2 Schema validation & message builders (`protocol/messages.py`)

```text
Using protocol_blueprint.md §3–§4, implement protocol/messages.py:

  class ProtocolError(Exception):  # has .code (one of the spec's ERROR codes) and .message
  def validate_client_message(msg: dict, registered_alias: str | None) -> dict
      # checks envelope keys/types, msg_type in {CONNECT, BET, MOVE, DISCONNECT},
      # alias regex ^[A-Za-z0-9_]{1,16}$, BET.amount is int (not bool),
      # MOVE.action in {"HIT","STAND"}, player_id == registered_alias after CONNECT.
      # Raises ProtocolError with MALFORMED_MESSAGE / UNKNOWN_MSG_TYPE / INVALID_ACTION /
      # INVALID_BET. Range/funds/turn/phase checks are NOT done here (FSM does them).
  One builder per server message, returning a dict that matches the spec exactly:
  lobby_wait(), game_start(seat, players, cfg), state_update(table),
  error(code, message, ref_msg_type), game_over(reason, rounds_played, forfeited_by, results)

Every builder sets player_id="SERVER" and timestamp=int(time.time()).
Tests: each example JSON in §4 must validate (client types) or be reproduced
exactly by its builder (server types, timestamp mocked). Add one negative test
per ERROR code that this module raises.
```

### 3.3 Game state machine (`server/fsm.py`)

```text
Implement server/fsm.py as a pure class with NO socket code, exactly matching the
mermaid diagram and tables in fsm_specification.md.

  class State(Enum): INIT, WAITING_FOR_PLAYERS, GAME_START, BETTING, DEALING,
                     PLAYER_TURN, EVALUATE_MOVE, DEALER_TURN, ROUND_END, GAME_OVER, CLEANUP
  class GameFSM:
      def __init__(self, cfg, rng: random.Random)
      def handle(self, seat: str | None, msg: dict) -> list[tuple[str | None, dict]]
      def handle_event(self, event: str, seat: str) -> list[tuple[str | None, dict]]
          # event is "CLIENT_DISCONNECTED"
  Return value = outbound messages as (target_seat, message), where target None means
  broadcast. Use the builders from protocol/messages.py.

Transient states must run their entry action and advance immediately in the same
call. Out-of-turn MOVE -> ERROR NOT_YOUR_TURN with state unchanged. Implement turn
order (§2.1), settlement (§2.2) and end conditions (§2.3) exactly. Inject rng so tests
can stack the deck.
Tests: happy-path full round; PLAYER_1 busts but the round continues to PLAYER_2;
both blackjack -> DEALER_TURN; out-of-turn MOVE; invalid bet; disconnect in each of
WAITING_FOR_PLAYERS, BETTING, PLAYER_TURN with the expected GAME_OVER payload;
ROUND_END -> BETTING reset and CLEANUP -> WAITING_FOR_PLAYERS reset.
```

### 3.4 Server network loop (`server/server.py`)

```text
Implement server/server.py: the TCP server that connects sockets to GameFSM.
Use the concurrency model chosen in the SOW (Sprint 2); all GameFSM calls must be
serialized (one at a time) so game state is never mutated concurrently.
Per connection keep: socket, bytearray buffer, seat, alias.

The read path must follow protocol_blueprint.md §5.4 exactly:
 - data = sock.recv(4096); if not data -> CLIENT_DISCONNECTED (EOF), unregister, close
 - else buffer.extend(data); for msg in extract_frames(buffer): validate -> fsm.handle
 - ProtocolError -> send ERROR to that socket only, keep going
 - FrameTooLarge -> send ERROR MESSAGE_TOO_LARGE, then treat as disconnect
 - except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)
   -> log a warning, CLIENT_DISCONNECTED
Sending: wrap every send_msg in the same except tuple; a failure on one seat marks
that seat disconnected and must not stop delivery to the other seat.
Enable SO_REUSEADDR and SO_KEEPALIVE. Port 5457, host from argv (default 0.0.0.0).
Do not add any game rules here; all rules live in GameFSM.
```

### 3.5 Console client (`client/client.py`)

```text
Implement client/client.py, a console client for this protocol only. Resolve the
server host given in argv (default server.mills.edu) on port 5457. Send CONNECT
with the alias from argv. Use a background reader thread with the same buffer +
extract_frames loop, the same EOF check and the same exception handling as the server.
Render the table from the latest STATE_UPDATE only. Accept the user commands
"bet <n>", "hit", "stand", "quit" and translate them into BET / MOVE / DISCONNECT JSON
messages; reject anything else locally without sending. Show ERROR payloads to the
user. On GAME_OVER print the results and exit. On "quit" send DISCONNECT, then close().
```

## 4. Review Checklist for AI Output

- [ ] No message type, field, enum value, error code or state that is missing from the spec
- [ ] Buffer + `extract_frames` loop is used everywhere `recv()` is called
- [ ] `if not data:` EOF check is present in every receive loop
- [ ] `ConnectionResetError`, `ConnectionAbortedError`, `BrokenPipeError`, `TimeoutError` are caught on send and receive
- [ ] Invalid / out-of-turn input returns `ERROR` and never raises out of the loop
- [ ] Tests cover coalesced and fragmented streams from the blueprint's wire examples
- [ ] All three disconnect sources lead to the same `CLIENT_DISCONNECTED` path
