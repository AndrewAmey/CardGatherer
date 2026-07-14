"""
SQLite storage for CardVault.

Tables:
  cards       — printing-level data fetched from Scryfall (cached locally)
  collection  — quantities owned, keyed by (scryfall_id, finish, location_id)
  locations   — physical containers (boxes, binders, deck boxes)
  price_snapshots — collection value over time
"""

import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime

# Locate the data directory: the project root is one level up from src/,
# and the database lives in a 'data' subfolder there.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SRC_DIR)
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

DB_PATH = os.path.join(_DATA_DIR, "cardvault.db")
# Legacy paths from previous app versions, in order of preference.
# Old layout had the DB at the project root next to the .py files.
_LEGACY_DB_PATHS = [
    os.path.join(_PROJECT_ROOT, "cardvault.db"),
    os.path.join(_SRC_DIR, "cardvault.db"),
]


def _ensure_data_dir():
    """Create the data folder if missing, and migrate any legacy DB into it."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        return
    for legacy in _LEGACY_DB_PATHS:
        if os.path.exists(legacy):
            shutil.move(legacy, DB_PATH)
            return


@contextmanager
def get_conn():
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        c = conn.cursor()

        # Printings cached from Scryfall.
        # scryfall_id uniquely identifies a printing (a specific set + collector number).
        c.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT NOT NULL,
                name TEXT NOT NULL,
                set_code TEXT NOT NULL,
                set_name TEXT NOT NULL,
                collector_number TEXT NOT NULL,
                rarity TEXT,
                mana_cost TEXT,
                type_line TEXT,
                oracle_text TEXT,
                colors TEXT,
                color_identity TEXT,
                image_uri TEXT,
                scryfall_uri TEXT,
                price_usd REAL,
                price_usd_foil REAL,
                price_eur REAL,
                price_tix REAL,
                last_updated TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_oracle_id ON cards(oracle_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)")

        # Physical storage containers (boxes, binders, deck boxes, etc).
        c.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT,
                notes TEXT,
                created_date TEXT
            )
        """)
        # Seed a default "Unsorted" bucket so the collection always has somewhere
        # to land when no location is specified (e.g. older data, manual entry
        # without picking a spot).
        c.execute(
            "INSERT OR IGNORE INTO locations (id, name, kind, notes, created_date) "
            "VALUES (1, 'Unsorted', 'other', "
            "'Default location for cards without a specified container', "
            "datetime('now'))"
        )

        # Collection: how many of each (printing + finish + location) you own.
        # Splitting copies across locations means same printing -> multiple rows.
        c.execute("""
            CREATE TABLE IF NOT EXISTS collection (
                scryfall_id TEXT NOT NULL,
                finish TEXT NOT NULL DEFAULT 'nonfoil',
                location_id INTEGER NOT NULL DEFAULT 1,
                quantity INTEGER NOT NULL DEFAULT 0,
                condition TEXT DEFAULT 'NM',
                notes TEXT,
                added_date TEXT,
                PRIMARY KEY (scryfall_id, finish, location_id),
                FOREIGN KEY (scryfall_id) REFERENCES cards(scryfall_id),
                FOREIGN KEY (location_id) REFERENCES locations(id)
            )
        """)

        # Migration: older databases had collection with PK (scryfall_id, finish).
        # Add location_id if missing and backfill to the Unsorted bucket.
        cols = {row["name"] for row in c.execute("PRAGMA table_info(collection)").fetchall()}
        if "location_id" not in cols:
            # SQLite can't add a column to an existing PK, so rebuild the table.
            c.execute("ALTER TABLE collection RENAME TO collection_old")
            c.execute("""
                CREATE TABLE collection (
                    scryfall_id TEXT NOT NULL,
                    finish TEXT NOT NULL DEFAULT 'nonfoil',
                    location_id INTEGER NOT NULL DEFAULT 1,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    condition TEXT DEFAULT 'NM',
                    notes TEXT,
                    added_date TEXT,
                    PRIMARY KEY (scryfall_id, finish, location_id),
                    FOREIGN KEY (scryfall_id) REFERENCES cards(scryfall_id),
                    FOREIGN KEY (location_id) REFERENCES locations(id)
                )
            """)
            c.execute("""
                INSERT INTO collection
                    (scryfall_id, finish, location_id, quantity, condition, notes, added_date)
                SELECT scryfall_id, finish, 1, quantity, condition, notes, added_date
                FROM collection_old
            """)
            c.execute("DROP TABLE collection_old")

        # Decks were removed in favor of Treasure Hunt. Drop the legacy tables
        # if a previous version of the app created them.
        c.execute("DROP TABLE IF EXISTS deck_cards")
        c.execute("DROP TABLE IF EXISTS decks")

        # Price history snapshots
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                total_usd REAL,
                total_usd_foil REAL,
                total_eur REAL,
                card_count INTEGER,
                unique_count INTEGER
            )
        """)


# ---------------- backup / restore ----------------

def backup_db_bytes():
    """
    Return the raw bytes of a fully consistent snapshot of the live
    database, taken via SQLite's own backup API (safe even if a write is
    briefly in progress elsewhere). This captures everything in one file:
    cards cache, collection, locations, price history, and any other
    tables the app has created (e.g. saved treasure hunts).
    """
    _ensure_data_dir()
    src_conn = sqlite3.connect(DB_PATH)
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst_conn = sqlite3.connect(tmp_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        src_conn.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def restore_db_bytes(data):
    """
    Replace the live database with the contents of `data` (bytes from a
    previously downloaded backup file). Validates it looks like a real
    backup for this app before touching anything, and keeps a timestamped
    safety copy of whatever was live beforehand in the data/ folder in
    case the restore turns out to be a mistake.

    Raises ValueError if `data` doesn't look like a valid backup.
    """
    _ensure_data_dir()
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        # Make sure it's a real SQLite file with the tables we expect
        # before we let it anywhere near the live database.
        try:
            conn = sqlite3.connect(tmp_path)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            raise ValueError("That file isn't a valid database backup.")

        required = {"cards", "locations", "collection"}
        if not required.issubset(tables):
            raise ValueError(
                "That file doesn't look like a collection backup "
                "(missing expected tables)."
            )

        if os.path.exists(DB_PATH):
            safety_name = (
                f"pre_restore_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            )
            shutil.copy2(DB_PATH, os.path.join(_DATA_DIR, safety_name))

        shutil.move(tmp_path, DB_PATH)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
