"""
Interactive menu for CardVault.

Each top-level submenu loops until the user picks "back". All Scryfall
network calls are wrapped in try/except so a flaky connection doesn't
crash the app.
"""

import sys
from .collection import (
    add_to_collection, remove_from_collection,
    get_collection, find_in_collection_by_name,
    collection_value, snapshot_value, price_history,
    all_collection_ids,
)
from .treasure_hunt import (
    parse_card_list, plan_treasure_hunt,
    render_plan_as_text, remove_planned_cards,
)
from .importers import import_manabox_csv
from .locations import (
    list_locations, create_location, get_location, find_location_by_name,
    rename_location, delete_location, move_cards, KINDS,
)
from . import scryfall


# ---------------- input helpers ----------------

def prompt(msg, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        ans = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        raise
    return ans if ans else (default if default is not None else "")


def prompt_int(msg, default=None, minimum=None):
    while True:
        raw = prompt(msg, default)
        try:
            v = int(raw)
            if minimum is not None and v < minimum:
                print(f"  Must be >= {minimum}.")
                continue
            return v
        except ValueError:
            print("  Enter a whole number.")


def prompt_choice(msg, choices):
    """choices is a list of (key, label). Returns the key."""
    print()
    for k, label in choices:
        print(f"  {k}. {label}")
    valid = {str(k) for k, _ in choices}
    while True:
        ans = prompt(msg)
        if ans in valid:
            return ans
        print("  Invalid choice.")


def confirm(msg):
    return prompt(f"{msg} (y/N)").lower() in ("y", "yes")


def money(v, symbol="$"):
    if v is None:
        return "  —   "
    return f"{symbol}{v:>7.2f}"


# ---------------- Scryfall + printing picker ----------------

def pick_printing(name):
    """
    Look up all printings of `name` on Scryfall, let the user choose one.
    Returns the chosen card dict (a row from _row_from_scryfall), or None.
    """
    try:
        prints = scryfall.search_printings(name)
    except RuntimeError as e:
        print(f"  Scryfall error: {e}")
        return None
    if not prints:
        print(f"  No card found matching '{name}'.")
        return None

    # Normalize to our row format.
    rows = [scryfall._row_from_scryfall(p) for p in prints]

    # Confirm the canonical name once (in case fuzzy resolved to something else).
    canonical = rows[0]["name"]
    if canonical.lower() != name.lower():
        print(f"  Matched: {canonical}")

    if len(rows) == 1:
        return rows[0]

    # Show choices, newest set first (rows arrive newest-first from Scryfall).
    print(f"\n  '{canonical}' has {len(rows)} printings:")
    for i, r in enumerate(rows[:30], 1):
        price = money(r["price_usd"])
        print(f"   {i:>3}. {r['set_code'].upper():>5}  #{r['collector_number']:<6}  "
              f"{r['set_name'][:30]:<30}  {price} USD")
    if len(rows) > 30:
        print(f"   ... and {len(rows) - 30} more (showing 30 most recent)")

    while True:
        ans = prompt("Pick printing # (or 0 to cancel)", "1")
        try:
            idx = int(ans)
        except ValueError:
            print("  Enter a number.")
            continue
        if idx == 0:
            return None
        if 1 <= idx <= len(rows):
            return rows[idx - 1]
        print("  Out of range.")


def pick_finish(card):
    """Choose nonfoil vs foil. Some cards are foil-only or nonfoil-only on Scryfall;
    we don't track that strictly — just let the user pick."""
    print("\n  Finish:")
    print("   1. Nonfoil")
    if card.get("price_usd_foil"):
        print(f"   2. Foil  ({money(card['price_usd_foil'])} USD)")
    else:
        print("   2. Foil")
    ans = prompt("Choose", "1")
    return "foil" if ans == "2" else "nonfoil"


def pick_location(allow_create=True, label="Pick a location"):
    """
    Present the list of locations and return the chosen location_id, or None if cancelled.
    If allow_create=True, the user can type 'n' to create a new one inline.
    """
    locs = list_locations()
    print(f"\n  {label}:")
    for i, l in enumerate(locs, 1):
        kind = f" [{l['kind']}]" if l["kind"] else ""
        print(f"   {i:>2}. {l['name']}{kind}")
    if allow_create:
        print(f"    n. + Create new location")
    print(f"    0. Cancel")
    while True:
        ans = prompt("Choose", "1").strip().lower()
        if ans == "0":
            return None
        if ans == "n" and allow_create:
            new_id = do_create_location_inline()
            if new_id:
                return new_id
            continue
        try:
            idx = int(ans)
            if 1 <= idx <= len(locs):
                return locs[idx - 1]["id"]
        except ValueError:
            pass
        print("  Invalid choice.")


def do_create_location_inline():
    """Create a location from inside another prompt. Returns new id or None."""
    name = prompt("\n  New location name (blank to cancel)")
    if not name:
        return None
    print(f"  Kinds: {', '.join(KINDS)}")
    kind = prompt("  Kind", "box")
    if kind not in KINDS:
        kind = "other"
    try:
        new_id = create_location(name, kind=kind)
        print(f"  Created '{name}'.")
        return new_id
    except ValueError as e:
        print(f"  {e}")
        return None


# ---------------- main menu ----------------

def main_menu():
    while True:
        choice = prompt_choice("Main menu", [
            ("1", "Collection"),
            ("2", "Treasure Hunt"),
            ("3", "Prices"),
            ("4", "Locations"),
            ("5", "Quick stats"),
            ("q", "Quit"),
        ])
        if choice == "1":
            collection_menu()
        elif choice == "2":
            treasure_hunt_menu()
        elif choice == "3":
            prices_menu()
        elif choice == "4":
            locations_menu()
        elif choice == "5":
            show_stats()
        elif choice == "q":
            print("\nGoodbye.")
            sys.exit(0)


# ---------------- collection ----------------

def collection_menu():
    while True:
        choice = prompt_choice("Collection", [
            ("1", "Add card"),
            ("2", "Remove card"),
            ("3", "Browse / list"),
            ("4", "Look up a card you own"),
            ("5", "Import Manabox CSV"),
            ("b", "Back"),
        ])
        if choice == "1":
            do_add_card()
        elif choice == "2":
            do_remove_card()
        elif choice == "3":
            do_list_collection()
        elif choice == "4":
            do_lookup_owned()
        elif choice == "5":
            do_import_manabox()
        elif choice == "b":
            return


def do_add_card():
    name = prompt("\nCard name (or blank to cancel)")
    if not name:
        return
    print("  Searching Scryfall...")
    card = pick_printing(name)
    if not card:
        return
    finish = pick_finish(card)
    qty = prompt_int("Quantity", "1", minimum=1)
    loc_id = pick_location(label="Which container does this go in?")
    if loc_id is None:
        print("  Cancelled.")
        return
    loc = get_location(loc_id)
    add_to_collection(card["scryfall_id"], finish=finish, quantity=qty,
                      location_id=loc_id)
    print(f"\n  Added {qty}× {card['name']} ({card['set_code'].upper()} "
          f"#{card['collector_number']}) [{finish}] → {loc['name']}.")


def do_remove_card():
    name = prompt("\nCard name (or blank to cancel)")
    if not name:
        return
    owned = find_in_collection_by_name(name)
    if not owned:
        print(f"  You don't own any '{name}'.")
        return
    print(f"\n  '{owned[0]['name']}' in your collection:")
    for i, r in enumerate(owned, 1):
        loc = r.get("location_name") or "(no location)"
        print(f"   {i:>2}. {r['set_code'].upper():>5} #{r['collector_number']:<6} "
              f"[{r['finish']:<7}]  qty {r['quantity']}  @ {loc}")
    idx = prompt_int("Pick which one to remove (0 to cancel)", "0", minimum=0)
    if idx == 0 or idx > len(owned):
        return
    target = owned[idx - 1]
    qty = prompt_int(f"Remove how many (have {target['quantity']})", "1", minimum=1)
    new_qty = remove_from_collection(target["scryfall_id"], target["finish"], qty,
                                     target["location_id"])
    print(f"  Removed. Remaining at {target.get('location_name')}: {new_qty}")


def do_list_collection():
    print("\n  Filters (blank = no filter)")
    name_f = prompt("  Name contains")
    set_f = prompt("  Set code (e.g. MH3)")
    color_f = prompt("  Colors (e.g. WU, BR)")
    loc_filter = confirm("  Filter by location?")
    loc_id = None
    if loc_filter:
        loc_id = pick_location(allow_create=False, label="Filter to which location")
        if loc_id is None:
            return
    rows = get_collection(
        name_filter=name_f or None,
        set_filter=set_f or None,
        color_filter=color_f or None,
        location_id=loc_id,
    )
    if not rows:
        print("\n  No cards match.")
        return
    print(f"\n  {len(rows)} unique entries, "
          f"{sum(r['quantity'] for r in rows)} total cards\n")
    print(f"  {'QTY':>4}  {'NAME':<28}  {'SET':>5}  {'#':<5}  "
          f"{'FIN':<4}  {'LOCATION':<15}  {'USD':>7}")
    print(f"  {'-'*4}  {'-'*28}  {'-'*5}  {'-'*5}  {'-'*4}  {'-'*15}  {'-'*7}")
    total = 0.0
    for r in rows:
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        line_val = (price or 0) * r["quantity"]
        total += line_val
        loc = (r.get("location_name") or "—")[:15]
        finish_short = "foil" if r["finish"] == "foil" else "nm"
        print(f"  {r['quantity']:>4}  {r['name'][:28]:<28}  "
              f"{r['set_code'].upper():>5}  {r['collector_number']:<5}  "
              f"{finish_short:<4}  {loc:<15}  {money(price)}")
    print(f"\n  Subtotal value (USD): ${total:,.2f}")


def do_lookup_owned():
    name = prompt("\nCard name")
    if not name:
        return
    rows = find_in_collection_by_name(name)
    if not rows:
        print(f"  You don't own any '{name}'.")
        return
    print(f"\n  Your '{rows[0]['name']}':")
    total = 0
    for r in rows:
        total += r["quantity"]
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        loc = r.get("location_name") or "(no location)"
        print(f"   {r['quantity']}× {r['set_code'].upper()} "
              f"#{r['collector_number']} [{r['finish']}]  @ {loc}  {money(price)} USD")
    print(f"  Total: {total} copies")


def do_import_manabox():
    import os
    print("\n  Import a Manabox CSV export into your collection.")
    print("  Each row is added by its Scryfall ID; unfamiliar printings are")
    print("  fetched from Scryfall (about 0.1s each).\n")
    path = prompt("Path to CSV (blank to cancel)")
    if not path:
        return
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        print(f"  No such file: {path}")
        return

    loc_id = pick_location(label="Which container are these cards in?")
    if loc_id is None:
        print("  Cancelled.")
        return
    loc = get_location(loc_id)

    # Progress callback prints a single rewriting line.
    import sys as _sys
    def _progress(cur, total, msg):
        line = f"  [{cur}/{total}] {msg[:60]:<60}"
        _sys.stdout.write("\r" + line)
        _sys.stdout.flush()

    try:
        result = import_manabox_csv(path, location_id=loc_id,
                                    progress=_progress)
    except (ValueError, FileNotFoundError) as e:
        print(f"\n  Import failed: {e}")
        return
    except RuntimeError as e:
        print(f"\n  Scryfall error: {e}")
        return

    print()  # clear the progress line
    print(f"\n  === Import complete ===")
    print(f"  Destination:    {loc['name']}")
    print(f"  Rows in file:   {result['total_rows']}")
    print(f"  Imported:       {result['imported']}")
    print(f"  From cache:     {result['from_cache']}")
    print(f"  Fetched fresh:  {result['fetched']}")
    print(f"  Skipped:        {len(result['skipped'])}")
    if result["skipped"]:
        print("\n  Skipped rows:")
        for row_num, name, reason in result["skipped"][:15]:
            print(f"    row {row_num}: {name} — {reason}")
        if len(result["skipped"]) > 15:
            print(f"    ... and {len(result['skipped']) - 15} more")


# ---------------- treasure hunt ----------------

def treasure_hunt_menu():
    """
    Enter a list of cards you want; get back a per-location pull list.
    """
    print("\n  === Treasure Hunt ===")
    print("  Enter the cards you're looking for, one per line.")
    print("  Format: '4 Lightning Bolt' or '4x Lightning Bolt' or just 'Lightning Bolt'.")
    print("  Finish with a blank line, 'END', or Ctrl-D.\n")
    lines = []
    while True:
        try:
            line = input("  > " if not lines else "    ")
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)

    if not lines:
        print("  (Nothing entered.)")
        return

    wanted = parse_card_list("\n".join(lines))
    if not wanted:
        print("  Couldn't parse any cards from that.")
        return

    plan = plan_treasure_hunt(wanted)

    # Header
    t = plan["totals"]
    print(f"\n  === Pull List ===")
    print(f"  Requested: {t['cards_requested']} cards across {len(wanted)} unique names")
    print(f"  Plan pulls {t['cards_pulled']} cards from {t['locations_to_visit']} location(s)")
    if t["cards_short"]:
        print(f"  Short:     {t['cards_short']} cards")

    # Per-location pull list
    if plan["by_location"]:
        print()
        for loc, cards in plan["by_location"].items():
            total_at_loc = sum(c["qty_to_take"] for c in cards)
            print(f"  ── {loc} ── ({total_at_loc} cards, {len(cards)} unique)")
            for c in cards:
                short_tag = ""
                if c["shortfall"]:
                    short_tag = f"  ⚠ short {c['shortfall']} (need {c['needed']})"
                print(f"     {c['qty_to_take']}× {c['name']}  "
                      f"[{c['set_code'].upper()} #{c['collector_number']}, {c['finish']}]"
                      f"{short_tag}")
            print()

    # Missing entirely
    if plan["missing"]:
        print(f"  ── Not in your collection ──")
        for m in plan["missing"]:
            print(f"     {m['needed']}× {m['name']}")
        print()

    # Partials worth flagging — when you DO have copies but spread across boxes
    real_partials = [p for p in plan["partial"]
                     if p["total_owned"] > p["best_qty"]]
    if real_partials:
        print(f"  Note: these cards are spread across multiple locations —")
        print(f"  the plan grabs from the box with the most, but you have more elsewhere:")
        for p in real_partials:
            print(f"     {p['name']}: pulled {p['best_qty']} from {p['best_location']}, "
                  f"own {p['total_owned']} total")

    # If the plan has nothing actually pullable, no point in offering export/remove.
    if not plan["by_location"]:
        return

    # --- Optional: export to a text file ---
    if confirm("\n  Export this list as a text file?"):
        import os
        # Folder lives next to the app so it follows the DB around.
        app_dir = os.path.dirname(os.path.abspath(__file__))
        export_dir = os.path.join(app_dir, "Treasure Hunts")
        default_name = f"treasure_hunt_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        default_path = os.path.join(export_dir, default_name)
        path = prompt("File path", default_path)
        path = os.path.expanduser(path)
        try:
            # Create whatever folder the user pointed at (Treasure Hunts/ by
            # default, or somewhere else if they overrode the path).
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            text = render_plan_as_text(plan, wanted)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"  Wrote {path}")
        except OSError as e:
            print(f"  Couldn't write file: {e}")

    # --- Optional: remove the pulled cards from the collection ---
    print()
    t = plan["totals"]
    print(f"  Once you've physically pulled these cards, you can remove them from")
    print(f"  the database to keep your inventory accurate.")
    print(f"  This will remove {t['cards_pulled']} cards from "
          f"{t['locations_to_visit']} location(s).")
    if confirm("  Remove the pulled cards from your collection now?"):
        # Double-confirm because this is destructive.
        if confirm(f"  Really remove {t['cards_pulled']} cards? This can't be undone."):
            try:
                result = remove_planned_cards(plan)
                print(f"\n  Removed {result['removed']} cards "
                      f"across {result['stacks_affected']} stacks.")
                if result["stacks_emptied"]:
                    print(f"  {result['stacks_emptied']} stack(s) emptied completely.")
                if result["skipped"]:
                    print(f"  {result['skipped']} stack(s) skipped "
                          f"(quantity was lower than the plan expected — "
                          f"probably already removed).")
            except Exception as e:
                print(f"  Removal failed: {e}")
        else:
            print("  Skipped.")


