# Application Protocol Blueprint — CS457 Blackjack

**Author:** Christopher Aidan Mills
**Course:** CS 457 — Computer Networks
**Sprint:** 1 — Application Protocol & Game State Machine Design

This document is the authoritative specification for every byte exchanged between the Blackjack server and its two clients. Server and client code (including any AI-generated code) must conform to it exactly. The server-side state machine that drives these messages is specified in [`fsm_specification.md`](fsm_specification.md).

---

## 1. Transport & Serialization

| Property | Value |
|---|---|
| Transport | TCP (IPv4), one persistent connection per client |
| Server port | `5457` |
| Server host | `server.mills.edu` (resolved via R2 authoritative DNS) |
| Serialization | JSON object, UTF-8 encoded |
| Framing | Newline-delimited JSON (one JSON object per line, terminated by `\n` = `0x0A`) |
| Max frame size | 65,536 bytes including the terminating `\n` |
| Players | Exactly 2 clients (`PLAYER_1`, `PLAYER_2`); the server is the dealer |

---

## 2. Framing Rule (Newline-Delimited JSON)

### 2.1 Rule

1. **Sender:** serialize the message with `json.dumps(msg, separators=(",", ":"))`, encode as UTF-8, append exactly one `\n` (`0x0A`), and send the whole frame with `sendall()`.
2. A serialized message **never contains a raw `0x0A` byte**. Compact `json.dumps` output puts no newlines between tokens, and any newline inside a string value is escaped as the two characters `\` `n`. Therefore `0x0A` on the wire **always** marks the end of a message, so there is no delimiter collision.
3. **Receiver:** keep one `bytearray` stream buffer **per socket**. Append every `recv()` chunk to it, then extract **every** complete frame (everything up to and including each `\n`) before calling `recv()` again. Bytes after the last `\n` stay in the buffer as a partial frame.
4. Empty lines (a frame that is only `\n`) are ignored.
5. If the buffer grows past 65,536 bytes without a `\n`, the server sends `ERROR` (`MESSAGE_TOO_LARGE`) and closes the connection. This stops a misbehaving client from growing the buffer without limit.
6. If a complete line is not valid UTF-8 JSON, or does not match the schema in §4, the server replies `ERROR` (`MALFORMED_MESSAGE`). The connection **stays open** and the FSM state does not change.

### 2.2 Why this handles TCP stream boundaries

TCP delivers a byte stream, not messages. One `recv()` may return:

| Case | What arrives in one `recv()` | Receiver behavior |
|---|---|---|
| Exact | One full frame | Extract 1 message, buffer empty |
| **Coalescing** | Two or more frames back-to-back | Loop extracts **all** complete frames |
| **Fragmentation** | Part of a frame | No `\n` yet, so keep the bytes and `recv()` again |
| Mixed | End of frame A + all of B + start of C | Extract A and B, keep the start of C in the buffer |
| EOF | `b""` (0 bytes) | Peer closed, see §5 (never parse, never loop) |

### 2.3 Raw wire stream examples

`↵` marks the `0x0A` byte. Each example is one continuous TCP byte stream.

**Client 1 → Server (connect, then bet):**
```text
{"msg_type":"CONNECT","player_id":"Alice","timestamp":1759600000,"payload":{}}↵{"msg_type":"BET","player_id":"Alice","timestamp":1759600012,"payload":{"amount":50}}↵
```

**Server → Client 1 (coalesced: three messages arrive in a single `recv()`):**
```text
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","timestamp":1759600000,"payload":{"message":"Waiting for Player 2","players_connected":1,"players_required":2}}↵{"msg_type":"GAME_START","player_id":"SERVER","timestamp":1759600005,"payload":{"seat":"PLAYER_1","players":{"PLAYER_1":"Alice","PLAYER_2":"Bob"},"starting_balance":500,"max_rounds":5,"min_bet":10,"max_bet":500}}↵{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1759600005,"payload":{"phase":"BETTING","round":1,"active_player":null, ...}}↵
```

**Client 2 → Server (fragmented: one `MOVE` split across two `recv()` calls):**
```text
recv() #1: {"msg_type":"MOVE","player_id":"Bob","times
recv() #2: tamp":1759600040,"payload":{"action":"HIT"}}↵
```
After `recv()` #1 there is no `↵`, so the buffer holds the 43 bytes and nothing is parsed. After `recv()` #2 the buffer contains a full line, which is extracted and parsed as a single `MOVE`.

### 2.4 Receiver extraction logic (reference)

```python
MAX_FRAME = 65536

