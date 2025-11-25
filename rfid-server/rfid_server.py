#!/usr/bin/env python3
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import serial
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------
# Konfiguracja podstawowa
# ---------------------------------------------------------------------

CONFIG_PATH = "/opt/rfid-server/config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_known_tags(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        logging.warning("known_tags_file not set in config – lista znanych tagów pusta")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logging.info("Loaded %d known tags from %s", len(data), path)
            return data
        logging.warning(
            "known_tags_file %s nie jest obiektem JSON (dict), używam pustej listy",
            path,
        )
        return {}
    except FileNotFoundError:
        logging.warning("known_tags_file %s nie istnieje – lista znanych tagów pusta", path)
        return {}
    except Exception as e:
        logging.error("Błąd ładowania known_tags_file %s: %s", path, e)
        return {}


# Wczytanie configu na starcie
CONFIG: Dict[str, Any] = load_config()

# Poziom logowania z configu (opcjonalnie)
log_cfg = CONFIG.get("logging", {})
log_level_name = str(log_cfg.get("level", "INFO")).upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.getLogger().setLevel(log_level)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(CONFIG.get("db_path", str(BASE_DIR / "events.db")))

# Limit retencji (łączna liczba wierszy w tabeli events).
# Można nadpisać w config.json kluczem "max_events".
MAX_EVENTS: int = int(CONFIG.get("max_events", 1_000_000))

# Dedup/late
dedup_cfg = CONFIG.get("dedup", {})
DEDUP_WINDOW_SEC: int = int(dedup_cfg.get("window_sec", 10))
IGNORE_LATE_SEC: int = int(dedup_cfg.get("ignore_late_sec", 300))

# Schedules
READER_SCHEDULES: Dict[str, Any] = CONFIG.get("reader_schedules", {})

# Known tags
KNOWN_TAGS: Dict[str, Any] = load_known_tags(CONFIG.get("known_tags_file"))

# ---------------------------------------------------------------------
# Sterowanie przekaźnikiem RS-232 Eletechsup (4CH)
# ---------------------------------------------------------------------


class RelayBoard:
    """
    Sterowanie płytką Eletechsup 4CH po RS-232.

    Komendy (hex) – tryb "momentary" (200 ms wg dokumentacji):

    Channel 1 : 55 56 00 00 00 01 04 B0
    Channel 2 : 55 56 00 00 00 02 04 B1
    Channel 3 : 55 56 00 00 00 03 04 B2
    Channel 4 : 55 56 00 00 00 04 04 B3
    """

    CMD_MOMENTARY = {
        1: bytes.fromhex("55 56 00 00 00 01 04 B0"),
        2: bytes.fromhex("55 56 00 00 00 02 04 B1"),
        3: bytes.fromhex("55 56 00 00 00 03 04 B2"),
        4: bytes.fromhex("55 56 00 00 00 04 04 B3"),
    }

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None

    def _ensure_open(self) -> bool:
        if self.ser and self.ser.is_open:
            return True
        try:
            logging.info(
                "Opening relay serial port %s @ %d", self.port, self.baudrate
            )
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
            logging.info("Relay serial port opened")
            return True
        except serial.SerialException as e:
            logging.error("Nie mogę otworzyć portu przekaźnika %s: %s", self.port, e)
            self.ser = None
            return False

    def fire_momentary(self, channel: int) -> bool:
        cmd = self.CMD_MOMENTARY.get(channel)
        if cmd is None:
            logging.error("Nieznany kanał przekaźnika: %s", channel)
            return False

        if not self._ensure_open():
            return False

        try:
            self.ser.write(cmd)
            self.ser.flush()
            # Opcjonalnie czytamy odpowiedź (nie blokujmy za długo)
            time.sleep(0.05)
            try:
                _ = self.ser.read(8)
            except Exception:
                pass
            logging.info("Relay momentary fired on channel %d", channel)
            return True
        except serial.SerialException as e:
            logging.error("Błąd zapisu do przekaźnika: %s", e)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            return False


relay_cfg = CONFIG.get("relay", {})
RELAY_ENABLED: bool = bool(relay_cfg.get("enabled", True))
RELAY_MAPPING: Dict[str, int] = relay_cfg.get("mapping", {})
RELAY_BOARD: Optional[RelayBoard] = None

