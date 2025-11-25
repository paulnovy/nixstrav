#!/usr/bin/env python3
import json
import time
import sqlite3
import logging
from datetime import datetime, timezone

import serial
import requests

CONFIG_PATH = "/opt/rfid-wejscie/config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class EventStore:
    def __init__(self, db_path: str, max_events: int = 10000):
        self.db_path = db_path
        self.max_events = max_events
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                  id   INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts   TEXT NOT NULL,
                  tag  TEXT NOT NULL,
                  sent INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_sent_id ON events(sent, id)"
            )
            conn.commit()
        finally:
            conn.close()

    def add_event(self, ts_iso: str, tag: str):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO events(ts, tag, sent) VALUES (?, ?, 0)",
                (ts_iso, tag),
            )
            conn.commit()
            self._enforce_limit(cur)
            conn.commit()
        finally:
            conn.close()

    def _enforce_limit(self, cur):
        cur.execute("SELECT COUNT(*) FROM events")
        cnt = cur.fetchone()[0]
        if cnt > self.max_events:
            to_delete = cnt - self.max_events
            logging.info("Trimming %d oldest events", to_delete)
            cur.execute(
                "DELETE FROM events WHERE id IN ("
                "  SELECT id FROM events ORDER BY id ASC LIMIT ?"
                ")",
                (to_delete,),
            )

    def get_unsent(self, limit: int):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, ts, tag FROM events "
                "WHERE sent = 0 ORDER BY id ASC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            return rows
        finally:
            conn.close()

    def mark_sent(self, ids):
        if not ids:
            return
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE events SET sent = 1 WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                ids,
            )
            conn.commit()
        finally:
            conn.close()


class InnodReader:
    """
    Parser ramek INNOD RU5109 na podstawie realnych sniffów:

    Przykładowa ramka:
      43 54 00 1C 01 45 01 C3 83 25 08 01 3E 2A 01 0F 01 01
      E2 80 11 91 A5 03 00 60 AC B8 76 76
      82 3A

    - Prefiks:                0x43 0x54
    - Bajt długości (len):    na pozycji 3 (0-indeks) → tutaj 0x1C = 28
    - Całkowita długość:      4 + len (prefiks + 2 bajty + len)
    - EPC (12 bajtów):        offset 18, długość 12 bajtów
                              → E2 80 11 91 A5 03 00 60 AC B8 76 76
    - Na końcu:               2 bajty CRC

    Ramki mogą występować „sklejone” jedna po drugiej w strumieniu.
    """

    PREFIX = b"\x43\x54"   # 'C', 'T'
    PREFIX_LEN = len(PREFIX)
    HEADER_LEN = 4         # 43 54 00 LEN
    LEN_OFFSET = 3         # bajt długości
    EPC_OFFSET = 18        # od początku ramki
    EPC_LEN = 12           # bajty EPC
    # Uwaga: offset jest od początku ramki, więc minimalna długość to EPC_OFFSET + EPC_LEN + CRC(2)
    MIN_FRAME_LEN = EPC_OFFSET + EPC_LEN + 2

    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.buffer = bytearray()

    def open(self):
        while True:
            try:
                logging.info("Opening serial port %s @ %d", self.port, self.baudrate)
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0,  # non-blocking
                )
                logging.info("Serial port opened")
                return
            except serial.SerialException as e:
                logging.error("Serial open failed: %s. Retrying in 5s", e)
                time.sleep(5)

    def _feed_buffer(self):
        if self.ser is None:
            self.open()
        try:
            data = self.ser.read(256)
            if data:
                self.buffer.extend(data)
        except serial.SerialException as e:
            logging.error("Serial error on read: %s. Reopening...", e)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def read_tag_nonblocking(self):
        self._feed_buffer()
        buf = self.buffer

        while True:
            idx = buf.find(self.PREFIX)
            if idx == -1:
                # Brak prefiksu – zostaw końcówkę, która może być początkiem kolejnej ramki
                if len(buf) > self.PREFIX_LEN - 1:
                    del buf[: len(buf) - (self.PREFIX_LEN - 1)]
                return None

            # Upewnij się, że mamy chociaż nagłówek (4 bajty: 43 54 00 LEN)
            if len(buf) - idx < self.HEADER_LEN:
                if idx > 0:
                    del buf[:idx]
                return None

            length = buf[idx + self.LEN_OFFSET]
            frame_len = self.HEADER_LEN + length

            # Jeżeli ramka jeszcze nie jest kompletna, czekamy na więcej danych
            if len(buf) - idx < frame_len:
                if idx > 0:
                    del buf[:idx]
                return None

            # Mamy pełną ramkę
            frame = bytes(buf[idx: idx + frame_len])
            del buf[: idx + frame_len]

            # Sanity check długości – realna ramka ma 32 bajty
            if frame_len < self.MIN_FRAME_LEN:
                logging.debug("Frame too short (%d): %s", frame_len, frame.hex().upper())
                continue

            epc_start = self.EPC_OFFSET
            epc_end = epc_start + self.EPC_LEN
            epc_bytes = frame[epc_start:epc_end]

            if len(epc_bytes) != self.EPC_LEN:
                logging.debug(
                    "Unexpected EPC length in frame (%d): %s",
                    len(epc_bytes),
                    frame.hex().upper(),
                )
                continue

            # UHF EPC Gen2 często startuje od 0xE2 – prosty filtr, żeby odsiać śmieci
#            if epc_bytes[0] != 0xE2:
#                logging.debug("Non-EPC frame (no 0xE2 start): %s", frame.hex().upper())
#                continue

            epc_hex = epc_bytes.hex().upper()
            logging.debug("FRAME: %s EPC:%s", frame.hex().upper(), epc_hex)
            return epc_hex


class Sender:
    def __init__(self, server_url: str, reader_id: str):
        self.server_url = server_url
        self.reader_id = reader_id

    def send_events(self, events):
        if not events:
            return False

        payload = {
            "reader_id": self.reader_id,
            "events": [
                {"id": e_id, "ts": ts, "tag": tag}
                for (e_id, ts, tag) in events
            ],
        }

        try:
            resp = requests.post(
                self.server_url,
                json=payload,
                timeout=3,
            )
            if 200 <= resp.status_code < 300:
                logging.info(
                    "Sent %d events, server status: %d",
                    len(events),
                    resp.status_code,
                )
                return True
            else:
                logging.error(
                    "Server returned status %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except requests.RequestException as e:
            logging.error("HTTP error: %s", e)
            return False


def main():
    cfg = load_config()
    store = EventStore(cfg["db_path"], max_events=10000)
    reader = InnodReader(cfg["serial_port"], cfg["baudrate"])
    sender = Sender(cfg["server_url"], cfg["reader_id"])

    last_send = 0
    send_interval = cfg.get("send_interval_sec", 2)
    batch_size = cfg.get("send_batch_size", 200)

    reader.open()

    while True:
        tag = reader.read_tag_nonblocking()
        if tag:
            ts_iso = datetime.now(timezone.utc).isoformat()
            logging.info("EPC: %s @ %s", tag, ts_iso)
            store.add_event(ts_iso, tag)

        now = time.time()
        if now - last_send >= send_interval:
            last_send = now
            events = store.get_unsent(batch_size)
            if events:
                ok = sender.send_events(events)
                if ok:
                    store.mark_sent([e[0] for e in events])

        time.sleep(0.02)


if __name__ == "__main__":
    main()
