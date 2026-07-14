"""
Import a Manabox CSV export into the collection.

Manabox columns (as of the user's export):
  Name, Set code, Set name, Collector number, Foil, Rarity, Quantity,
  ManaBox ID, Scryfall ID, Purchase price, Misprint, Altered, Condition,
  Language, Purchase price currency, Added

Per spec, the following columns are IGNORED:
  Set name, Rarity, ManaBox ID, Purchase price, Misprint, Altered,
  Condition, Language, Purchase price currency, Added

The Foil column is ALWAYS honored, exactly as Manabox labels each row.
Manabox uses 'normal' / 'foil' / 'etched'; 'normal' (or blank) imports
as nonfoil, everything else imports as foil.

Performance: uses Scryfall's bulk POST /cards/collection endpoint to fetch
up to 75 cards per request. For a 240-card file that's 4 requests instead
of 240 — much faster and far less likely to trip rate limiting. Already-
cached cards are skipped entirely.

Resumable: if you hit Ctrl-C or a network error partway through, re-running
the import is safe. Cards added in the previous run come from cache; rows
will accumulate quantities (so don't blindly re-run unless you intend to add
another copy of everything — there's a 'replace' workflow for that, but it's
not the default).
"""

import csv
from .db import get_conn
from .collection import add_to_collection
from . import scryfall


REQUIRED_COLUMNS = {"Name", "Set code", "Collector number", "Quantity", "Scryfall ID"}


def _cached_ids(scryfall_ids):
    """Return the subset of `scryfall_ids` already in the local cards table."""
    if not scryfall_ids:
        return set()
    # Chunk so we don't blow past SQLite's ~999 param limit on huge files.
    cached = set()
    ids = list(scryfall_ids)
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT scryfall_id FROM cards WHERE scryfall_id IN ({placeholders})",
                chunk,
            ).fetchall()
            cached.update(r["scryfall_id"] for r in rows)
    return cached


def import_manabox_csv(path, location_id=1, progress=None):
    """
    Import a Manabox CSV. The Foil column is honored as-is for every row.

    Args:
        path: file path to the CSV
        location_id: physical container where these cards go. Default 1 (Unsorted).
        progress: optional callback(current, total, message) for UI updates

    Returns a dict:
        {
          "imported":    int,   # rows successfully added to collection
          "fetched":     int,   # cards fetched fresh from Scryfall this run
          "from_cache":  int,   # cards resolved from local cache
          "skipped":     [(row_num, name, reason), ...],
          "total_rows":  int,
        }
    """
    result = {
        "imported": 0,
        "fetched": 0,
        "from_cache": 0,
        "skipped": [],
        "total_rows": 0,
    }

    # --- Parse CSV ---
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"CSV is missing required columns: {sorted(missing)}. "
                f"Found: {reader.fieldnames}"
            )
        rows = list(reader)

    result["total_rows"] = len(rows)
    if not rows:
        return result

    # --- Pre-validate rows; collect Scryfall IDs that need fetching ---
    valid_rows = []  # list of (row_num, name, scryfall_id, qty, finish)
    for i, row in enumerate(rows, 1):
        name = (row.get("Name") or "").strip()
        scryfall_id = (row.get("Scryfall ID") or "").strip()
        qty_raw = (row.get("Quantity") or "").strip()
        foil_raw = (row.get("Foil") or "").strip().lower()

        if not scryfall_id:
            result["skipped"].append((i, name, "no Scryfall ID"))
            continue
        try:
            qty = int(qty_raw)
            if qty <= 0:
                raise ValueError
        except ValueError:
            result["skipped"].append((i, name, f"bad quantity: {qty_raw!r}"))
            continue

        # Take the Foil column exactly as Manabox labels it.
        finish = "nonfoil" if foil_raw in ("", "normal", "nonfoil") else "foil"

        valid_rows.append((i, name, scryfall_id, qty, finish))

    if not valid_rows:
        return result

    # --- Figure out which IDs need a network fetch ---
    all_ids = {r[2] for r in valid_rows}
    already_cached = _cached_ids(all_ids)
    needs_fetch = sorted(all_ids - already_cached)

    if progress:
        progress(0, len(valid_rows),
                 f"{len(already_cached)} cached, {len(needs_fetch)} to fetch")

    # --- Bulk-fetch the missing cards ---
    bulk_not_found = set()
    if needs_fetch:
        def _bulk_progress(done, total, msg):
            if progress:
                progress(done, total, msg)
        try:
            bulk = scryfall.get_cards_collection(needs_fetch, progress=_bulk_progress)
            result["fetched"] = len(bulk["found"])
            bulk_not_found = set(bulk["not_found"])
        except RuntimeError as e:
            # Catastrophic — mark all uncached rows as skipped
            for (rownum, nm, sid, _, _) in valid_rows:
                if sid in needs_fetch:
                    result["skipped"].append((rownum, nm, f"Scryfall error: {e}"))
            # Continue: cached cards can still be imported below.
            needs_fetch = []  # treat as "none more available"

    # --- Insert rows ---
    cached_after_fetch = _cached_ids(all_ids)
    for idx, (rownum, name, sid, qty, finish) in enumerate(valid_rows, 1):
        if progress and idx % 25 == 0:
            progress(idx, len(valid_rows), f"Inserting {name[:30]}")
        if sid not in cached_after_fetch:
            if sid in bulk_not_found:
                result["skipped"].append((rownum, name, "Scryfall didn't recognize this ID"))
            elif sid in needs_fetch:
                # We tried to fetch but it didn't come back successfully
                result["skipped"].append((rownum, name, "fetch failed (try again)"))
            continue
        if sid in already_cached:
            result["from_cache"] += 1
        try:
            add_to_collection(sid, finish=finish, quantity=qty, location_id=location_id)
            result["imported"] += 1
        except Exception as e:
            result["skipped"].append((rownum, name, f"DB error: {e}"))

    return result
