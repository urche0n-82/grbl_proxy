Here's the UML state diagram for the ProxyCore state machine as it stands through Phase 3:


┌─────────────────────────────────────────────────────────────────────────────┐
│                         ProxyCore State Machine                             │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌───────────┐
                         ───▶ │DISCONNECTED│
                              └─────┬─────┘
                                    │ TCP client connects
                                    │ on_client_connected()
                                    ▼
                              ┌───────────┐
               ┌──────────── │PASSTHROUGH│ ◀──────────────────────────────┐
               │              └─────┬─────┘                               │
               │  ? query            │  start_marker received              │
               │  forward to serial  │  (G4 P0.0)                         │
               │                    ▼                                     │
               │              ┌───────────┐                               │
               │              │ BUFFERING │                               │
               │              └─────┬─────┘                               │
               │                    │                                     │
               │  ? query            │  Transitions out of BUFFERING:     │
               │  synthetic <Run|..>  │                                    │
               │                    │  • end_marker (G4 P0.0)            │
               │                    │  • M2 / M30 received               │
               │                    │  • idle timeout fires              │
               │                    │  ──────────────────────            │
               │                    │  !, ~, Ctrl-X received             │
               │                    │  ──────────────────────            │
               │                    │  TCP disconnects mid-buffer        │
               │                    │  (buffer discarded)                │
               │                    │            │                       │
               │                    │   ┌────────┘                       │
               │                    │   │ discard                        │
               │                    │   ▼                                │
               │                    │  PASSTHROUGH (discard path)        │
               │                    │                                     │
               │                    │ end/M2/M30/timeout (buffer kept)   │
               │                    ▼                                     │
               │              ┌───────────┐                               │
               │              │ EXECUTING │ ──── ? query ────────────────┤
               │              │           │      synthetic <Run|..>       │
               │              │           │                               │
               │              │  serial_  │  any line command             │
               │              │  readable │  → error:9                   │
               │              │  CLEARED  │                               │
               └──────────────│           │                               │
                              └──┬────┬───┘                               │
                                 │    │                                   │
              ! (feed hold)      │    │  Ctrl-X (soft reset)             │
                                 │    │  or streamer: error:N / ALARM:N  │
                                 ▼    │                                   │
                          ┌────────┐  │                                   │
                          │ PAUSED │  │         ┌───────┐                │
                          └───┬────┘  └────────▶│ ERROR │                │
                              │        serial_   └───┬───┘                │
                              │        readable       │                   │
                    ~ (cycle  │        RE-SET         │  $X or $H        │
                      resume) │                       │  forwarded       │
                              │                       │  to serial       │
                              └──────▶ EXECUTING      └──────────────────┘
                                       (back up)      serial_readable
                                                       RE-SET
And more precisely as a PlantUML state diagram:


@startuml grbl_proxy_states

skinparam state {
  BackgroundColor LightBlue
  BorderColor DarkBlue
  FontSize 13
}

[*] --> DISCONNECTED

DISCONNECTED --> PASSTHROUGH : TCP connect\non_client_connected()

PASSTHROUGH --> PASSTHROUGH : ? → forward to serial\nother lines → forward to serial
PASSTHROUGH --> DISCONNECTED : TCP disconnect\non_client_disconnected()
PASSTHROUGH --> BUFFERING : start_marker received\n(G4 P0.0)

BUFFERING --> BUFFERING : normal line → buffer it\nspoof ok to LightBurn\n? → synthetic <Run|...>
BUFFERING --> PASSTHROUGH : !, ~, Ctrl-X received\n(buffer discarded)\nor TCP disconnect\n(buffer discarded)
BUFFERING --> EXECUTING : end_marker / M2 / M30\nor idle timeout\n(buffer finalized)\nserial_readable CLEARED

EXECUTING --> EXECUTING : ? → synthetic <Run|...>\nother lines → error:9\n(disconnect-safe: TCP drop ignored)
EXECUTING --> PAUSED : ! (feed hold)\nforwarded to serial
EXECUTING --> ERROR : Ctrl-X (soft reset)\nor GRBL error:N\nor GRBL ALARM:N\nserial_readable RE-SET
EXECUTING --> PASSTHROUGH : all lines ack'd cleanly\n(streamer complete)\nserial_readable RE-SET

PAUSED --> PAUSED : ? → synthetic <Hold|...>\nother lines → error:9
PAUSED --> EXECUTING : ~ (cycle resume)\nforwarded to serial
PAUSED --> ERROR : Ctrl-X (soft reset)\nor GRBL ALARM:N\nserial_readable RE-SET

ERROR --> ERROR : all commands → error:9\n? → synthetic <Alarm|...>
ERROR --> PASSTHROUGH : $X (alarm clear)\nor $H (re-home)\nforwarded to serial\nserial_readable RE-SET

@enduml
Key architectural notes alongside the states:

State	serial_readable	Serial reads owned by	LightBurn sees
DISCONNECTED	SET	_serial_to_tcp	—
PASSTHROUGH	SET	_serial_to_tcp	Real GRBL responses
BUFFERING	SET	_serial_to_tcp	Spoofed ok / synthetic <Run|...>
EXECUTING	CLEARED	GrblStreamer	Synthetic <Run|...> for ? only
PAUSED	CLEARED	GrblStreamer	Synthetic <Hold|...> for ? only
ERROR	SET	_serial_to_tcp	Synthetic <Alarm|...> for ?, error:9 for all else
The serial_readable asyncio.Event is the key coordination mechanism — cleared when the streamer owns the serial read path, set otherwise. _serial_to_tcp blocks at serial_readable.wait() during EXECUTING and PAUSED to avoid competing with the streamer for the serial port.