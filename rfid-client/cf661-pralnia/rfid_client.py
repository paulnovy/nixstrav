#!/usr/bin/env python3
import json
import time
import sqlite3
import logging
from datetime import datetime, timezone

import serial
import requests

CONFIG_PATH = "/opt/rfid-client/config.json"

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


class CF661Reader:
    """
    Parser ramek CF661 zgodnie z protokołem:
    CF 00 00 01 12 00 XX YY 01 00 LL [LL bajtów EPC] [CRC_H] [CRC_L]

    - Szukamy prefiksu CF 00 00 01 12 (5 bajtów),
    - Czekamy aż w buforze będzie co najmniej 11 bajtów nagłówka,
    - Bajt 10 = długość EPC (len),
    - Pełna ramka ma: 11 (header) + len + 2 (CRC) bajtów,
    - EPC = bajty 11 .. 10+len.
    """

    PREFIX = b"\xCF\x00\x00\x01\x12"
    PREFIX_LEN = 5
    MIN_HEADER_LEN = 11  # do bajtu length włącznie

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
                # brak prefiksu – zostaw tylko tyle, żeby ewentualny prefiks zmieścił się na końcu
                if len(buf) > self.PREFIX_LEN - 1:
                    del buf[: len(buf) - (self.PREFIX_LEN - 1)]
                return None

            # Mamy początek prefiksu, ale jeszcze niekoniecznie cały nagłówek (11 bajtów)
            if len(buf) - idx < self.MIN_HEADER_LEN:
                # poczekaj na więcej danych – ale śmieci przed prefiksem można wyrzucić
                if idx > 0:
                    del buf[:idx]
                return None

            # Bajt length (liczba bajtów EPC)
            length = buf[idx + 10]
            frame_len = self.MIN_HEADER_LEN + length + 2  # header(11) + EPC + CRC(2)

            if len(buf) - idx < frame_len:
                # pełnej ramki jeszcze nie ma
                if idx > 0:
                    del buf[:idx]
                return None

            # Mamy pełną ramkę
            frame = bytes(buf[idx : idx + frame_len])
            del buf[: idx + frame_len]

            # EPC = bajty 11 .. 10+length
            epc_bytes = frame[11 : 11 + length]
            epc_hex = epc_bytes.hex().upper()

            # Debug: pełna ramka
            logging.debug("FRAME: %s EPC:%s", frame.hex().upper(), epc_hex)

            # Zwracamy EPC (standardowy identyfikator taga)
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
                timeout=3
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
    reader = CF661Reader(cfg["serial_port"], cfg["baudrate"])
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
