"""
CardVault — Web interface.

Run:  python web.py
Then open http://localhost:5000 in a browser. To access from your phone
on the same Wi-Fi, find your laptop's local IP (e.g. with `ipconfig` on
Windows or `ifconfig`/`ip addr` on Mac/Linux) and visit
http://<that-ip>:5000 from the phone.

All business logic lives in the other modules — this file is just
routing + HTML rendering.
"""

import io
import os
import random
import socket
import threading
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, abort,
)

from src.db import init_db, get_conn, backup_db_bytes, restore_db_bytes
from src.collection import (
    add_to_collection, remove_from_collection,
    get_collection, find_in_collection_by_name,
    collection_value, snapshot_value, price_history,
    all_collection_ids, update_collection_entry, export_collection_csv,
)
from src.locations import (
    list_locations, get_location, create_location,
    rename_location, delete_location, move_cards, KINDS,
)
from src.treasure_hunt import (
    parse_card_list, plan_treasure_hunt,
    render_plan_as_text, remove_planned_cards,
)
from src.importers import import_manabox_csv
from src import scryfall


app = Flask(__name__)
app.secret_key = "cardvault-local-app-not-for-production"

# Make sure the DB exists before any request is served. Safe to call
# repeatedly — init_db uses CREATE TABLE IF NOT EXISTS.
init_db()

# Track long-running background jobs (imports, price refresh) by id.
# Each value: {"status": "running"|"done"|"error", "current": int, "total": int,
#              "message": str, "result": dict-or-None}
_jobs = {}
_jobs_lock = threading.Lock()


# ---------------- helpers ----------------

def _job_update(job_id, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _new_job():
    job_id = f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "current": 0, "total": 0,
                         "message": "Starting...", "result": None}
    return job_id


@app.template_filter("usd")
def usd_filter(value):
    if value is None:
        return "—"
    return f"${value:,.2f}"


@app.template_filter("safe_loc")
def safe_loc_filter(value):
    return value or "(no location)"


# ---------------- pages ----------------

@app.route("/")
def home():
    """Home: search bar + a marquee of random cards from the collection."""
    rows = get_collection()
    # one printing per unique card name, images only
    seen = {}
    for r in rows:
        if r.get("image_uri") and r["name"] not in seen:
            seen[r["name"]] = r
    pool = list(seen.values())
    showcase = random.sample(pool, min(20, len(pool))) if pool else []
    return render_template("home.html", showcase=showcase)


# ---------- Collection ----------

