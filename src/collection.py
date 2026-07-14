"""
Collection operations: add, remove, list, search, value — now location-aware.

Every collection row is keyed by (scryfall_id, finish, location_id). The same
printing can have multiple rows if you've split copies across containers.
"""

from datetime import datetime, timezone
from .db import get_conn


def add_to_collection(scryfall_id, finish="nonfoil", quantity=1,
                      location_id=1, condition="NM", notes=None):
    """Add `quantity` of a printing to the collection at `location_id`."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (scryfall_id, finish, location_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE collection SET quantity = quantity + ? "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (quantity, scryfall_id, finish, location_id),
            )
        else:
            conn.execute(
                "INSERT INTO collection "
                "(scryfall_id, finish, location_id, quantity, condition, notes, added_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scryfall_id, finish, location_id, quantity, condition, notes, now),
            )


def remove_from_collection(scryfall_id, finish="nonfoil", quantity=1, location_id=1):
    """Decrement quantity at a specific location; remove row at zero. Returns new qty."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (scryfall_id, finish, location_id),
        ).fetchone()
        if not row:
            return 0
        new_qty = row["quantity"] - quantity
        if new_qty <= 0:
            conn.execute(
                "DELETE FROM collection "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (scryfall_id, finish, location_id),
            )
            return 0
        conn.execute(
            "UPDATE collection SET quantity=? "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (new_qty, scryfall_id, finish, location_id),
        )
        return new_qty


def get_collection(name_filter=None, set_filter=None, color_filter=None,
                   location_id=None, sort_by="name", sort_dir="asc"):
    """List collection rows joined with card data. Filters and sorting are optional.

    sort_by: name | price | quantity | color
    sort_dir: asc | desc
    """
    # Whitelist sort columns to prevent SQL injection
    _sort_map = {
        "name":     "c.name COLLATE NOCASE",
        "price":    "COALESCE(CASE WHEN col.finish='foil' THEN c.price_usd_foil ELSE c.price_usd END, 0)",
        "quantity": "col.quantity",
        "color":    "c.color_identity COLLATE NOCASE",
    }
    _dir = "DESC" if sort_dir == "desc" else "ASC"
    sort_expr = _sort_map.get(sort_by, "c.name COLLATE NOCASE")

    query = """
        SELECT col.scryfall_id, col.finish, col.quantity, col.condition, col.notes,
               col.location_id, loc.name AS location_name,
               c.name, c.set_code, c.set_name, c.collector_number, c.rarity,
               c.mana_cost, c.type_line, c.colors, c.color_identity,
               c.image_uri,
               c.price_usd, c.price_usd_foil, c.price_eur
        FROM collection col
        JOIN cards c ON col.scryfall_id = c.scryfall_id
        LEFT JOIN locations loc ON col.location_id = loc.id
        WHERE 1=1
    """
    params = []
    if name_filter:
        query += " AND LOWER(c.name) LIKE ?"
        params.append(f"%{name_filter.lower()}%")
    if set_filter:
        query += " AND LOWER(c.set_code) = ?"
        params.append(set_filter.lower())
    if color_filter:
        query += " AND ("
        clauses = []
        for ch in color_filter.upper():
            clauses.append("c.color_identity LIKE ?")
            params.append(f"%{ch}%")
        query += " OR ".join(clauses)
        query += ")"
    if location_id is not None:
        query += " AND col.location_id = ?"
        params.append(location_id)
    query += f" ORDER BY {sort_expr} {_dir}, c.name COLLATE NOCASE, col.finish"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_collection_entry(old_scryfall_id, old_finish, old_location_id,
                             new_scryfall_id, new_finish, new_location_id, new_quantity):
    """
    Change the printing, finish, location, or quantity of an existing stack.

    If the target (new_scryfall_id, new_finish, new_location_id) already exists,
    the quantities are merged. The old row is deleted.
    """
    with get_conn() as conn:
        old = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (old_scryfall_id, old_finish, old_location_id),
        ).fetchone()
        if not old:
            return

        # Check whether the destination already has a row (possible if changing
        # location only and the card is already in the target).
        same = (new_scryfall_id == old_scryfall_id and
                new_finish == old_finish and
                new_location_id == old_location_id)
        if same:
            # Just update quantity in place.
            conn.execute(
                "UPDATE collection SET quantity=? "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (new_quantity, old_scryfall_id, old_finish, old_location_id),
            )
            return

        existing_dest = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (new_scryfall_id, new_finish, new_location_id),
        ).fetchone()

        # Delete the old row first.
        conn.execute(
            "DELETE FROM collection WHERE scryfall_id=? AND finish=? AND location_id=?",
            (old_scryfall_id, old_finish, old_location_id),
        )

        if existing_dest:
            # Merge into the existing destination row.
            conn.execute(
                "UPDATE collection SET quantity = quantity + ? "
                "WHERE scryfall_id=? AND finish=? AND location_id=?",
                (new_quantity, new_scryfall_id, new_finish, new_location_id),
            )
        else:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO collection "
                "(scryfall_id, finish, location_id, quantity, added_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_scryfall_id, new_finish, new_location_id, new_quantity, now),
            )