def extract_frames(buffer: bytearray) -> list[dict | None]:
    """Remove every complete \n-terminated frame from buffer.
    Returns parsed dicts; None marks a malformed frame (caller sends ERROR)."""
    messages = []
    while True:
        idx = buffer.find(b"\n")
        if idx == -1:
            break                         # partial frame (or empty): wait for more bytes
        line = bytes(buffer[:idx])
        del buffer[:idx + 1]              # consume the frame AND its \n
        if not line.strip():
            continue                      # ignore empty lines
        try:
            messages.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            messages.append(None)
    if len(buffer) > MAX_FRAME:
        raise FrameTooLarge()             # caller sends ERROR MESSAGE_TOO_LARGE, then closes
    return messages

def send_msg(sock, msg: dict) -> None:
    frame = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(frame)
```

---

## 3. Common Message Envelope

Every message, in both directions, is a JSON object with exactly these four top-level keys:

| Key | Type | Required | Description |
|---|---|---|---|
| `msg_type` | string (enum) | yes | One of the 9 types in §4 (case-sensitive, uppercase) |
| `player_id` | string | yes | Client → Server: the sender's alias. Server → Client: always `"SERVER"` |
| `timestamp` | integer | yes | Unix epoch seconds when the sender created the message |
| `payload` | object | yes | Type-specific fields (§4). `{}` when the type has no fields |

**Shared field rules**

- **Alias (`player_id` from clients):** 1–16 characters, regex `^[A-Za-z0-9_]{1,16}$`. `"SERVER"` is reserved.
- **Seat:** `"PLAYER_1"` (first client to connect, acts first each round) or `"PLAYER_2"` (second client).
- **Card:** string `<rank><suit>`. Rank is `A`, `2`–`10`, `J`, `Q` or `K`. Suit is `S`, `H`, `D` or `C`. Examples: `"AS"`, `"10H"`, `"QD"`. A face-down card is `"??"`.
- **Money:** non-negative integers (whole dollars). No floats anywhere in the protocol.
- **Unknown keys** in a client message are ignored. **Missing required keys** or **wrong types** produce `MALFORMED_MESSAGE`.
- After `CONNECT`, the server identifies a client by **its socket**, not by `player_id`. A message whose `player_id` does not match the alias registered for that socket is rejected with `MALFORMED_MESSAGE`.

---

## 4. Message Types

| # | `msg_type` | Direction | Purpose |
|---|---|---|---|
| 1 | `CONNECT` | Client → Server | Join the table with an alias |
| 2 | `LOBBY_WAIT` | Server → Client | Tell Player 1 the server is waiting for Player 2 |
| 3 | `GAME_START` | Server → Clients | Game begins; assign each client its seat (role) and the table rules |
| 4 | `BET` | Client → Server | Place a wager for the current round |
| 5 | `MOVE` | Client → Server | Active player chooses `HIT` or `STAND` |
| 6 | `STATE_UPDATE` | Server → Clients | Broadcast the full table: hands, balances, bets, phase, whose turn |
| 7 | `ERROR` | Server → Client | Reject an invalid, out-of-turn, out-of-phase or malformed message |
| 8 | `DISCONNECT` | Client → Server | Intentional quit (forfeit if a game is in progress) |
| 9 | `GAME_OVER` | Server → Clients | Final outcome (Win / Loss / Draw / Forfeit) and final balances |

### 4.1 `CONNECT` — Client → Server

Sent once, immediately after the TCP connection is established.

| Field | Type | Constraints |
|---|---|---|
| `player_id` (envelope) | string | Alias, see §3 |
| `payload` | object | `{}` |

```json
{"msg_type":"CONNECT","player_id":"Alice","timestamp":1759600000,"payload":{}}
```

Errors: `NAME_TAKEN` (alias already seated), `LOBBY_FULL` (2 players already seated; the server then closes the socket), `MALFORMED_MESSAGE` (invalid alias).

### 4.2 `LOBBY_WAIT` — Server → Client

Sent to Player 1 after a valid `CONNECT` while the second seat is empty.

| Field | Type | Description |
|---|---|---|
| `message` | string | Human-readable status |
| `players_connected` | integer | Currently `1` |
| `players_required` | integer | Always `2` |

```json
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","timestamp":1759600000,"payload":{"message":"Waiting for Player 2","players_connected":1,"players_required":2}}
```

### 4.3 `GAME_START` — Server → Clients

Sent to **each** client individually when the second player connects. `seat` differs per recipient, and this is how roles are assigned.

| Field | Type | Description |
|---|---|---|
| `seat` | string enum | The **recipient's** role: `"PLAYER_1"` or `"PLAYER_2"` |
| `players` | object | `{"PLAYER_1": <alias>, "PLAYER_2": <alias>}` |
| `starting_balance` | integer | Starting bankroll for each player (default `500`) |
| `max_rounds` | integer | Number of rounds before the game ends (default `5`) |
| `min_bet` | integer | Minimum wager (default `10`) |
| `max_bet` | integer | Maximum wager (default `500`, also capped by current balance) |

```json
{"msg_type":"GAME_START","player_id":"SERVER","timestamp":1759600005,"payload":{"seat":"PLAYER_2","players":{"PLAYER_1":"Alice","PLAYER_2":"Bob"},"starting_balance":500,"max_rounds":5,"min_bet":10,"max_bet":500}}
```

### 4.4 `BET` — Client → Server

Valid only in the `BETTING` phase, once per player per round.

| Field | Type | Constraints |
|---|---|---|
| `amount` | integer | `min_bet ≤ amount ≤ min(max_bet, current balance)` |

```json
{"msg_type":"BET","player_id":"Alice","timestamp":1759600012,"payload":{"amount":50}}
```

Errors: `INVALID_BET` (not an integer or out of range), `INSUFFICIENT_FUNDS` (more than the balance), `WRONG_PHASE` (not betting, or already bet this round).

### 4.5 `MOVE` — Client → Server

Valid only in `PLAYER_TURN`, and only from the **active** player.

| Field | Type | Constraints |
|---|---|---|
| `action` | string enum | `"HIT"` or `"STAND"` |

```json
{"msg_type":"MOVE","player_id":"Alice","timestamp":1759600030,"payload":{"action":"HIT"}}
```

Errors: `NOT_YOUR_TURN` (sender is not `active_player`), `INVALID_ACTION` (action not in the enum), `WRONG_PHASE` (not in `PLAYER_TURN`).

### 4.6 `STATE_UPDATE` — Server → Clients

Broadcast to **both** clients after every accepted `BET` or `MOVE`, after the deal, after each dealer card, and at round end. Each one is a full snapshot (not a diff), so a client can always redraw its screen from the latest `STATE_UPDATE` alone.

| Field | Type | Description |
|---|---|---|
| `phase` | string enum | `"BETTING"`, `"PLAYER_TURN"`, `"DEALER_TURN"` or `"ROUND_END"` |
| `round` | integer | Current round, 1-based |
| `active_player` | string enum or `null` | `"PLAYER_1"` / `"PLAYER_2"` during `PLAYER_TURN`, otherwise `null` |
| `dealer` | object | `{"cards": [card], "value": integer or null}`. Before `DEALER_TURN` the hole card is `"??"` and `value` is `null` |
| `players` | object | Keyed by seat. Each value is a player object (below) |
| `last_event` | string | Human-readable description of what just happened |

**Player object**

| Field | Type | Description |
|---|---|---|
| `alias` | string | Player alias |
| `cards` | array of card | Current hand (empty during `BETTING`) |
| `value` | integer | Best hand value (Aces count 11 unless that busts) |
| `balance` | integer | Money not currently wagered |
| `bet` | integer | Wager this round (`0` if not placed yet) |
| `status` | string enum | `"BETTING"`, `"WAITING"`, `"PLAYING"`, `"STAND"`, `"BUST"` or `"BLACKJACK"` |
| `result` | string enum or `null` | `"WIN"`, `"LOSS"` or `"PUSH"`, only in `ROUND_END`, otherwise `null` |

```json
{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1759600031,"payload":{
  "phase":"PLAYER_TURN","round":1,"active_player":"PLAYER_1",
  "dealer":{"cards":["KS","??"],"value":null},
  "players":{
    "PLAYER_1":{"alias":"Alice","cards":["9H","5C","4D"],"value":18,"balance":450,"bet":50,"status":"PLAYING","result":null},
    "PLAYER_2":{"alias":"Bob","cards":["QH","7S"],"value":17,"balance":400,"bet":100,"status":"WAITING","result":null}
  },
  "last_event":"Alice hits: 4D"}}