# ---------------- prices ----------------

def prices_menu():
    while True:
        choice = prompt_choice("Prices", [
            ("1", "Show collection value"),
            ("2", "Refresh all prices from Scryfall"),
            ("3", "Snapshot value (save to history)"),
            ("4", "View price history"),
            ("b", "Back"),
        ])
        if choice == "1":
            do_show_value()
        elif choice == "2":
            do_refresh_prices()
        elif choice == "3":
            v = snapshot_value()
            print(f"\n  Snapshot saved: ${v['total_usd']:,.2f} USD "
                  f"/ €{v['total_eur']:,.2f} EUR  ({v['total_cards']} cards)")
        elif choice == "4":
            do_price_history()
        elif choice == "b":
            return


def do_show_value():
    v = collection_value()
    print(f"\n  Total cards:     {v['total_cards']:>8,}")
    print(f"  Priced cards:    {v['priced_cards']:>8,}")
    print(f"  Unpriced cards:  {v['unpriced_cards']:>8,}")
    print(f"  Total USD:       ${v['total_usd']:>10,.2f}")
    print(f"  Total EUR:       €{v['total_eur']:>10,.2f}")
    if v["unpriced_cards"]:
        print(f"\n  ({v['unpriced_cards']} card(s) have no current price data — "
              f"try 'Refresh all prices'.)")


