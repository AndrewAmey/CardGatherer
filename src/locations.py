"""
Physical storage locations (boxes, binders, deck boxes, etc).

One location per container. Cards reference a location_id from the
collection table; the same printing can appear in multiple locations
(separate rows).
"""

from datetime import datetime, timezone
from .db import get_conn


KINDS = ("box", "binder", "deck_box", "shelf", "other")


def list_locations(with_counts=False):
    """All locations, optionally with card counts attached.

    Sorted by kind (in KINDS order: box, binder, deck_box, shelf, other,
    with any unrecognized/blank kind sorted last), then by name within
    each kind.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, kind, notes FROM locations"
        ).fetchall()
        result = [dict(r) for r in rows]

        def sort_key(loc):
            kind = loc["kind"] or ""
            try:
                kind_rank = KINDS.index(kind)
            except ValueError:
                kind_rank = len(KINDS)  # unknown/blank kinds sort last
            return (kind_rank, loc["name"].casefold())

        result.sort(key=sort_key)

        if with_counts:
            for loc in result:
                counts = conn.execute(
                    "SELECT COUNT(*) AS unique_entries, "
                    "COALESCE(SUM(quantity), 0) AS total_cards "
                    "FROM collection WHERE location_id = ?",
                    (loc["id"],),
                ).fetchone()
                loc["unique_entries"] = counts["unique_entries"]
                loc["total_cards"] = counts["total_cards"]
        return result


def get_location(location_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, kind, notes FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        return dict(row) if row else None


def find_location_by_name(name):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, kind, notes FROM locations "
            "WHERE LOWER(name) = LOWER(?)",
            (name,),
        ).fetchone()
        return dict(row) if row else None


def create_location(name, kind=None, notes=None):
    """Create a new location. Returns its id. Raises ValueError on duplicate name."""
    if not name or not name.strip():
        raise ValueError("Location name can't be empty.")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM locations WHERE LOWER(name) = LOWER(?)",
            (name.strip(),),
        ).fetchone()
        if existing:
            raise ValueError(f"Location '{name}' already exists.")
        cur = conn.execute(
            "INSERT INTO locations (name, kind, notes, created_date) "
            "VALUES (?, ?, ?, ?)",
            (name.strip(), kind, notes, now),
        )
        return cur.lastrowid


def rename_location(location_id, new_name):
    with get_conn() as conn:
        conn.execute(
            "UPDATE locations SET name = ? WHERE id = ?",
            (new_name.strip(), location_id),
        )


def delete_location(location_id, move_to_id=1):
    """
    Delete a location. Any cards in it are moved to `move_to_id`
    (default: Unsorted, id=1). The Unsorted bucket itself can't be deleted.
    """
    if location_id == 1:
        raise ValueError("The Unsorted location can't be deleted.")
    with get_conn() as conn:
        # Merge collection rows: same printing+finish may already exist at the target.
        rows = conn.execute(
            "SELECT scryfall_id, finish, quantity, condition, notes, added_date "
            "FROM collection WHERE location_id = ?",
            (location_id,),
        ).fetchall()
        for r in rows:
            existing = conn.execute(
                "SELECT quantity FROM collection "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (r["scryfall_id"], r["finish"], move_to_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE collection SET quantity = quantity + ? "
                    "WHERE scryfall_id=? AND finish=? AND location_id=?",
                    (r["quantity"], r["scryfall_id"], r["finish"], move_to_id),
                )
                conn.execute(
                    "DELETE FROM collection WHERE scryfall_id=? AND finish=? AND location_id=?",
                    (r["scryfall_id"], r["finish"], location_id),
                )
            else:
                conn.execute(
                    "UPDATE collection SET location_id = ? "
                    "WHERE scryfall_id=? AND finish=? AND location_id=?",
                    (move_to_id, r["scryfall_id"], r["finish"], location_id),
                )
        conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))


def move_cards(scryfall_id, finish, from_location_id, to_location_id, quantity):
    """Move `quantity` of a specific stack from one location to another."""
    if from_location_id == to_location_id:
        return
    with get_conn() as conn:
        src = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (scryfall_id, finish, from_location_id),
        ).fetchone()
        if not src or src["quantity"] < quantity:
            raise ValueError("Not enough copies at source location.")
        # Decrement (or delete) source
        if src["quantity"] == quantity:
            conn.execute(
                "DELETE FROM collection "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (scryfall_id, finish, from_location_id),
            )
        else:
            conn.execute(
                "UPDATE collection SET quantity = quantity - ? "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (quantity, scryfall_id, finish, from_location_id),
            )
        # Increment (or insert) destination
        dest = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (scryfall_id, finish, to_location_id),
        ).fetchone()
        if dest:
            conn.execute(
                "UPDATE collection SET quantity = quantity + ? "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (quantity, scryfall_id, finish, to_location_id),
            )
        else:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO collection "
                "(scryfall_id, finish, location_id, quantity, added_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (scryfall_id, finish, to_location_id, quantity, now),
            )
