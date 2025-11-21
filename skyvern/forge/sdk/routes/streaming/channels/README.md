# Channels

A "channel", as used within the streaming mechanism of our remote browsers,
is a WebSocket fit to some particular purpose.

There is/are:
  - a "VNC" channel that transmits NoVNC's RFB protocol data
  - a "Message" channel that transmits JSON between the frontend app and
    the api server
  - "CDP" channels that send messages to a remote browser using CDP protocol
    data
    - an "Execution" channel (one-off executions)
    - a soon-to-be "Exfiltration" channel (user event streaming)

In all cases, these are just WebSockets. They have been bucketed into "named channels"
to aid understanding.

These channels are described at the top of their respective files.

## Architecture

WARN: below is an AI-generated architecture document for all of the code beneath
the `skyvern/forge/sdk/routes/streaming` directory. It looks correct.

### High-Level Component Diagram

```
┌─────────────────┐
│  Frontend App   │
│   (Skyvern)     │
└────────┬────────┘
         │
         │ Two WebSocket Connections (paired via client_id)
         │
    ┌────┴────┬──────────────────────────────────────────┐
    │         │                                          │
    │    VNC Channel                            Message Channel
  http   (RFB Protocol)                         (JSON Messages)
    │         │                                          │
    │         │                                          │
┌───▼─────────▼──────────────────────────────────────────▼────┐
│                    API Server                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Registries (In-Memory State)              │ │
│  │  - vnc_channels: dict[client_id -> VncChannel]         │ │
│  │  - message_channels: dict[client_id -> MessageChannel] │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  VNC Channel Logic:           Message Channel Logic:        │
│  - RFB pass-through           - Copy/paste coordination     │
│  - Keyboard/mouse filtering   - Control handoff (agent/user)│
│  - Interactor control         - Clipboard management        │
│  - Copy/paste detection       - Channel coordination        │
│                                                             │
│  CDP Channels (created on-demand):                          │
│  - ExecutionChannel: JS evaluation (paste, get selected)    │
│  - ExfiltrationChannel: (future) user event streaming       │
│                                                             │
└────┬─────────────────────────────────────────────────────┬──┘
     │                                                     │
     │ WebSocket (RFB)                     Playwright (CDP)│
     │                                                     │
┌────▼─────────────────────────────────────────────────────▼──┐
│              Persistent Browser Session                     │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  noVNC Server                     Chrome/Chromium    │   │
│  │  (websockify)                                        │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Channel Pairing & Sticky Sessions

**Critical Design Constraint**: The VNC and Message channels for a given frontend
instance MUST connect to the same API server instance because they coordinate
via in-memory registries keyed by `client_id`.

```
Frontend Instance (client_id="abc123")
    │
    ├─→ VNC Channel ─────→ API Server Instance #2
    │                           ↓
    │                      vnc_channels["abc123"] = VncChannel
    │                           ↕ (coordinate via client_id)
    └─→ Message Channel ──→ API Server Instance #2
                               ↓
                          message_channels["abc123"] = MessageChannel
```

**Deployment Requirement**: Load balancer must use sticky sessions (e.g., cookie-based
or IP-based affinity) to ensure both WebSocket connections from the same client_id
reach the same backend instance.

### Channel Lifecycle & Verification

```
┌──────────────────────────────────────────────────────────────┐
│  Channel Creation (per browser_session/task/workflow_run)    │
└────────────────────┬─────────────────────────────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────────┐
    │  Initial Verification                  │
    │  - verify_browser_session()            │
    │  - verify_task()                       │
    │  - verify_workflow_run()               │
    │  Returns: entity + browser session     │
    └────────┬───────────────────────────────┘
             │
             ▼
    ┌────────────────────────────────────────┐
    │  Channel + Loops Created               │
    │  VncChannel/MessageChannel initialized │
    │  + Added to registry                   │
    └────────┬───────────────────────────────┘
             │
             ▼
    ┌────────────────────────────────────────┐
    │  Concurrent Loop Execution             │
    │  (via collect() - fail-fast)           │
    │                                        │
    │  Loop 1: Verification Loop             │
    │    - Polls every 5s                    │
    │    - Updates channel state             │
    │    - Exits if entity invalid           │
    │                                        │
    │  Loop 2: Data Streaming Loop           │
    │    - VNC: bidirectional RFB            │
    │    - Message: JSON messages            │
    │    - Exits on disconnect               │
    └────────┬───────────────────────────────┘
             │
             ▼
    ┌────────────────────────────────────────┐
    │  Channel Cleanup                       │
    │  - Close WebSocket                     │
    │  - Remove from registry                │
    │  - Clear channel state                 │
    └────────────────────────────────────────┘
```

### VNC Channel Data Flow

```
User Keyboard/Mouse Input
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│  Frontend (noVNC client)                                  │
│  Encodes input as RFB protocol bytes                      │
└────────┬──────────────────────────────────────────────────┘
         │ WebSocket (bytes)
         ▼