def do_refresh_prices():
    ids = all_collection_ids()
    if not ids:
        print("  Collection is empty.")
        return
    print(f"\n  Refreshing {len(ids)} cards from Scryfall "
          f"(~{len(ids) * 0.1:.0f}s minimum)...")
    try:
        n = scryfall.refresh_prices(ids)
        print(f"  Updated {n} cards.")
    except RuntimeError as e:
        print(f"  Scryfall error: {e}")


def do_price_history():
    rows = price_history(20)
    if not rows:
        print("\n  No snapshots yet — take one from the Prices menu.")
        return
    print(f"\n  {'DATE':<25}  {'USD':>10}  {'EUR':>10}  {'CARDS':>6}  {'UNIQUE':>7}")
    print(f"  {'-'*25}  {'-'*10}  {'-'*10}  {'-'*6}  {'-'*7}")
    for r in rows:
        date_short = r["snapshot_date"][:19].replace("T", " ")
        print(f"  {date_short:<25}  ${r['total_usd'] or 0:>9,.2f}  "
              f"€{r['total_eur'] or 0:>9,.2f}  "
              f"{r['card_count'] or 0:>6}  {r['unique_count'] or 0:>7}")


# ---------------- locations ----------------

def locations_menu():
    while True:
        choice = prompt_choice("Locations", [
            ("1", "List locations"),
            ("2", "Create location"),
            ("3", "Rename location"),
            ("4", "Delete location (cards move to Unsorted)"),
            ("5", "Move cards between locations"),
            ("6", "View contents of a location"),
            ("b", "Back"),
        ])
        if choice == "1":
            do_list_locations()
        elif choice == "2":
            do_create_location_inline()
        elif choice == "3":
            do_rename_location()
        elif choice == "4":
            do_delete_location()
        elif choice == "5":
            do_move_cards()
        elif choice == "6":
            do_view_location()
        elif choice == "b":
            return