@app.route("/collection")
def collection_browse():
    name = request.args.get("name", "").strip()
    set_code = request.args.get("set", "").strip()
    colors = request.args.get("colors", "").strip()
    loc_id = request.args.get("location_id", "").strip()
    sort_by = request.args.get("sort_by", "name")
    sort_dir = request.args.get("sort_dir", "asc")
    try:
        loc_id_int = int(loc_id) if loc_id else None
    except ValueError:
        loc_id_int = None
    rows = get_collection(
        name_filter=name or None,
        set_filter=set_code or None,
        color_filter=colors or None,
        location_id=loc_id_int,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    # When sorting by name group cards together visually; for other sorts
    # keep individual stacks separate so ordering is meaningful.
    if sort_by == "name":
        grouped = {}
        for r in rows:
            grouped.setdefault(r["name"], []).append(r)
    else:
        # Each row is its own "group" (one stack per tile)
        grouped = {f"{r['name']}||{r['scryfall_id']}||{r['finish']}||{r['location_id']}": [r]
                   for r in rows}
    locs = list_locations()
    return render_template("collection.html",
                           grouped=grouped,
                           total_unique=len(rows),
                           total_cards=sum(r["quantity"] for r in rows),
                           filters={"name": name, "set": set_code,
                                    "colors": colors, "location_id": loc_id},
                           sort_by=sort_by,
                           sort_dir=sort_dir,
                           locations=locs)


@app.route("/collection/card/<name>")
def card_detail(name):
    rows = find_in_collection_by_name(name)
    if not rows:
        # Fallback: pull printings from the cards cache anyway, so we can
        # show details for a card the user has searched for but doesn't own.
        with get_conn() as conn:
            cached = conn.execute(
                "SELECT * FROM cards WHERE LOWER(name) = LOWER(?) "
                "ORDER BY set_code DESC LIMIT 30",
                (name,),
            ).fetchall()
        if not cached:
            abort(404)
        return render_template("card_detail.html",
                               name=name, rows=[], cached_only=[dict(r) for r in cached],
                               all_locations=list_locations())
    # Pull image_uri for each printing the user owns.
    with get_conn() as conn:
        ids = list({r["scryfall_id"] for r in rows})
        placeholders = ",".join("?" for _ in ids)
        img_map = {
            r["scryfall_id"]: r["image_uri"]
            for r in conn.execute(
                f"SELECT scryfall_id, image_uri FROM cards "
                f"WHERE scryfall_id IN ({placeholders})", ids
            ).fetchall()
        }
    for r in rows:
        r["image_uri"] = img_map.get(r["scryfall_id"])
    return render_template("card_detail.html", name=rows[0]["name"], rows=rows,
                           cached_only=[], all_locations=list_locations())


@app.route("/collection/add", methods=["GET", "POST"])
def collection_add():
    """
    Two-step add:
      GET ?name=...     -> show printing picker (search Scryfall)
      POST              -> commit (scryfall_id, finish, qty, location_id)
    """
    if request.method == "POST":
        scryfall_id = request.form["scryfall_id"]
        finish = request.form.get("finish", "nonfoil")
        qty = int(request.form.get("quantity", "1"))
        loc_id = int(request.form["location_id"])
        # Make sure the printing is cached locally first.
        with get_conn() as conn:
            cached = conn.execute(
                "SELECT 1 FROM cards WHERE scryfall_id = ?", (scryfall_id,)
            ).fetchone()
        if not cached:
            try:
                card = scryfall._request(f"/cards/{scryfall_id}")
                if card:
                    scryfall.cache_card(card)
            except RuntimeError as e:
                flash(f"Scryfall error: {e}", "error")
                return redirect(url_for("collection_add"))
        add_to_collection(scryfall_id, finish=finish, quantity=qty, location_id=loc_id)
        flash(f"Added {qty}× to collection.", "success")
        return redirect(url_for("card_detail",
                                name=request.form.get("card_name", "")))

    name = request.args.get("name", "").strip()
    printings = []
    error = None
    if name:
        try:
            raw = scryfall.search_printings(name)
            printings = []
            for r in raw:
                row = scryfall._row_from_scryfall(r)
                # Augment with extra fields useful for picking the right
                # printing. These aren't persisted in the cards table; they
                # live only on this row for the picker UI.
                row["released_at"] = r.get("released_at")
                row["promo"] = r.get("promo", False)
                row["variation"] = r.get("variation", False)
                row["frame"] = r.get("frame")
                row["border_color"] = r.get("border_color")
                row["full_art"] = r.get("full_art", False)
                row["lang"] = r.get("lang", "en")
                row["finishes"] = r.get("finishes", [])  # ["nonfoil", "foil", "etched"]
                printings.append(row)
        except RuntimeError as e:
            error = str(e)
    locs = list_locations()
    return render_template("add.html", name=name, printings=printings,
                           locations=locs, error=error)


@app.route("/collection/export")
def collection_export():
    """Download entire collection as CSV."""
    csv_text = export_collection_csv()
    fname = f"cardvault_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        io.BytesIO(csv_text.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/collection/edit", methods=["POST"])
def collection_edit():
    """Change the printing, finish, location, or quantity of a stack."""
    old_sid  = request.form["old_scryfall_id"]
    old_fin  = request.form["old_finish"]
    old_loc  = int(request.form["old_location_id"])
    new_sid  = request.form["new_scryfall_id"]
    new_fin  = request.form.get("new_finish", old_fin)
    new_loc  = int(request.form.get("new_location_id", old_loc))
    new_qty  = int(request.form.get("new_quantity", 1))
    card_name = request.form.get("card_name", "")

    # If the printing changed, make sure we have it cached locally.
    if new_sid != old_sid:
        with get_conn() as conn:
            cached = conn.execute(
                "SELECT 1 FROM cards WHERE scryfall_id=?", (new_sid,)
            ).fetchone()
        if not cached:
            try:
                card = scryfall._request(f"/cards/{new_sid}")
                if card:
                    scryfall.cache_card(card)
                else:
                    flash("Printing not found on Scryfall.", "error")
                    return redirect(url_for("card_detail", name=card_name))
            except RuntimeError as e:
                flash(f"Scryfall error: {e}", "error")
                return redirect(url_for("card_detail", name=card_name))

    update_collection_entry(old_sid, old_fin, old_loc,
                            new_sid, new_fin, new_loc, new_qty)
    flash("Entry updated.", "success")
    return redirect(url_for("card_detail", name=card_name))