```
*(Shown pretty-printed for readability. On the wire it is one compact line ending in `\n`.)*

### 4.7 `ERROR` — Server → Client

Sent **only to the offending client**. Other than `MESSAGE_TOO_LARGE` and `LOBBY_FULL`, an error never closes the connection or changes the FSM state.

| Field | Type | Description |
|---|---|---|
| `code` | string enum | See the table below |
| `message` | string | Human-readable explanation |
| `ref_msg_type` | string or `null` | `msg_type` of the rejected message, or `null` if it could not be parsed |

| `code` | Trigger |
|---|---|
| `MALFORMED_MESSAGE` | Not valid JSON/UTF-8, missing or mistyped keys, alias mismatch |
| `UNKNOWN_MSG_TYPE` | `msg_type` is not one of the client → server types |
| `WRONG_PHASE` | Message is valid but not allowed in the current FSM state |
| `NOT_YOUR_TURN` | `MOVE` from the non-active player |
| `INVALID_ACTION` | `MOVE.action` not `HIT`/`STAND` |
| `INVALID_BET` | Bet not an integer or outside `[min_bet, max_bet]` |
| `INSUFFICIENT_FUNDS` | Bet greater than the player's balance |
| `NAME_TAKEN` | Alias already in use |
| `LOBBY_FULL` | Two players already seated (connection then closed) |
| `MESSAGE_TOO_LARGE` | Frame exceeded 65,536 bytes (connection then closed) |

```json
{"msg_type":"ERROR","player_id":"SERVER","timestamp":1759600033,"payload":{"code":"NOT_YOUR_TURN","message":"It is PLAYER_1's turn","ref_msg_type":"MOVE"}}
```

### 4.8 `DISCONNECT` — Client → Server

Graceful, intentional quit. The client sends it, then closes its socket.

| Field | Type | Constraints |
|---|---|---|
| `reason` | string | Optional free text, ≤ 128 chars (may be `""`) |

```json
{"msg_type":"DISCONNECT","player_id":"Bob","timestamp":1759600100,"payload":{"reason":"user quit"}}
```

Effect: in the lobby, the seat is freed. During a game, the sender **forfeits** (see §5.3).

### 4.9 `GAME_OVER` — Server → Clients

Sent to every still-connected client when the game ends. The server then closes all game sockets (FSM `CLEANUP`).

| Field | Type | Description |
|---|---|---|
| `reason` | string enum | `"ROUNDS_COMPLETE"`, `"PLAYER_BROKE"` or `"FORFEIT"` |
| `rounds_played` | integer | Completed rounds |
| `forfeited_by` | string enum or `null` | Seat that disconnected or quit, if `reason` is `"FORFEIT"` |
| `results` | object | Keyed by seat: `{"alias", "final_balance", "net", "outcome"}` |

`outcome` is one of `"WIN"` (ended above `starting_balance`), `"LOSS"` (ended below it), `"DRAW"` (ended exactly at it), `"WIN_BY_FORFEIT"` (opponent left) or `"FORFEIT"` (this player left; any bet in play is lost). `net = final_balance − starting_balance`.

```json
{"msg_type":"GAME_OVER","player_id":"SERVER","timestamp":1759600101,"payload":{"reason":"FORFEIT","rounds_played":2,"forfeited_by":"PLAYER_2","results":{"PLAYER_1":{"alias":"Alice","final_balance":620,"net":120,"outcome":"WIN_BY_FORFEIT"},"PLAYER_2":{"alias":"Bob","final_balance":300,"net":-200,"outcome":"FORFEIT"}}}}
```

---

## 5. Connection Termination & Socket Lifecycle

### 5.1 Lifecycle summary

```text
Client: connect() → send CONNECT → [game messages] → (send DISCONNECT) → close()
Server: accept()  → per-client buffer → [dispatch to FSM] → on GAME_OVER/CLEANUP: close() every socket
```

### 5.2 Ways a connection can end, and how each is detected

| Termination | What happens on the wire | How the server detects it | Server action |
|---|---|---|---|
| **Application disconnect** | Client sends `DISCONNECT`, then `close()` → TCP FIN | `DISCONNECT` message parsed | Treat as a forfeit (§5.3), then close the socket |
| **Graceful TCP close** (client exits without `DISCONNECT`) | TCP FIN (4-way teardown) | `recv()` returns `b""` (**EOF**) | Same as `DISCONNECT` |
| **Abrupt drop** (`kill -9`, crash, power loss) | OS sends TCP RST, or nothing | `recv()`/`sendall()` raises `ConnectionResetError` / `ConnectionAbortedError` | Same as `DISCONNECT` |
| **Write to a closed peer** | Peer already gone | `sendall()` raises `BrokenPipeError` (EPIPE) | Same as `DISCONNECT` |
| **Silent network drop** (CML link severed, no FIN/RST) | Nothing arrives | TCP keepalive (`SO_KEEPALIVE`) or the next `sendall()` eventually fails with `TimeoutError` / `OSError` | Same as `DISCONNECT` |

Every row feeds **one** FSM event, `CLIENT_DISCONNECTED(seat)`, so the state machine has a single disconnect path no matter how the connection ended.

### 5.3 Forfeit rules (FSM `CLIENT_DISCONNECTED` handling)

- **In `WAITING_FOR_PLAYERS`:** the seat is freed and no `GAME_OVER` is sent (no game has started).
- **After `GAME_START`** (any of `BETTING`, `PLAYER_TURN`, `DEALER_TURN`, `ROUND_END`): the game ends immediately. The leaving player's current bet is lost, and the server sends `GAME_OVER` with `reason:"FORFEIT"` to the remaining player (`outcome:"WIN_BY_FORFEIT"`), then moves to `CLEANUP`.
- **If both players disconnect:** go straight to `CLEANUP` with no one to notify.

### 5.4 The TCP EOF (0-byte) rule

`recv()` returning `b""` is **not** an error and raises no exception. It is the POSIX signal that the peer closed its write half (FIN received). Every receive loop **must** check for it and stop reading. Otherwise `recv()` returns `b""` instantly forever, and the loop spins at 100% CPU.

```python
def client_reader(sock, seat, buffer):
    try:
        while True:
            data = sock.recv(4096)
            if not data:                                   # EOF: peer closed cleanly (FIN)
                log.info("%s closed connection (EOF)", seat)
                break
            buffer.extend(data)
            for msg in extract_frames(buffer):             # handles coalescing + fragmentation
                if msg is None:
                    send_error(sock, "MALFORMED_MESSAGE", None)
                else:
                    fsm.handle(seat, msg)                  # invalid moves -> ERROR, loop keeps running
    except FrameTooLarge:
        send_error(sock, "MESSAGE_TOO_LARGE", None)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError) as e:
        log.warning("%s connection lost abruptly: %s", seat, e)   # RST / drop
    finally:
        fsm.handle_event("CLIENT_DISCONNECTED", seat)      # single cleanup path
        sock.close()
```

**Sending** is wrapped the same way. A `BrokenPipeError` or `ConnectionResetError` raised while broadcasting `STATE_UPDATE` or `GAME_OVER` to one player marks that player disconnected. It never crashes the server or stops the broadcast to the other player.

### 5.5 Client-side handling

- If the server's socket returns EOF or raises a reset error, the client prints "Connection to server lost" and exits cleanly.
- When the user types `quit`, the client sends `DISCONNECT`, then calls `close()`.
- After receiving `GAME_OVER`, the client prints the results and closes its socket.
