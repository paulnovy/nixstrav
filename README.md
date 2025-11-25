# RFID IoT Perimeter Guard

End-to-end UHF RFID–based **IoT** system for monitoring critical exits in a care facility and triggering a **Satel Integra** alarm via a relay board.

Designed and implemented as a **production-grade prototype**: robust to power loss, different readers / multiple readers per edge, and network outages.

_Current version: **v0.1.0** (baseline perimeter-monitoring prototype)._

---

## 1. Overview

This project is an **RFID-driven IoT perimeter monitoring system**:

- Multiple **UHF RFID readers** (different vendors/protocols) installed at:
  - main gate,
  - main entrance,
  - internal “edges” (laundry, dining room, etc.).
- Small **Linux IoT edge nodes** next to each reader handle:
  - raw serial/Ethernet parsing,
  - local buffering in SQLite,
  - HTTP push to the central backend.
- A **central RFID server**:
  - receives events from all readers,
  - checks whitelists (known tags),
  - applies schedules and deduplication logic,
  - drives a USB→RS-232 relay board,
  - exposes clean HTTP/JSON API for integrations (n8n, Telegram, Home Assistant, etc.).
- The relay board is wired into a **Satel Integra + INT-E** input to:
  - trigger alarm sirens,
  - integrate with the existing intrusion/technical alarm infrastructure,
  - act as a hardware trigger for **push and SMS notifications** 

Primary use case: **exit monitoring for residents with dementia** in a care facility  
(but architecture is generic enough for warehouses, logistics yards, access control, etc.).

---

## 2. High-Level Architecture

    [ RFID TAG ]
        ↓ (UHF)
    [ RFID READER ]
        ↓ RS-232 / USB / Ethernet
    [ EDGE IOT NODE (Linux box) ]
        - vendor-specific frame parsing
        - local SQLite event store
        - HTTP/JSON client

        ↓ HTTP / JSON
    [ CENTRAL RFID SERVER ]
        - Flask API
        - events DB (SQLite)
        - tag whitelist (known_tags.json)
        - dedup & schedules
        - relay abstraction

        ↓ USB / RS-232
    [ 4-CHANNEL RELAY BOARD ]
        ↓ dry contact (NC/NO)
    [ SATEL INTEGRA + INT-E ]
        ↓
    [ SIREN / BMS / NOTIFICATIONS ]

**Key concepts:**

- IoT **edge nodes** do all the dirty work with proprietary reader protocols.
- Central server is simple, explicit, and auditable: **pure HTTP + SQLite + JSON**.
- Alarm integration strictly via **dry contacts**; no hacks on the alarm bus.

---

## 3. Components

### 3.1 Central RFID Server

Running on a dedicated Linux host (e.g. `marianserwer`).

#### API layer

- Python + Flask app exposing `POST /api/tags`.
- Each edge node sends batches:

    {
      "reader_id": "cf-ru5112-brama-1",
      "events": [
        { "id": 123, "ts": "...UTC...", "tag": "E28011..." },
        ...
      ]
    }

- API validates payload, normalizes timestamps and stores events.

#### Persistence

- SQLite DB (`events.db`).
- Table `events` roughly:
  - `id` (PK, autoincrement),
  - `reader_id`,
  - `received_at` (server timestamp),
  - `tag` (canonical hex string),
  - `reason` (`ok`, `duplicate`, `outside_schedule`, `unknown_tag`, `relay_error`),
  - `fired` (0/1 – whether alarm was actually triggered).
- Simple retention policy: keep up to **N** events, delete oldest.

#### Whitelisting

- `known_tags.json` maps tag → metadata:

    "E2801191A5030060ACB87676": {
      "owner": "TAG-1",
      "note": "first test tag"
    }

- If tag not present → `reason = "unknown_tag"`, no alarm trigger.

#### Scheduling

- Per-reader schedule in `config.json`:
  - `mode: "always"` – reader is always armed (e.g. gate).
  - `mode: "window"` with `start_hour` / `end_hour` – armed only at night (e.g. 21:00–06:00 for internal doors).
- Outside schedule → `reason = "outside_schedule"`.

#### Deduplication & stale-event handling

- Dedup window (e.g. **10 seconds** per reader+tag) to avoid a storm of alarms when a tag sits in the beam.
  - Within window → `reason = "duplicate"`.
- `ignore_late_sec` (e.g. **300 s**) to drop stale buffered events after link recovery.
  - Prevents replaying a night’s worth of events with siren at once.

#### Relay driver

Configured in `config.json`:

- serial port (by-id path),
- baudrate, timeout,
- mapping `reader_id → relay channel (1–4)`.

Behaviour:

- On **valid & in-window first event**:
  - open relay serial port if needed,
  - fire momentary pulse on mapped channel,
  - mark event as `reason="ok"`, `fired=1`.
