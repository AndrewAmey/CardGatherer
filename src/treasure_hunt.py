"""
Treasure Hunt — given a list of card names + quantities, tell the user
the most efficient way to physically retrieve them from their containers.

Strategy per card:
  1. Gather all stacks (location × finish × printing) that match the name.
  2. Prefer a single location that can satisfy the entire need on its own.
  3. If no location has enough, pick the one with the most copies (and
     report the shortfall).
  4. Show one location per card. We don't try to split a pull across
     multiple boxes — the user asked for the simple version.

Then group the output by location so the walk-through reads as
"go to DMU Box, grab these N cards; go to Binder, grab these...".
"""

import re
import json
from collections import defaultdict
from .collection import find_in_collection_by_name
from .db import get_conn


def _deck_box_location_ids():
    """IDs of locations whose kind is deck_box."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM locations WHERE kind = 'deck_box'"
        ).fetchall()
        return {r["id"] for r in rows}


def parse_card_list(text):
    """
    Parse a free-form card list. Supports:
        4 Lightning Bolt
        4x Lightning Bolt
        Lightning Bolt        (defaults to qty 1)
        // comments and blank lines are ignored

    Returns a list of (qty, name) tuples, with duplicates collapsed
    (same name listed twice gets its quantities added).
    """
    accum = {}  # name -> total qty (case-insensitive merge, original casing kept)
    casing = {}  # lower -> first-seen original casing
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        # "4 Bolt" / "4x Bolt"
        m = re.match(r"^(\d+)\s*[xX]?\s+(.+)$", line)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip()
        else:
            qty = 1
            name = line
        # Strip "(SET) collector_number" suffixes that some decklists include
        if " (" in name:
            name = name.split(" (")[0].strip()
        if not name:
            continue
        key = name.lower()
        accum[key] = accum.get(key, 0) + qty
        casing.setdefault(key, name)
    return [(qty, casing[key]) for key, qty in accum.items()]


def _pick_best_stack(stacks, needed):
    """
    Given all stacks for one card and how many are needed, pick the best
    single location.

    A "stack" here is a row from find_in_collection_by_name, i.e.
    {scryfall_id, finish, quantity, location_id, location_name,
     set_code, collector_number, ...}.

    Returns (chosen_stack, qty_to_take, shortfall) where:
      - chosen_stack is the stack dict (or None if nothing owned)
      - qty_to_take is how many to grab from there
      - shortfall is how many we still don't have anywhere (0 if covered)
    """
    if not stacks:
        return None, 0, needed

    # Total owned across everywhere
    total_owned = sum(s["quantity"] for s in stacks)

    # First preference: a single location that holds enough.
    # Among those, pick the one with the smallest excess (don't strip your
    # most-stocked box for a 1-of). Tie-break by location name for stability.
    sufficient = [s for s in stacks if s["quantity"] >= needed]
    if sufficient:
        chosen = min(
            sufficient,
            key=lambda s: (s["quantity"], (s.get("location_name") or "")),
        )
        return chosen, needed, 0

    # No single location has enough. Pick the one with the most copies
    # so the user gets the biggest single grab; the rest is shortfall.
    chosen = max(
        stacks,
        key=lambda s: (s["quantity"], -ord((s.get("location_name") or "z")[0])),
    )
    qty_to_take = chosen["quantity"]
    shortfall = needed - min(qty_to_take, total_owned)
    # If they own more elsewhere but we chose only one stack, reflect that
    # in the message — they have *some* but not at one place.
    return chosen, qty_to_take, shortfall


def plan_treasure_hunt(wanted, include_deck_boxes=True):
    """
    Given `wanted` = list of (qty, name), produce a retrieval plan.

    If include_deck_boxes is False, stacks stored in locations of kind
    'deck_box' are ignored — cards sleeved up in decks stay where they are.

    Each planned card carries:
      - "key": stable id (scryfall_id|finish|location_id) used to mark
        individual cards as pulled
      - "pulled": whether the user has physically grabbed it (and had it
        removed from the collection)
    """
    excluded_locations = set()
    if not include_deck_boxes:
        excluded_locations = _deck_box_location_ids()

    by_location = defaultdict(list)
    missing = []
    partial = []
    cards_requested = 0
    cards_pulled = 0
    cards_short = 0

    for needed, name in wanted:
        cards_requested += needed
        stacks = find_in_collection_by_name(name)
        total_anywhere = sum(s["quantity"] for s in stacks)
        if excluded_locations:
            stacks = [s for s in stacks
                      if s["location_id"] not in excluded_locations]
        chosen, qty_to_take, shortfall = _pick_best_stack(stacks, needed)

        if chosen is None:
            # Nothing owned in any searchable location. total_anywhere > 0
            # means copies exist but only inside deck boxes.
            missing.append({
                "name": name,
                "needed": needed,
                "owned_elsewhere": total_anywhere,
            })
            cards_short += needed
            continue

        loc_name = chosen.get("location_name") or "(no location)"
        by_location[loc_name].append({
            "name": chosen["name"],  # canonical name from the DB
            "scryfall_id": chosen["scryfall_id"],
            "location_id": chosen["location_id"],
            "key": f"{chosen['scryfall_id']}|{chosen['finish']}|{chosen['location_id']}",
            "pulled": False,
            "qty_to_take": qty_to_take,
            "qty_at_plan_time": chosen["quantity"],  # for safe removal
            "needed": needed,
            "set_code": chosen["set_code"],
            "collector_number": chosen["collector_number"],
            "finish": chosen["finish"],
            "shortfall": shortfall,
        })
        cards_pulled += qty_to_take
        cards_short += shortfall

        if shortfall > 0:
            total_owned = sum(s["quantity"] for s in stacks)
            partial.append({
                "name": chosen["name"],
                "needed": needed,
                "best_qty": qty_to_take,
                "best_location": loc_name,
                "total_owned": total_owned,
                "shortfall": shortfall,
            })

    # Sort locations alphabetically; within each, alphabetize the cards.
    ordered = {}
    for loc in sorted(by_location.keys(), key=lambda s: s.lower()):
        cards = sorted(by_location[loc], key=lambda c: c["name"].lower())
        ordered[loc] = cards

    return {
        "by_location": ordered,
        "missing": missing,
        "partial": partial,
        "totals": {
            "cards_requested": cards_requested,
            "cards_pulled": cards_pulled,
            "cards_short": cards_short,
            "locations_to_visit": len(ordered),
        },
    }


def render_plan_as_text(plan, wanted, title=None):
    """
    Render a treasure-hunt plan as plain text suitable for writing to a file.
    Mirrors the terminal output but without color/Unicode flourishes that don't
    travel well across editors or printers.
    """
    from datetime import datetime
    lines = []
    lines.append("=" * 60)
    lines.append(f"  CardVault — Treasure Hunt Pull List")
    if title:
        lines.append(f"  {title}")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    lines.append("")

    t = plan["totals"]
    lines.append(f"Requested:  {t['cards_requested']} cards "
                 f"across {len(wanted)} unique names")
    lines.append(f"Plan pulls: {t['cards_pulled']} cards "
                 f"from {t['locations_to_visit']} location(s)")
    if t["cards_short"]:
        lines.append(f"Short:      {t['cards_short']} cards")
    lines.append("")

    if plan["by_location"]:
        for loc, cards in plan["by_location"].items():
            total_at_loc = sum(c["qty_to_take"] for c in cards)
            lines.append(f"--- {loc} ({total_at_loc} cards, {len(cards)} unique) ---")
            for c in cards:
                short_tag = ""
                if c["shortfall"]:
                    short_tag = f"  [SHORT {c['shortfall']} -- need {c['needed']}]"
                lines.append(f"  [ ] {c['qty_to_take']}x {c['name']}  "
                             f"({c['set_code'].upper()} #{c['collector_number']}, "
                             f"{c['finish']}){short_tag}")
            lines.append("")

    if plan["missing"]:
        lines.append("--- Not in your collection ---")
        for m in plan["missing"]:
            lines.append(f"  {m['needed']}x {m['name']}")
        lines.append("")

    real_partials = [p for p in plan["partial"]
                     if p["total_owned"] > p["best_qty"]]
    if real_partials:
        lines.append("--- Spread across multiple locations ---")
        lines.append("(plan grabs from the box with the most; more exist elsewhere)")
        for p in real_partials:
            lines.append(f"  {p['name']}: pulled {p['best_qty']} from "
                         f"{p['best_location']}, own {p['total_owned']} total")
        lines.append("")

    return "\n".join(lines) + "\n"


def remove_planned_cards(plan):
    """
    Subtract every card the plan grabs from the collection.

    Only the qty_to_take amounts are removed — shortfalls and missing cards
    are untouched (you didn't pick them up, so nothing changes for them).

    Safety: each stack is only decremented if its current quantity still
    matches what was present when the plan was generated. If anything has
    changed — the user already ran the removal, or edited the collection
    in between — that stack is skipped. This prevents accidental
    double-removal from eating into other copies.

    Returns:
        {"removed": int (total cards actually removed),
         "stacks_affected": int (rows touched),
         "stacks_emptied": int (rows that hit zero),
         "skipped": int (stacks where qty was lower than expected)}
    """
    from .collection import remove_from_collection
    from .db import get_conn

    removed = 0
    stacks_affected = 0
    stacks_emptied = 0
    skipped = 0

    for loc_name, cards in plan["by_location"].items():
        for c in cards:
            # Look up current quantity at that stack before removing.
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT quantity FROM collection "
                    "WHERE scryfall_id=? AND finish=? AND location_id=?",
                    (c["scryfall_id"], c["finish"], c["location_id"]),
                ).fetchone()
            current = row["quantity"] if row else 0
            if current != c.get("qty_at_plan_time", c["qty_to_take"]):
                # Stack changed since the plan was generated — could be a
                # repeat run after the first removal already happened, or
                # the user added/removed copies separately. Skip rather than
                # mutate something we don't fully understand.
                skipped += 1
                continue
            new_qty = remove_from_collection(
                c["scryfall_id"], c["finish"],
                quantity=c["qty_to_take"], location_id=c["location_id"],
            )
            removed += c["qty_to_take"]
            stacks_affected += 1
            if new_qty == 0:
                stacks_emptied += 1

    return {
        "removed": removed,
        "stacks_affected": stacks_affected,
        "stacks_emptied": stacks_emptied,
        "skipped": skipped,
    }

# ---------------------------------------------------------------------------
# Saved hunts
#
# Hunts are persisted so they can be reopened later with their pulled/not-
# pulled state intact. Stored as one JSON blob per hunt — the plan structure
# above serializes cleanly.
# ---------------------------------------------------------------------------

def _ensure_hunts_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hunts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_date TEXT,
                updated_date TEXT,
                data TEXT NOT NULL
            )
        """)


