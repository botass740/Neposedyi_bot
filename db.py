import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'neposedi.db')


def migrate_db() -> None:
    """Миграция базы данных: добавляет недостающие колонки и таблицы"""
    with sqlite3.connect(DB_PATH) as conn:
        # Проверяем, существует ли колонка master_id в таблице bookings
        cursor = conn.execute("PRAGMA table_info(bookings)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'master_id' not in columns:
            logger.info("[DB MIGRATION] Добавляю колонку master_id в таблицу bookings...")
            conn.execute("ALTER TABLE bookings ADD COLUMN master_id INTEGER")
            logger.info("[DB MIGRATION] Колонка master_id добавлена!")
        else:
            logger.info("[DB MIGRATION] Колонка master_id уже существует")
        
        # Создаем таблицу ratings, если её нет
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                master_id INTEGER NOT NULL,
                booking_id INTEGER,
                rating INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id),
                FOREIGN KEY(booking_id) REFERENCES bookings(id)
            )
            """
        )
        logger.info("[DB MIGRATION] Миграция завершена!")


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
                master_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                master_id INTEGER NOT NULL,
                booking_id INTEGER,
                rating INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id),
                FOREIGN KEY(booking_id) REFERENCES bookings(id)
            )
            """
        )
    
    # Запускаем миграцию после инициализации
    migrate_db()


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


def add_booking(client_id: int, visit_time_iso: str, service: str | None, event_id: str | None, master_id: int | None = None) -> int:
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO bookings(client_id, visit_time, service, event_id, master_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, visit_time_iso, service, event_id, master_id, now),
        )
        return cur.lastrowid


def get_last_master_for_client(phone: str) -> int | None:
    """Получить ID последнего мастера для клиента"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT b.master_id 
            FROM bookings b
            JOIN clients c ON b.client_id = c.id
            WHERE c.phone = ? AND b.master_id IS NOT NULL
            ORDER BY b.visit_time DESC
            LIMIT 1
            """,
            (phone,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def add_rating(client_id: int, master_id: int, rating: int, booking_id: int | None = None, comment: str | None = None) -> int:
    """Добавить оценку мастеру"""
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO ratings(client_id, master_id, booking_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, master_id, booking_id, rating, comment, now),
        )
        return cur.lastrowid


def get_master_rating(master_id: int) -> float | None:
    """Получить средний рейтинг мастера"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT AVG(rating) FROM ratings WHERE master_id = ?",
            (master_id,)
        )
        row = cur.fetchone()
        return round(row[0], 1) if row and row[0] else None