def do_list_locations():
    locs = list_locations(with_counts=True)
    if not locs:
        print("\n  No locations yet.")
        return
    print(f"\n  {'ID':>3}  {'NAME':<25}  {'KIND':<10}  {'UNIQUE':>6}  {'CARDS':>6}")
    print(f"  {'-'*3}  {'-'*25}  {'-'*10}  {'-'*6}  {'-'*6}")
    for l in locs:
        print(f"  {l['id']:>3}  {l['name'][:25]:<25}  "
              f"{(l['kind'] or '—'):<10}  "
              f"{l['unique_entries']:>6}  {l['total_cards']:>6}")


def do_rename_location():
    do_list_locations()
    loc_id = pick_location(allow_create=False, label="Rename which location")
    if loc_id is None:
        return
    new_name = prompt("New name")
    if not new_name:
        return
    try:
        rename_location(loc_id, new_name)
        print(f"  Renamed.")
    except Exception as e:
        print(f"  Couldn't rename: {e}")


def do_delete_location():
    do_list_locations()
    loc_id = pick_location(allow_create=False, label="Delete which location")
    if loc_id is None:
        return
    if loc_id == 1:
        print("  The Unsorted location can't be deleted.")
        return
    loc = get_location(loc_id)
    if not confirm(f"Delete '{loc['name']}'? (Cards will move to Unsorted.)"):
        return
    try:
        delete_location(loc_id)
        print("  Deleted.")
    except ValueError as e:
        print(f"  {e}")