if RELAY_ENABLED:
    relay_port = relay_cfg.get("port", "/dev/ttyS0")
    relay_baud = int(relay_cfg.get("baudrate", 9600))
    relay_timeout = float(relay_cfg.get("timeout_sec", 0.2))
    RELAY_BOARD = RelayBoard(port=relay_port, baudrate=relay_baud, timeout=relay_timeout)
    logging.info(
        "Relay board enabled on %s @ %d, mapping: %s",
        relay_port,
        relay_baud,
        RELAY_MAPPING,
    )
else:
    logging.info("Relay board DISABLED in config")

# ---------------------------------------------------------------------
# Baza danych
# ---------------------------------------------------------------------


def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              reader_id    TEXT NOT NULL,
              tag          TEXT NOT NULL,
              ts_client    TEXT NOT NULL,
              received_at  TEXT NOT NULL,
              source_ip    TEXT NOT NULL,
              fired        INTEGER NOT NULL DEFAULT 0,
              reason       TEXT NOT NULL,
              edge_event_id INTEGER
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_reader_tag_ts "
            "ON events(reader_id, tag, ts_client)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_reader_tag_recv "
            "ON events(reader_id, tag, received_at)"
        )
        conn.commit()
        logging.info("DB ready at %s", DB_PATH)
    finally:
        conn.close()


def enforce_retention(conn: sqlite3.Connection) -> None:
    """
    Utrzymuje w tabeli events maksymalnie MAX_EVENTS rekordów,
    kasując najstarsze (po id). Timestampy zostają, nic nie obcinamy.
    """
    if MAX_EVENTS <= 0:
        return

    cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM events")
    row = cur.fetchone()
    if not row or row[0] is None:
        return

    max_id = int(row[0])
    min_id_to_keep = max_id - MAX_EVENTS + 1
    if min_id_to_keep <= 1:
        # Mamy mniej niż MAX_EVENTS rekordów – nic nie robimy.
        return

    cur.execute("DELETE FROM events WHERE id < ?", (min_id_to_keep,))
    deleted = cur.rowcount or 0
    if deleted > 0:
        logging.info(
            "Retention: deleted %d oldest events (id < %d) to keep ~%d rows",
            deleted,
            min_id_to_keep,
            MAX_EVENTS,
        )
        conn.commit()


# ---------------------------------------------------------------------
# Logika pomocnicza
# ---------------------------------------------------------------------


def parse_ts_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # klient używa datetime.now(timezone.utc).isoformat()
        return datetime.fromisoformat(ts)
    except Exception:
        logging.warning("Nie mogę sparsować ts_client: %r", ts)
        return None


def is_late(ts_client: Optional[datetime], received_at: datetime) -> Tuple[bool, str]:
    if IGNORE_LATE_SEC <= 0 or ts_client is None:
        return False, ""
    delta = (received_at - ts_client).total_seconds()
    if delta > IGNORE_LATE_SEC:
        return True, "too_late"
    return False, ""