@app.route("/collection/mass-add", methods=["GET", "POST"])
def mass_add():
    """Add multiple cards at once without going through Scryfall search per card."""
    if request.method == "GET":
        return render_template("mass_add.html", locations=list_locations())

    locations = list_locations()
    results = []
    errors = []

    # The form sends parallel arrays: names[], quantities[], location_ids[], finishes[]
    names       = request.form.getlist("name")
    quantities  = request.form.getlist("quantity")
    loc_ids     = request.form.getlist("location_id")
    finishes    = request.form.getlist("finish")

    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        try:
            qty = int(quantities[i]) if i < len(quantities) else 1
            loc_id = int(loc_ids[i]) if i < len(loc_ids) else 1
            finish = finishes[i] if i < len(finishes) else "nonfoil"
        except (ValueError, IndexError):
            errors.append(f"Row {i+1}: bad quantity or location.")
            continue

        # Look up printings for this card name via Scryfall.
        try:
            prints = scryfall.search_printings(name)
        except RuntimeError as e:
            errors.append(f"{name}: Scryfall error — {e}")
            continue

        if not prints:
            errors.append(f"{name}: not found on Scryfall.")
            continue

        # Use the first (most recent) printing unless there's already a cached
        # version of this card — prefer the one that's already in our DB.
        chosen = None
        with get_conn() as conn:
            for p in prints:
                row = conn.execute(
                    "SELECT 1 FROM cards WHERE scryfall_id=?", (p["id"],)
                ).fetchone()
                if row:
                    chosen = p
                    break
        if not chosen:
            chosen = prints[0]

        sid = chosen["id"] if "id" in chosen else chosen["scryfall_id"]
        add_to_collection(sid, finish=finish, quantity=qty, location_id=loc_id)
        results.append({
            "name": chosen.get("name", name),
            "set_code": chosen.get("set", "?").upper(),
            "collector_number": chosen.get("collector_number", "?"),
            "qty": qty,
            "finish": finish,
            "location": next((l["name"] for l in locations if l["id"] == loc_id), "?"),
        })

    if results:
        flash(f"Added {len(results)} card(s) to collection.", "success")
    for e in errors:
        flash(e, "error")
    return render_template("mass_add.html", locations=locations,
                           results=results, errors=errors)


@app.route("/collection/remove", methods=["POST"])
def collection_remove():
    scryfall_id = request.form["scryfall_id"]
    finish = request.form["finish"]
    loc_id = int(request.form["location_id"])
    qty = int(request.form.get("quantity", "1"))
    name = request.form.get("card_name", "")
    new_qty = remove_from_collection(scryfall_id, finish, qty, loc_id)
    flash(f"Removed {qty}. Remaining: {new_qty}", "success")
    if name:
        return redirect(url_for("card_detail", name=name))
    return redirect(url_for("collection_browse"))


# ---------- Locations ----------

@app.route("/locations")
def locations_list():
    locs = list_locations(with_counts=True)
    # Add value column.
    for l in locs:
        rows = get_collection(location_id=l["id"])
        v = 0.0
        for r in rows:
            price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
            if price:
                v += price * r["quantity"]
        l["value"] = v
    return render_template("locations.html", locations=locs, kinds=KINDS)