def do_move_cards():
    name = prompt("\nCard name to move (blank to cancel)")
    if not name:
        return
    owned = find_in_collection_by_name(name)
    if not owned:
        print(f"  You don't own any '{name}'.")
        return
    print(f"\n  '{owned[0]['name']}' stacks:")
    for i, r in enumerate(owned, 1):
        loc = r.get("location_name") or "(no location)"
        print(f"   {i:>2}. {r['quantity']}× {r['set_code'].upper()} "
              f"#{r['collector_number']} [{r['finish']}] @ {loc}")
    idx = prompt_int("Which stack to move (0 to cancel)", "0", minimum=0)
    if idx == 0 or idx > len(owned):
        return
    src = owned[idx - 1]
    qty = prompt_int(f"Move how many (have {src['quantity']})", str(src['quantity']),
                     minimum=1)
    if qty > src["quantity"]:
        print("  Can't move more than you have.")
        return
    dest_id = pick_location(label="Move to which location")
    if dest_id is None or dest_id == src["location_id"]:
        print("  Cancelled (or same location).")
        return
    try:
        move_cards(src["scryfall_id"], src["finish"], src["location_id"], dest_id, qty)
        print(f"  Moved {qty}× to '{get_location(dest_id)['name']}'.")
    except ValueError as e:
        print(f"  {e}")


