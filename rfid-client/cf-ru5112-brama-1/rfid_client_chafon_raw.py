#!/usr/bin/env python3
import logging
import time
from datetime import datetime, timezone

import requests
import serial

# ------------------------------------------------------------
# Konfiguracja
# ------------------------------------------------------------

SERVER_URL = "http://192.168.67.10:5000/api/tags"
READER_ID = "cf-ru5112-brama-1"

SER_PORT = "/dev/ttyS0"
SER_BAUD = 115200

SEND_INTERVAL_SEC = 0.5      # jak często wysyłać batch
MAX_BATCH_SIZE = 20          # max eventów w jednym batchu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def open_serial() -> serial.Serial:
    """Otwieranie portu RS232 z retry w pętli."""
    while True:
        try:
            logging.info("Opening serial port %s @ %d", SER_PORT, SER_BAUD)
            ser = serial.Serial(
                port=SER_PORT,
                baudrate=SER_BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
            )
            logging.info("Serial port opened")
            return ser
        except serial.SerialException as e:
            logging.error("Serial open failed: %s. Retrying in 5s", e)
            time.sleep(5)


FRAME_PREFIX = b"\x11\x00\xEE\x00"
FRAME_LEN = 4 + 12 + 2    # nagłówek + 12 bajtów EPC + 2 bajty ogona
EPC_OFFSET = 4
EPC_LEN = 12

def extract_epcs(buf: bytearray):
    """
    Ramka Chafon (to co sniffowałeś):

      11 00 EE 00  [12 bajtów EPC]  [2 bajty suma / ogon]

    Możemy mieć kilka ramek sklejonych w buforze.
    """
    epcs = []
    i = 0

    while True:
        idx = buf.find(FRAME_PREFIX, i)
        if idx == -1:
            break

        # Czy mamy całą ramkę?
        if len(buf) < idx + FRAME_LEN:
            # Zostawiamy od nagłówka w górę – resztę odrzucamy
            if idx > 0:
                del buf[:idx]
            return epcs

        frame = bytes(buf[idx:idx + FRAME_LEN])
        epc_bytes = frame[EPC_OFFSET:EPC_OFFSET + EPC_LEN]
        epcs.append(epc_bytes.hex().upper())

        i = idx + FRAME_LEN

    if i > 0:
        del buf[:i]

    return epcs


def send_events(pending):
    """Wysyła batch eventów do centralnego serwera."""
    if not pending:
        return

    payload = {
        "reader_id": READER_ID,
        "events": [
            {
                "id": ev["id"],
                "tag": ev["tag"],          # KLUCZ "tag" – to jest krytyczne
                "ts": ev["ts"],
            }
            for ev in pending
        ],
    }

    try:
        resp = requests.post(SERVER_URL, json=payload, timeout=3)
        logging.info(
            "Sent %d events, server status: %s",
            len(pending),
            resp.status_code,
        )
        if resp.status_code == 200:
            pending.clear()
    except Exception as e:
        logging.error("Error sending events: %s", e)
        # pending zostaje – spróbujemy wysłać w kolejnej iteracji


def main():
    ser = open_serial()
    buf = bytearray()
    pending = []
    seq = 0
    last_send = time.time()

    while True:
        try:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                epcs = extract_epcs(buf)
                now_iso = datetime.now(timezone.utc).isoformat()

                for epc in epcs:
                    seq += 1
                    pending.append(
                        {
                            "id": seq,
                            "tag": epc,
                            "ts": now_iso,
                        }
                    )
                    logging.info("EPC: %s", epc)

            now_t = time.time()
            if pending and (
                now_t - last_send >= SEND_INTERVAL_SEC
                or len(pending) >= MAX_BATCH_SIZE
            ):
                send_events(pending)
                last_send = now_t

        except serial.SerialException as e:
            logging.error("Serial error: %s, reopening in 5s", e)
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(5)
            ser = open_serial()
        except Exception as e:
            logging.error("Unexpected error: %s", e)
            time.sleep(1)


if __name__ == "__main__":
    main()