@app.route("/locations/create", methods=["POST"])
def locations_create():
    name = request.form["name"].strip()
    kind = request.form.get("kind", "other")
    try:
        create_location(name, kind=kind)
        flash(f"Created '{name}'.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("locations_list"))


@app.route("/locations/<int:loc_id>")
def location_detail(loc_id):
    loc = get_location(loc_id)
    if not loc:
        abort(404)
    rows = get_collection(location_id=loc_id)
    # Attach image URIs
    if rows:
        with get_conn() as conn:
            ids = list({r["scryfall_id"] for r in rows})
            placeholders = ",".join("?" for _ in ids)
            img_map = {
                r["scryfall_id"]: r["image_uri"]
                for r in conn.execute(
                    f"SELECT scryfall_id, image_uri FROM cards "
                    f"WHERE scryfall_id IN ({placeholders})", ids
                ).fetchall()
            }
        for r in rows:
            r["image_uri"] = img_map.get(r["scryfall_id"])
    total = sum(r["quantity"] for r in rows)
    value = 0.0
    for r in rows:
        price = r["price_usd_foil"] if r["finish"] == "foil" else r["price_usd"]
        if price:
            value += price * r["quantity"]
    return render_template("location_detail.html",
                           loc=loc, rows=rows, total=total, value=value,
                           all_locations=list_locations())


@app.route("/locations/<int:loc_id>/rename", methods=["POST"])
def locations_rename(loc_id):
    new_name = request.form["name"].strip()
    if new_name:
        rename_location(loc_id, new_name)
        flash(f"Renamed to '{new_name}'.", "success")
    return redirect(url_for("locations_list"))


@app.route("/locations/<int:loc_id>/delete", methods=["POST"])
def locations_delete(loc_id):
    try:
        delete_location(loc_id)
        flash("Deleted (cards moved to Unsorted).", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("locations_list"))


@app.route("/locations/move", methods=["POST"])
def locations_move():
    scryfall_id = request.form["scryfall_id"]
    finish = request.form["finish"]
    src_id = int(request.form["src_id"])
    dst_id = int(request.form["dst_id"])
    qty = int(request.form["quantity"])
    try:
        move_cards(scryfall_id, finish, src_id, dst_id, qty)
        flash(f"Moved {qty}.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("location_detail", loc_id=src_id))


@app.route("/locations/backup")
def locations_backup():
    """Download a full snapshot of the database as a single backup file —
    collection, locations, price history, and saved treasure hunts all
    included."""
    data = backup_db_bytes()
    fname = f"cardvault_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    return send_file(
        io.BytesIO(data),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/locations/restore", methods=["POST"])
def locations_restore():
    """Restore the entire database from a previously downloaded backup file."""
    upload = request.files.get("backupfile")
    if not upload or not upload.filename:
        flash("Pick a backup file to restore.", "error")
        return redirect(url_for("locations_list"))
    data = upload.read()
    try:
        restore_db_bytes(data)
        flash("Backup restored. Your previous data was saved as a safety "
              "copy in the data folder, just in case.", "success")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Restore failed: {e}", "error")
    return redirect(url_for("locations_list"))


# ---------- Treasure Hunt ----------

@app.route("/treasure-hunt", methods=["GET", "POST"])
def treasure_hunt():
    if request.method == "GET":
        return render_template("treasure_hunt_form.html")
    text = request.form.get("cards", "")
    wanted = parse_card_list(text)
    if not wanted:
        flash("Couldn't parse any cards from that.", "error")
        return redirect(url_for("treasure_hunt"))
    plan = plan_treasure_hunt(wanted)
    # Attach image URIs for plan cards
    all_sids = {c["scryfall_id"]
                for cards in plan["by_location"].values()
                for c in cards}
    img_map = {}
    if all_sids:
        with get_conn() as conn:
            placeholders = ",".join("?" for _ in all_sids)
            img_map = {
                r["scryfall_id"]: r["image_uri"]
                for r in conn.execute(
                    f"SELECT scryfall_id, image_uri FROM cards "
                    f"WHERE scryfall_id IN ({placeholders})",
                    list(all_sids),
                ).fetchall()
            }
    for cards in plan["by_location"].values():
        for c in cards:
            c["image_uri"] = img_map.get(c["scryfall_id"])
    # Stash plan + wishlist in flask session-like storage via a job id, so the
    # export/remove buttons on the result page can reference it without a
    # round-trip through the form.
    job_id = _new_job()
    with _jobs_lock:
        _jobs[job_id]["plan"] = plan
        _jobs[job_id]["wanted"] = wanted
        _jobs[job_id]["status"] = "ready"
    return render_template("treasure_hunt_result.html",
                           plan=plan, wanted=wanted, plan_id=job_id,
                           total_unique=len(wanted))


@app.route("/treasure-hunt/<plan_id>/export")
def treasure_hunt_export(plan_id):
    with _jobs_lock:
        job = _jobs.get(plan_id)
    if not job or "plan" not in job:
        flash("That treasure hunt has expired. Generate a new one.", "error")
        return redirect(url_for("treasure_hunt"))
    text = render_plan_as_text(job["plan"], job["wanted"])
    # Save to disk too (the same folder the CLI uses)
    app_dir = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(app_dir, "Treasure Hunts")
    os.makedirs(export_dir, exist_ok=True)
    fname = f"treasure_hunt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path = os.path.join(export_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    # Also send as download to the browser
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/treasure-hunt/<plan_id>/remove", methods=["POST"])
def treasure_hunt_remove(plan_id):
    with _jobs_lock:
        job = _jobs.get(plan_id)
    if not job or "plan" not in job:
        flash("That treasure hunt has expired. Generate a new one.", "error")
        return redirect(url_for("treasure_hunt"))
    result = remove_planned_cards(job["plan"])
    msg = f"Removed {result['removed']} cards across {result['stacks_affected']} stacks."
    if result["stacks_emptied"]:
        msg += f" {result['stacks_emptied']} stack(s) emptied."
    if result["skipped"]:
        msg += f" {result['skipped']} skipped (state changed since plan)."
    flash(msg, "success")
    return redirect(url_for("home"))


# ---------- Prices ----------

@app.route("/prices")
def prices_page():
    v = collection_value()
    history = price_history(20)
    return render_template("prices.html", value=v, history=history)


@app.route("/prices/snapshot", methods=["POST"])
def prices_snapshot():
    v = snapshot_value()
    flash(f"Snapshot saved: ${v['total_usd']:,.2f}", "success")
    return redirect(url_for("prices_page"))


@app.route("/prices/refresh", methods=["POST"])
def prices_refresh():
    ids = all_collection_ids()
    if not ids:
        flash("Collection is empty.", "error")
        return redirect(url_for("prices_page"))
    job_id = _new_job()
    _jobs[job_id]["total"] = len(ids)

    def _worker():
        def _prog(done, total, msg):
            _job_update(job_id, current=done, total=total, message=msg)
        try:
            # Use the bulk endpoint
            result = scryfall.get_cards_collection(ids, progress=_prog)
            _job_update(job_id, status="done",
                        result={"updated": len(result["found"])},
                        message=f"Updated {len(result['found'])} cards.")
        except Exception as e:
            _job_update(job_id, status="error", message=str(e))

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("job_status", job_id=job_id,
                            done_url=url_for("prices_page")))


# ---------- Import ----------

@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import.html",
                               locations=list_locations(),
                               kinds=KINDS)

    # POST: handle upload
    upload = request.files.get("csvfile")
    if not upload or not upload.filename:
        flash("Pick a file to upload.", "error")
        return redirect(url_for("import_page"))

    # Either an existing location or a new one
    use_existing = request.form.get("location_choice") == "existing"
    if use_existing:
        loc_id = int(request.form["location_id"])
    else:
        new_name = request.form.get("new_location_name", "").strip()
        if not new_name:
            flash("Type a name for the new location.", "error")
            return redirect(url_for("import_page"))
        try:
            loc_id = create_location(new_name,
                                     kind=request.form.get("new_location_kind", "box"))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("import_page"))

    # Save upload to a temp file (importer needs a path)
    app_dir = os.path.dirname(os.path.abspath(__file__))
    tmpdir = os.path.join(app_dir, ".uploads")
    os.makedirs(tmpdir, exist_ok=True)
    tmp_path = os.path.join(tmpdir, f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    upload.save(tmp_path)

    job_id = _new_job()

    def _worker():
        def _prog(done, total, msg):
            _job_update(job_id, current=done, total=total, message=msg)
        try:
            result = import_manabox_csv(tmp_path, location_id=loc_id,
                                        progress=_prog)
            _job_update(job_id, status="done", result=result,
                        message=f"Imported {result['imported']} / {result['total_rows']} rows.")
        except Exception as e:
            _job_update(job_id, status="error", message=str(e))
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("job_status", job_id=job_id,
                            done_url=url_for("home")))


# ---------- Background job status ----------

@app.route("/job/<job_id>")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return render_template("job_status.html", job_id=job_id, job=job,
                           done_url=request.args.get("done_url", "/"))


@app.route("/job/<job_id>/json")
def job_status_json(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": job["status"],
        "current": job["current"],
        "total": job["total"],
        "message": job["message"],
        "result": job["result"],
    })


# ---------------- bootstrap ----------------

def _local_ip():
    """Best-effort local IP for the 'open from your phone' hint."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    init_db()
    ip = _local_ip()
    port = 5000
    print()
    print("=" * 60)
    print("  CardVault Web — running")
    print("=" * 60)
    print(f"  On this machine:  http://localhost:{port}")
    print(f"  On the network:   http://{ip}:{port}")
    print(f"  (open the second URL on your phone if it's on the same Wi-Fi)")
    print()
    print("  Press Ctrl-C to stop.")
    print()
    app.run(host="0.0.0.0", port=port, debug=False)