def export_collection_csv():
    """
    Return the entire collection as a CSV string, one row per stack.
    Columns match Manabox format where possible for compatibility.
    """
    import csv, io
    rows = get_collection()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "Name", "Set code", "Set name", "Collector number", "Foil",
        "Rarity", "Quantity", "Scryfall ID", "Location",
        "Price USD", "Price USD Foil", "Price EUR",
    ])
    for r in rows:
        foil_val = "foil" if r["finish"] == "foil" else "normal"
        writer.writerow([
            r["name"],
            r["set_code"].upper(),
            r["set_name"],
            r["collector_number"],
            foil_val,
            r["rarity"] or "",
            r["quantity"],
            r["scryfall_id"],
            r["location_name"] or "",
            r["price_usd"] or "",
            r["price_usd_foil"] or "",
            r["price_eur"] or "",
        ])
    return out.getvalue()


def find_in_collection_by_name(name):
    """All printings (across locations) of a given oracle name."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT col.scryfall_id, col.finish, col.quantity,
                   col.location_id, loc.name AS location_name,
                   c.name, c.set_code, c.set_name, c.collector_number,
                   c.price_usd, c.price_usd_foil
            FROM collection col
            JOIN cards c ON col.scryfall_id = c.scryfall_id
            LEFT JOIN locations loc ON col.location_id = loc.id
            WHERE LOWER(c.name) = LOWER(?)
            ORDER BY c.set_code, col.finish, loc.name
        """, (name,)).fetchall()
        return [dict(r) for r in rows]


def collection_value():
    """Sum collection value across USD and EUR, respecting finish."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT col.quantity, col.finish,
                   c.price_usd, c.price_usd_foil, c.price_eur
            FROM collection col
            JOIN cards c ON col.scryfall_id = c.scryfall_id
        """).fetchall()
    total_usd = 0.0
    total_eur = 0.0
    priced = 0
    unpriced = 0
    total_cards = 0
    for r in rows:
        qty = r["quantity"]
        total_cards += qty
        usd = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        eur = r["price_eur"]
        if usd:
            total_usd += usd * qty
            priced += qty
        else:
            unpriced += qty
        if eur:
            total_eur += eur * qty
    return {
        "total_usd": total_usd,
        "total_eur": total_eur,
        "total_cards": total_cards,
        "priced_cards": priced,
        "unpriced_cards": unpriced,
    }


def snapshot_value():
    """Record a price snapshot for history tracking."""
    v = collection_value()
    with get_conn() as conn:
        unique_count = conn.execute(
            "SELECT COUNT(*) AS n FROM collection"
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO price_snapshots "
            "(snapshot_date, total_usd, total_eur, card_count, unique_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                v["total_usd"],
                v["total_eur"],
                v["total_cards"],
                unique_count,
            ),
        )
    return v


def price_history(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT snapshot_date, total_usd, total_eur, card_count, unique_count "
            "FROM price_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def all_collection_ids():
    """All distinct scryfall_ids in the collection — used for bulk price refresh."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scryfall_id FROM collection"
        ).fetchall()
        return [r["scryfall_id"] for r in rows]