def is_duplicate(
    cur: sqlite3.Cursor,
    reader_id: str,
    tag: str,
    received_at: datetime,
) -> bool:
    if DEDUP_WINDOW_SEC <= 0:
        return False

    cur.execute(
        """
        SELECT received_at
        FROM events
        WHERE reader_id = ? AND tag = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (reader_id, tag),
    )
    row = cur.fetchone()
    if not row:
        return False

    try:
        prev = datetime.fromisoformat(row[0])
    except Exception:
        return False

    delta = (received_at - prev).total_seconds()
    if abs(delta) <= DEDUP_WINDOW_SEC:
        return True
    return False


def is_reader_armed(reader_id: str, now_utc: datetime) -> bool:
    """
    Sprawdza, czy dany reader jest "uzbrojony" wg configu:
    - mode = "always"  -> zawsze
    - mode = "never"   -> nigdy
    - mode = "window"  -> przedział godzinowy [start_hour, end_hour)
                         w czasie lokalnym serwera; jeśli start > end,
                         to okno nocne (np. 21-6).
    """
    cfg = READER_SCHEDULES.get(reader_id)
    if not cfg:
        # brak wpisu -> domyślnie zawsze
        return True

    mode = str(cfg.get("mode", "always")).lower()
    if mode == "always":
        return True
    if mode == "never":
        return False

    if mode == "window":
        local = now_utc.astimezone()  # używa strefy systemowej (timedatectl)
        hour = local.hour
        start = int(cfg.get("start_hour", 0))
        end = int(cfg.get("end_hour", 0))

        if start == end:
            # bezpiecznie: traktuj jako 24/7
            return True

        if start < end:
            # zwykłe okno, np. 8-16
            return start <= hour < end
        else:
            # okno "przez północ", np. 21-6
            return hour >= start or hour < end

    # nieznany mode -> domyślnie zawsze
    return True


def trigger_relay(reader_id: str) -> Tuple[bool, str]:
    if not RELAY_ENABLED or RELAY_BOARD is None:
        return False, "relay_disabled"

    channel = RELAY_MAPPING.get(reader_id)
    if not channel:
        return False, "no_channel_for_reader"

    ok = RELAY_BOARD.fire_momentary(channel)
    if ok:
        return True, "ok"
    return False, "relay_error"


# ---------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------

app = Flask(__name__)


@app.route("/api/health", methods=["GET"])
def health() -> Any:
    return jsonify({"status": "ok"})


@app.route("/api/events", methods=["GET"])
def list_events() -> Any:
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100

    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, reader_id, tag, ts_client, received_at,
                   source_ip, fired, reason, edge_event_id
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    result: List[Dict[str, Any]] = []
    for r in rows:
        result.append(
            {
                "id": r[0],
                "reader_id": r[1],
                "tag": r[2],
                "ts_client": r[3],
                "received_at": r[4],
                "source_ip": r[5],
                "fired": bool(r[6]),
                "reason": r[7],
                "edge_event_id": r[8],
            }
        )

    return jsonify(result)


@app.route("/api/tags", methods=["POST"])
def ingest_tags() -> Any:
    received_at = datetime.now(timezone.utc)
    received_at_iso = received_at.isoformat()
    source_ip = request.remote_addr or "unknown"

    try:
        payload = request.get_json(force=True)
    except Exception:
        logging.warning("Bad JSON in /api/tags from %s", source_ip)
        return jsonify({"status": "error", "error": "invalid_json"}), 400

    reader_id = payload.get("reader_id")
    events = payload.get("events")

    if not isinstance(reader_id, str) or not isinstance(events, list):
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "missing_reader_or_events",
                }
            ),
            400,
        )

    conn = get_db_conn()
    results: List[Dict[str, Any]] = []
    try:
        cur = conn.cursor()

        for ev in events:
            edge_event_id = ev.get("id")
            ts_client_str = ev.get("ts")
            tag_raw = ev.get("tag")

            if tag_raw is None:
                continue

            tag = str(tag_raw).strip().upper()
            ts_client_dt = parse_ts_iso(ts_client_str)

            reason = ""
            fired_flag = 0

            # 1) filtr po znanych tagach
            if tag not in KNOWN_TAGS:
                reason = "unknown_tag"
            else:
                # 2) okno czasowe readera
                if not is_reader_armed(reader_id, received_at):
                    reason = "outside_schedule"
                else:
                    # 3) spóźnione eventy
                    late, late_reason = is_late(ts_client_dt, received_at)
                    if late:
                        reason = late_reason
                    else:
                        # 4) deduplikacja
                        if is_duplicate(cur, reader_id, tag, received_at):
                            reason = "duplicate"
                        else:
                            # 5) przekaźnik
                            ok, relay_reason = trigger_relay(reader_id)
                            if ok:
                                fired_flag = 1
                                reason = "ok"
                            else:
                                reason = relay_reason

            cur.execute(
                """
                INSERT INTO events (
                    reader_id, tag, ts_client,
                    received_at, source_ip,
                    fired, reason, edge_event_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reader_id,
                    tag,
                    ts_client_str or "",
                    received_at_iso,
                    source_ip,
                    fired_flag,
                    reason,
                    edge_event_id,
                ),
            )
            db_id = cur.lastrowid

            results.append(
                {
                    "db_id": db_id,
                    "edge_event_id": edge_event_id,
                    "tag": tag,
                    "fired": bool(fired_flag),
                    "reason": reason,
                }
            )

        conn.commit()
        # Po każdym batchu egzekwujemy retencję.
        enforce_retention(conn)
    finally:
        conn.close()

    return jsonify({"status": "ok", "count": len(results), "results": results})


# ---------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------

if __name__ == "__main__":
    logging.info("Starting RFID central API server...")
    init_db()

    listen_host = CONFIG.get("listen_host", "0.0.0.0")
    listen_port = int(CONFIG.get("listen_port", 5000))

    logging.info("Listening on %s:%d", listen_host, listen_port)
    # Flask dev server – do środka LAN wystarczy
    app.run(host=listen_host, port=listen_port)
