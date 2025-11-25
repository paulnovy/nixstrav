#!/usr/bin/env python3
import serial, time, binascii

# PODSTAW TU WŁAŚCIWY PORT Z /dev/serial/by-id
PORT = "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0"
BAUD = 9600

CMDS = {
    1: bytes.fromhex("55 56 00 00 00 01 04 B0"),
    2: bytes.fromhex("55 56 00 00 00 02 04 B1"),
    3: bytes.fromhex("55 56 00 00 00 03 04 B2"),
    4: bytes.fromhex("55 56 00 00 00 04 04 B3"),
}

ser = serial.Serial(
    port=PORT,
    baudrate=BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0.5,
)

print(f"Opened {PORT} @ {BAUD}")

def fire(ch: int):
    data = CMDS[ch]
    print(f"\n=== FIRE CHANNEL {ch} ===")
    print("TX:", binascii.hexlify(data).decode().upper())
    ser.write(data)
    ser.flush()
    # ta płytka najczęściej NIC nie odpowiada – więc brak RX jest normalny,
    # ale spróbujmy zajrzeć w bufor
    time.sleep(0.1)
    resp = ser.read(64)
    if resp:
        print("RX:", binascii.hexlify(resp).decode().upper())
    else:
        print("RX: (no response)")

try:
    input("Enter aby odpalić kanał 1...")
    fire(1)

    input("Enter aby odpalić kanał 2...")
    fire(2)

    input("Enter aby odpalić kanał 3...")
    fire(3)

    input("Enter aby odpalić kanał 4...")
    fire(4)

finally:
    ser.close()
    print("Closed", PORT)
EOF

chmod 755 /root/test_relay_eletechsup.py
