import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'neposedi.db')


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                visit_time TEXT NOT NULL,
                service TEXT,
                event_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
            """
        )


def upsert_client(name: str, phone: str) -> int:
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        # Try update name if exists
        cur = conn.execute("SELECT id FROM clients WHERE phone = ?", (phone,))
        row = cur.fetchone()
        if row:
            client_id = row[0]
            if name:
                conn.execute("UPDATE clients SET name = ? WHERE id = ?", (name, client_id))
            return client_id
        cur = conn.execute(
            "INSERT INTO clients(name, phone, created_at) VALUES (?, ?, ?)",
            (name, phone, now),
        )
        return cur.lastrowid


def add_booking(client_id: int, visit_time_iso: str, service: str | None, event_id: str | None) -> int:
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO bookings(client_id, visit_time, service, event_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (client_id, visit_time_iso, service, event_id, now),
        )
        return cur.lastrowid