def do_view_location():
    loc_id = pick_location(allow_create=False, label="View which location")
    if loc_id is None:
        return
    loc = get_location(loc_id)
    rows = get_collection(location_id=loc_id)
    if not rows:
        print(f"\n  '{loc['name']}' is empty.")
        return
    total = sum(r["quantity"] for r in rows)
    value = sum((r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]) or 0
                * 1 * r["quantity"] for r in rows)
    # ^ a bit of a fiddly expression — recompute cleanly:
    value = 0.0
    for r in rows:
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        if price:
            value += price * r["quantity"]
    print(f"\n  === {loc['name']} ===")
    print(f"  {len(rows)} unique entries, {total} cards, ${value:,.2f} USD\n")
    for r in rows:
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        print(f"   {r['quantity']:>3}× {r['name'][:32]:<32}  "
              f"{r['set_code'].upper():>5} #{r['collector_number']:<5}  "
              f"[{r['finish']}]  {money(price)}")


# ---------------- stats ----------------

def show_stats():
    rows = get_collection()
    if not rows:
        print("\n  Collection is empty.")
        return
    total = sum(r["quantity"] for r in rows)
    v = collection_value()
    print(f"\n  === Quick stats ===")
    print(f"  Total cards:       {total}")
    print(f"  Total value (USD): ${v['total_usd']:,.2f}")

    # Per-location breakdown. Aggregate from the same rows we already pulled
    # so we don't re-query — and respect finish for pricing.
    by_loc = {}  # loc_name -> {"cards": int, "value": float}
    for r in rows:
        loc = r.get("location_name") or "(no location)"
        bucket = by_loc.setdefault(loc, {"cards": 0, "value": 0.0})
        bucket["cards"] += r["quantity"]
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        if price:
            bucket["value"] += price * r["quantity"]

    print(f"\n  By location:")
    # Sort by total value descending so the most valuable container is first.
    sorted_locs = sorted(by_loc.items(), key=lambda kv: -kv[1]["value"])
    name_w = max(len(name) for name, _ in sorted_locs)
    name_w = max(name_w, 8)
    print(f"    {'LOCATION':<{name_w}}  {'CARDS':>6}  {'VALUE':>10}")
    print(f"    {'-' * name_w}  {'-' * 6}  {'-' * 10}")
    for loc_name, stats in sorted_locs:
        print(f"    {loc_name:<{name_w}}  {stats['cards']:>6}  "
              f"${stats['value']:>9,.2f}")
