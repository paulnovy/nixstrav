#!/usr/bin/env python3
import sqlite3
import time

DB_PATH = "/opt/rfid-server/events.db"
POLL_INTERVAL_SEC = 0.5  # co ile odświeżamy (sekundy)


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ostatni ID w całej tabeli
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM events")
    row = cur.fetchone()
    last_id = row[0] if row and row[0] is not None else 0

    print(f"Start od id={last_id}. Czekam na nowe eventy (Ctrl+C aby wyjść).\n")
    print(" id    czas      reader_id             tag                       reason")
    print("------ --------  -------------------   ------------------------  ----------------")

    try:
        while True:
            cur.execute(
                """
                SELECT id, reader_id, tag, reason, received_at
                FROM events
                WHERE id > ?
                ORDER BY id ASC
                """,
                (last_id,),
            )
            rows = cur.fetchall()

            for ev_id, reader_id, tag, reason, ts in rows:
                last_id = ev_id
                t = ts[11:19] if ts else ""
                print(f"{ev_id:6d} {t}  {reader_id:19s}  {tag:24s}  {reason}")

            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nKoniec (Ctrl+C).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