- On failure to open or talk to relay:
  - `reason="relay_error"`, `fired=0`,
  - tag event still logged for forensic analysis.

#### Service / Ops

- Runs as dedicated Linux user (e.g. `rfid`) via `systemd` service (`rfid-server.service`).
- Automatically starts on boot, restarts on crash.
- Logs via `journalctl` with INFO/ERROR lines for:
  - incoming HTTP traffic,
  - relay operations,
  - permission issues on `/dev/serial/...`.

---

### 3.2 Edge IoT Node – INNOD Reader (Entrance)

Device near entrance (e.g. `wejscie`) connected to an **INNOD RU5109** reader.

#### Serial protocol parsing

- Proprietary frames starting with `0x43 0x54` (`"CT"`).
- Length byte at fixed offset; **EPC (12 bytes)** at fixed offset.
- Handles concatenated frames in stream and partial reads.
- Initially filtered only tags starting with `0xE2` (classic EPC), later relaxed to support **decimal-encoded tags** as well.

#### Local event store

- SQLite DB (`events.db`) on edge node.
- Table: `id`, `ts` (edge timestamp, UTC ISO), `tag`, `sent`.
- Bounded size with trimming of oldest rows.

#### HTTP client

Periodically:

- fetch unsent events (up to N),
- POST to central `/api/tags`,
- on success → mark `sent=1`.

If server unreachable, events accumulate locally and are pushed when connectivity returns.

#### Resilience

- Non-blocking serial reads with automatic reopen on errors.
- `systemd` service to survive reboots and intermittent power.

---

### 3.3 Edge IoT Node – Chafon CF-RU5112 (Gate)

Device at the gate (e.g. `marianbrama`) connected to **Chafon CF-RU5112** via RS-232.

#### Raw frame sniffing

- Reads 256-byte chunks from `/dev/ttyS0` at 115200 baud.
- Accumulates in a buffer, extracts EPCs using pattern matching.

#### Dual tag format handling

Supports:

- Normal EPC tags: `E28011...` style.
- Decimal-encoded tags (zeros + short numeric: `000000000000000000003773`),  
  to keep multiple tag batches consistent across readers.

Ensures that **the same physical tag** is represented as the **same canonical string** across gate and entrance.

#### Batching & POST

- Maintains in-memory list of pending events with simple incremental `id`.
- Sends batch to central server when:
  - time since last send ≥ defined interval, or
  - batch size ≥ defined max.
- On HTTP error, keeps pending list to retry later.

#### Error recovery

- Serial exceptions cause controlled close/reopen with delay.
- No data loss at the gate side; worst case is temporary non-delivery.

---

### 3.4 Edge IoT Node – Dual Chafon CF661 (Dining Room)

- Two **Chafon CF661** readers used to cover both dining room doors.
- IoT node bridges them to the central server using:
  - local parsing and batching logic similar to other edge nodes,
  - **MT7601U Wi-Fi dongle**, because wired connection to the main server rack is not possible.
- From the server’s point of view:
  - each CF661 door is a separate `reader_id`,
  - they share the same edge node and network uplink.

---

### 3.5 Alarm & Relay Integration (Satel Integra + INT-E)

Hardware chain:

- USB-serial dongle on central server (`/dev/serial/by-id/...`) →
- 4-channel Eletechsup relay board →
- INT-E expansion module of Satel Integra →
- Alarm input type NC/NO configured in Integra →
- Siren pattern logic configured in alarm panel.

Design choices:

- Use **dry contacts only**, no non-standard integration with the alarm bus.
- Mapping readers to relay channels (example):
  - `cf661-pralnia` → CH1
  - `cf661-jadalnia-1`, `cf661-jadalnia-2` → CH2
  - `innod-wejscie-1` → CH3
  - `cf-ru5112-brama-1` → CH4
- Relay pulses are **short momentary closures** (tunable in code and Integra input settings).

We also tested:

- NC/NO logic on INT-E,
- minimum pulse width vs detection latency.

---

## 4. Data Model & Event Semantics

### 4.1 Event lifecycle

For each tag read:

1. **Edge node**:
   - raw frame → parsed EPC/ID → local event row.
2. **Central server**:
   - normalizes and stores event with `reason` + `fired`.

**Classification logic:**

- `unknown_tag` – not found in `known_tags.json`.
- `outside_schedule` – tag known, but reader currently disarmed by time window.
- `duplicate` – within dedup window for same reader+tag.
- `ok` – valid first event, within schedule, relay fired successfully.
- `relay_error` – valid first event, but relay board failed or port unavailable.

### 4.2 Tag metadata

- `known_tags.json` is a simple JSON mapping:  
  `tag string → { owner, note }`.