┌───────────────────────────────────────────────────────────┐
│  API Server: VncChannel.loop_stream_vnc()                 │
│  frontend_to_browser() coroutine                          │
│                                                           │
│  1. Receive RFB bytes from frontend                       │
│  2. Detect message type (keyboard=4, mouse=5)             │
│  3. Update key_state tracking                             │
│  4. Check for special key combinations:                   │
│     - Ctrl+C / Cmd+C → copy_text() via CDP                │
│     - Ctrl+V / Cmd+V → ask_for_clipboard() via Message    │
│     - Ctrl+O → BLOCK (forbidden)                          │
│  5. Check interactor mode:                                │
│     - If interactor=="agent" → BLOCK user input           │
│     - If interactor=="user" → PASS THROUGH                │
│  6. Block right-mouse-button (security)                   │
│  7. Forward to noVNC server                               │
└────────┬──────────────────────────────────────────────────┘
         │ WebSocket (bytes)
         ▼
┌───────────────────────────────────────────────────────────┐
│  Persistent Browser: noVNC Server (websockify)            │
│  Translates RFB → VNC protocol                            │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  Browser Display Update                                   │
└───────────────────────────────────────────────────────────┘

Screen Updates (reverse direction):
Browser → noVNC → VncChannel.browser_to_frontend() → Frontend
```

### Message Channel + CDP Execution Flow

```
User Pastes (Ctrl+V detected in VNC channel)
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│  VncChannel: ask_for_clipboard()                          │
│  Finds MessageChannel via registry[client_id]             │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  MessageChannel.ask_for_clipboard()                       │
│  Sends: {"kind": "ask-for-clipboard"}                     │
└────────┬──────────────────────────────────────────────────┘
         │ WebSocket (JSON)
         ▼
┌───────────────────────────────────────────────────────────┐
│  Frontend: User's clipboard content                       │
│  Responds: {"kind": "ask-for-clipboard-response",         │
│             "text": "clipboard content"}                  │
└────────┬──────────────────────────────────────────────────┘
         │ WebSocket (JSON)
         ▼
┌───────────────────────────────────────────────────────────┐
│  MessageChannel: handle_data()                            │
│  Receives clipboard text                                  │
│  Finds VncChannel via registry[client_id]                 │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  ExecutionChannel (CDP)                                   │
│  1. Connect to browser via Playwright                     │
│  2. Get browser context + page                            │
│  3. evaluate_js(paste_text_script, clipboard_text)        │
│  4. Close CDP connection                                  │
└────────┬──────────────────────────────────────────────────┘
         │ CDP over WebSocket
         ▼
┌───────────────────────────────────────────────────────────┐
│  Browser: Text pasted into active element                 │
└───────────────────────────────────────────────────────────┘

Similar flow for Copy (Ctrl+C):
VNC detects → ExecutionChannel.get_selected_text() →
MessageChannel sends {"kind": "copied-text", "text": "..."}
→ Frontend updates clipboard
```

### Control Flow: Agent ↔ User Interaction

NOTE: we don't really have an "agent" at this time. But any control of the
browser that is not user-originated is kinda' agent-like, by some
definition of "agent". Here, we do not have an "AI agent". Future work may
alter this state of affairs - and some "agent" could operate the browser
automatically.

```
┌───────────────────────────────────────────────────────────┐
│  Initial State: interactor = "agent"                      │
│  - User keyboard/mouse input is BLOCKED                   │
│  - Agent can control browser via CDP                      │
└────────┬──────────────────────────────────────────────────┘
         │
         │ User clicks "Take Control" in frontend
         ▼
┌───────────────────────────────────────────────────────────┐
│  Frontend → MessageChannel                                │
│  {"kind": "take-control"}                                 │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  MessageChannel.handle_data()                             │
│  vnc_channel.interactor = "user"                          │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  New State: interactor = "user"                           │
│  - User keyboard/mouse input is PASSED THROUGH            │
│  - Agent should pause automation                          │
└────────┬──────────────────────────────────────────────────┘
         │
         │ User clicks "Cede Control" in frontend
         ▼
┌───────────────────────────────────────────────────────────┐
│  Frontend → MessageChannel                                │
│  {"kind": "cede-control"}                                 │
└────────┬──────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────┐
│  MessageChannel.handle_data()                             │
│  vnc_channel.interactor = "agent"                         │
│  → Back to initial state                                  │
└───────────────────────────────────────────────────────────┘
```

### Error Propagation & Cleanup

The system uses `collect()` (fail-fast gather) for loop management:

```
Channel has 2 concurrent loops:
  - Verification loop (polls DB every 5s)
  - Streaming loop (handles WebSocket I/O)

collect() behavior:
  1. Waits for ANY loop to fail or complete
  2. Cancels all other loops
  3. Propagates the first exception

Cleanup (always executed via finally):
  - channel.close()
    - Sets browser_session/task/workflow_run = None
    - Closes WebSocket
    - Removes from registry
```

### Database Entity Relationships

```
Organization
    │
    ├─→ BrowserSession ────────┐
    │                          │
    ├─→ Task ──────────────────┤
    │   (has optional          │
    │    browser_session)      │
    │                          │
    └─→ WorkflowRun ───────────┤
        (has optional          │
         browser_session)      │
                               │
                               ▼
                    VncChannel + MessageChannel
                    (in-memory, paired by client_id)
```

### Key Design Patterns

1. **Channel Pairing**: Two WebSocket connections coordinated via in-memory registry
2. **Fail-Fast Loops**: `collect()` ensures any loop failure closes the entire channel
3. **Interactor Mode**: Binary state controlling whether user input is allowed
4. **On-Demand CDP**: ExecutionChannel creates temporary connections for each operation
5. **Polling Verification**: Every 5s, channels verify their backing entity still exists
6. **Pass-Through Proxy**: API server intercepts but doesn't transform RFB data