def create_hunt(title, wanted, plan):
    """Persist a new hunt. Returns its id."""
    from datetime import datetime
    _ensure_hunts_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps({"wanted": wanted, "plan": plan})
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO hunts (title, created_date, updated_date, data) "
            "VALUES (?, ?, ?, ?)",
            (title, now, now, payload),
        )
        return cur.lastrowid


def get_hunt(hunt_id):
    """Load a saved hunt: {id, title, created_date, updated_date, wanted, plan}."""
    _ensure_hunts_table()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM hunts WHERE id = ?", (hunt_id,)
        ).fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    # JSON round-trips (qty, name) tuples as lists; normalize back.
    wanted = [tuple(w) for w in data["wanted"]]
    return {
        "id": row["id"],
        "title": row["title"],
        "created_date": row["created_date"],
        "updated_date": row["updated_date"],
        "wanted": wanted,
        "plan": data["plan"],
    }


def update_hunt(hunt_id, wanted=None, plan=None, title=None):
    """Save changed pieces of a hunt (pulled flags, rename, ...)."""
    from datetime import datetime
    hunt = get_hunt(hunt_id)
    if not hunt:
        return False
    if wanted is not None:
        hunt["wanted"] = wanted
    if plan is not None:
        hunt["plan"] = plan
    if title is not None:
        hunt["title"] = title
    payload = json.dumps({"wanted": hunt["wanted"], "plan": hunt["plan"]})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "UPDATE hunts SET title = ?, data = ?, updated_date = ? WHERE id = ?",
            (hunt["title"], payload, now, hunt_id),
        )
    return True