- For now tags are named generically (`TAG-1`, `TAG-2`, …) to keep code independent from personal data.

In a final deployment, this would be extended with:

- resident ID,
- photo/room mapping,
- risk category, etc.

---

## 5. IoT / System Design Highlights

### Edge

- All hardware-specific complexity lives on **IoT edge nodes**.
- Central server is a clean **HTTP/JSON API** with minimal dependencies.

### Offline tolerance

- Edge nodes keep logging to SQLite when the central server is down.
- Central dedup + `ignore_late_sec` prevent **alarm storms** after a network outage.

### Multi-vendor reader support

Implemented custom parsers for:

- INNOD RU5109 (`"CT"` header with EPC offset),
- Chafon CF-RU5112 (simple raw pattern matching),
- 2 × Chafon CF661.

Architecture is ready for mass extension:  
a new reader type = a new small edge client.

### Industrial integration

- Clean handoff to an existing **Satel Integra** security system.
- Relay board is powered and wired from the alarm infrastructure,  
  with INT-E input supervision and proper NC/NO logic.

### Observability

- Full event history in SQL.
- Simple CLI tools:
  - `live_all.py`, `live_wejscie.py` for live tailing of events by reader.
- Server logs every HTTP event batch and every relay activation.

---

## 6. Example Use Cases

### Elder-care / dementia ward

- Residents wear RFID wristbands.
- Critical doors/gates are monitored.
- At night, any tagged exit attempt:
  - is logged with reader + timestamp,
  - fires siren through Satel Integra,
  - can be correlated after the fact,
  - can trigger push/SMS notifications via automation (n8n, Telegram, SMS gateway, etc.).

### Perimeter security for facilities

- Track movement of specific assets (vehicles, containers).
- Trigger alarms only for high-value or high-risk tags.

### Industrial / logistics IoT

- Use the same architecture to feed events into **MES/WMS** systems instead of (or in addition to) an alarm panel.

---

## 7. Implementation Notes (Tech Stack)

- **Language:** Python 3

- **Core libraries:**
  - `Flask` – HTTP API server,
  - `requests` – HTTP client on edge nodes,
  - `pyserial` – serial communication with readers and relay board,
  - `sqlite3` – lightweight embedded database.

- **OS / Runtime:**
  - Linux (Ubuntu / Debian) on all nodes,
  - `systemd` services for:
    - central server (`rfid-server.service`),
    - each edge client (per reader type).

- **Topology:**
  - VLAN-friendly IP layout; readers talk over isolated network to central server.
  - USB-serial devices referenced via `/dev/serial/by-id/...` (stable names).

---

## 8. Roadmap / Possible Extensions

### Web dashboard

- Live map of readers, active alarms, tag history.
- Simple UI to manage `known_tags.json` (add/remove tags, assign resident names, rooms).

### Alternative notification channels

- n8n workflows:
  - Telegram node notifications (photo snapshot + reader name + timestamp),
  - email alerts for specific tags or readers.
- Integration with **Home Assistant** / other IoT hubs.

### Additional hardware

- More reader types (BLE, LoRa, NFC).
- Redundant relay boards and alarm inputs.

### Analytics

- Heatmaps of movement over time.
- Anomaly detection (e.g. unusually frequent door approaches by the same tag).

---

## 9. Status

The system is:

- Fully wired and tested end-to-end:
  - multiple UHF readers,
  - edge IoT nodes,
  - central server,
  - relay board,
  - Satel Integra alarm.

- Robust to:
  - power loss – UPS at edge nodes / main server,
  - power cycles (BIOS “power on after AC restore” on edge nodes + `systemd`),
  - USB permission issues (service user + correct udev/group configuration),
  - reader protocol quirks (EPC vs decimal-encoded tags).

---

## 10. Versioning & Releases

This repository uses a **simple semantic versioning** scheme:

- `MAJOR.MINOR.PATCH` (for example: `v0.1.0`, `v0.2.0`, `v1.0.0`).
- While the project is still evolving rapidly, we stay in `0.x` (breaking changes are allowed between minor versions).

### Current plan

- **v0.1.0**
  - Baseline perimeter monitoring:
    - multi-reader IoT edge nodes,
    - central server, dedup, schedules,
    - Satel Integra / relay integration,
    - CLI tools (`live_all.py`, `live_wejscie.py`).

- **v0.2.0** (planned)
  - n8n-based automation:
    - Telegram notifications with camera picture capture,
    - SMS / push notifications for specific tags/readers,
    - optional integration with Home Assistant / other IoT platforms.
    - AI-powered system healthcheck, anomaly detection.

Later versions can introduce:

- Web dashboard,
- extended analytics,
- new hardware types (LoRa, BLE, etc.).