def delete_hunt(hunt_id):
    _ensure_hunts_table()
    with get_conn() as conn:
        conn.execute("DELETE FROM hunts WHERE id = ?", (hunt_id,))


def list_hunts():
    """
    Saved hunts, most recently updated first, each with progress counts:
    [{id, title, created_date, updated_date, pulled_count, total_count,
      remaining_cards}]
    """
    _ensure_hunts_table()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hunts ORDER BY updated_date DESC"
        ).fetchall()
    result = []
    for row in rows:
        plan = json.loads(row["data"])["plan"]
        planned = [c for cards in plan["by_location"].values() for c in cards]
        pulled = sum(1 for c in planned if c.get("pulled"))
        remaining = sum(q for q, _ in remaining_list(plan))
        result.append({
            "id": row["id"],
            "title": row["title"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
            "pulled_count": pulled,
            "total_count": len(planned),
            "remaining_cards": remaining,
        })
    return result


def find_card_in_plan(plan, key):
    """Locate a planned card by its stable key. Returns the card dict or None."""
    for cards in plan["by_location"].values():
        for c in cards:
            if c.get("key") == key:
                return c
    return None


def pull_card(plan, key):
    """
    Mark one planned card as pulled and remove its copies from the collection.

    Removes up to qty_to_take from the card's stack (never more than the
    stack currently holds — if the collection changed since the plan was
    made, we take what's there and say so). Sets pulled=True either way,
    since the user is telling us the card is now in hand.

    Returns a human-readable message describing what happened, or None if
    the key wasn't found.
    """
    from .collection import remove_from_collection

    c = find_card_in_plan(plan, key)
    if c is None:
        return None
    if c.get("pulled"):
        return f"{c['name']} was already marked as pulled."

    with get_conn() as conn:
        row = conn.execute(
            "SELECT quantity FROM collection "
            "WHERE scryfall_id=? AND finish=? AND location_id=?",
            (c["scryfall_id"], c["finish"], c["location_id"]),
        ).fetchone()
    current = row["quantity"] if row else 0
    to_remove = min(c["qty_to_take"], current)

    if to_remove > 0:
        remove_from_collection(
            c["scryfall_id"], c["finish"],
            quantity=to_remove, location_id=c["location_id"],
        )

    c["pulled"] = True
    if to_remove == c["qty_to_take"]:
        return f"Pulled {to_remove}× {c['name']} — removed from collection."
    if to_remove > 0:
        return (f"Pulled {c['name']}: only {to_remove} of {c['qty_to_take']} "
                f"were still in that location; removed what was there.")
    return (f"Marked {c['name']} as pulled, but that stack was already empty "
            f"— nothing removed.")


def remaining_list(plan):
    """
    The cards still needed: everything not yet pulled, plus everything that
    wasn't in the collection, plus shortfalls on pulled cards.

    Per requested card:
      - not pulled  -> still need the full requested amount
      - pulled      -> still need only the shortfall (if any)
      - missing     -> still need the full requested amount

    Returns [(qty, name)] merged by name, alphabetized — the same shape a
    decklist parser expects.
    """
    need = {}
    casing = {}

    def add(name, qty):
        if qty <= 0:
            return
        key = name.lower()
        need[key] = need.get(key, 0) + qty
        casing.setdefault(key, name)

    for cards in plan["by_location"].values():
        for c in cards:
            if c.get("pulled"):
                add(c["name"], c.get("shortfall", 0))
            else:
                add(c["name"], c["needed"])

    for m in plan["missing"]:
        add(m["name"], m["needed"])

    return [(need[k], casing[k]) for k in sorted(need, key=lambda s: s)]


def render_remaining_as_text(plan):
    """
    Render the still-needed cards as a plain decklist:

        1 Departed Deckhand
        4 Lightning Bolt

    No headers or decoration — the output can be pasted straight into a
    store's mass-entry / buylist box.
    """
    lines = [f"{qty} {name}" for qty, name in remaining_list(plan)]
    return "\n".join(lines) + ("\n" if lines else "")
