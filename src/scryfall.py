"""
Scryfall API client.

Uses only the standard library (urllib) — no external dependencies.

Scryfall asks for:
  - A descriptive User-Agent
  - At least 50-100ms between requests
  - Caching when possible

We satisfy all three. Cards we've already fetched are stored in the local DB
and re-used. Network calls only happen on cache misses or explicit refresh.
"""

import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from .db import get_conn

SCRYFALL_API = "https://api.scryfall.com"
USER_AGENT = "CardVault/1.0 (local collection manager)"
# Scryfall's docs ask for 50-100ms; in practice their rate limiter is
# stricter when you sustain a burst. 150ms gives a safer floor.
REQUEST_DELAY = 0.15
MAX_RETRIES = 4  # retry budget for 429 / transient 5xx

_last_request_time = 0.0


def _rate_limit():
    """Block until at least REQUEST_DELAY has passed since the last call."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request_time = time.time()


def _request(path, params=None, method="GET", body=None):
    """
    Make a request against the Scryfall API.

    - Returns parsed JSON, or None on 404.
    - Retries on 429 (rate limit) and 5xx with exponential backoff.
    - For POST, pass `body` as a dict; it'll be JSON-encoded.
    """
    url = f"{SCRYFALL_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        _rate_limit()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            # Retry on rate limit / transient server errors
            if e.code == 429 or 500 <= e.code < 600:
                # Honor Retry-After if Scryfall sent one
                retry_after = None
                try:
                    retry_after = float(e.headers.get("Retry-After", "0"))
                except (TypeError, ValueError):
                    retry_after = None
                wait = retry_after if retry_after else (2 ** attempt)
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
                    last_err = e
                    continue
            raise RuntimeError(f"Scryfall HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise RuntimeError(f"Network error contacting Scryfall: {e.reason}")
    # Shouldn't reach here, but be safe
    raise RuntimeError(f"Scryfall request failed after retries: {last_err}")


def _row_from_scryfall(card):
    """Flatten a Scryfall card JSON object into the columns of our `cards` table."""
    # Double-faced cards have image_uris under card_faces[0].
    image_uri = None
    if "image_uris" in card:
        image_uri = card["image_uris"].get("normal")
    elif "card_faces" in card and card["card_faces"]:
        face = card["card_faces"][0]
        if "image_uris" in face:
            image_uri = face["image_uris"].get("normal")

    prices = card.get("prices", {}) or {}
    return {
        "scryfall_id": card["id"],
        "oracle_id": card.get("oracle_id", ""),
        "name": card["name"],
        "set_code": card["set"],
        "set_name": card["set_name"],
        "collector_number": card["collector_number"],
        "rarity": card.get("rarity"),
        "mana_cost": card.get("mana_cost", ""),
        "type_line": card.get("type_line", ""),
        "oracle_text": card.get("oracle_text", ""),
        "colors": ",".join(card.get("colors") or []),
        "color_identity": ",".join(card.get("color_identity") or []),
        "image_uri": image_uri,
        "scryfall_uri": card.get("scryfall_uri", ""),
        "price_usd": _to_float(prices.get("usd")),
        "price_usd_foil": _to_float(prices.get("usd_foil")),
        "price_eur": _to_float(prices.get("eur")),
        "price_tix": _to_float(prices.get("tix")),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cache_card(card_json):
    """Store/update a Scryfall card record in the local DB."""
    row = _row_from_scryfall(card_json)
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "scryfall_id")
    sql = (
        f"INSERT INTO cards ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(scryfall_id) DO UPDATE SET {updates}"
    )
    with get_conn() as conn:
        conn.execute(sql, [row[c] for c in cols])
    return row


def search_printings(name, exact=True):
    """
    Return all printings of a card name.

    Uses Scryfall's named-search to resolve the canonical name (handles
    fuzzy matches), then fetches all printings via the prints_search_uri.
    """
    params = {"exact" if exact else "fuzzy": name}
    named = _request("/cards/named", params=params)
    if named is None:
        if exact:
            # Fall back to fuzzy.
            return search_printings(name, exact=False)
        return []

    # Fetch all printings for this oracle card.
    prints_uri = named.get("prints_search_uri")
    if not prints_uri:
        cache_card(named)
        return [named]

    # prints_search_uri is a full URL; strip the host to reuse _request.
    parsed = urllib.parse.urlparse(prints_uri)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    # parse_qs gives lists; flatten.
    flat_query = {k: v[0] for k, v in query.items()}

    all_prints = []
    page_path = path
    page_query = flat_query
    while True:
        page = _request(page_path, params=page_query)
        if not page:
            break
        for card in page.get("data", []):
            cache_card(card)
            all_prints.append(card)
        if not page.get("has_more"):
            break
        next_uri = page.get("next_page")
        if not next_uri:
            break
        parsed = urllib.parse.urlparse(next_uri)
        page_path = parsed.path
        page_query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    return all_prints


def get_card_by_id(scryfall_id, refresh=False):
    """Get a single printing, from cache unless refresh=True."""
    if not refresh:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM cards WHERE scryfall_id = ?", (scryfall_id,)
            ).fetchone()
            if row:
                return dict(row)
    card = _request(f"/cards/{scryfall_id}")
    if card is None:
        return None
    return _row_from_scryfall(card) if cache_card(card) else None


def refresh_prices(scryfall_ids):
    """Re-fetch a batch of cards to update their prices. Returns count updated."""
    updated = 0
    for sid in scryfall_ids:
        card = _request(f"/cards/{sid}")
        if card:
            cache_card(card)
            updated += 1
    return updated


def get_cards_collection(scryfall_ids, progress=None):
    """
    Bulk-fetch cards by Scryfall ID using POST /cards/collection.

    Scryfall accepts up to 75 identifiers per request. This is much faster and
    friendlier than 1-at-a-time GETs — for 240 cards it's 4 requests instead of 240.

    Args:
        scryfall_ids: iterable of Scryfall UUIDs
        progress: optional callback(fetched_so_far, total, message)

    Returns:
        {
            "found":      list of card JSON objects (also cached to DB),
            "not_found":  list of scryfall_ids the server didn't recognize,
        }
    """
    ids = list(scryfall_ids)
    found = []
    not_found = []
    total = len(ids)
    batch_size = 75

    for batch_start in range(0, total, batch_size):
        batch = ids[batch_start:batch_start + batch_size]
        identifiers = [{"id": sid} for sid in batch]
        if progress:
            progress(batch_start, total, f"Fetching {len(batch)} cards...")
        try:
            resp = _request(
                "/cards/collection",
                method="POST",
                body={"identifiers": identifiers},
            )
        except RuntimeError as e:
            # On a hard failure for one batch, record the IDs and keep going.
            for sid in batch:
                not_found.append(sid)
            if progress:
                progress(batch_start + len(batch), total,
                         f"Batch failed: {e}")
            continue
        if not resp:
            for sid in batch:
                not_found.append(sid)
            continue
        for card in resp.get("data", []):
            cache_card(card)
            found.append(card)
        # not_found in the response is a list of the identifier objects we sent
        # that didn't match anything. Pull the id back out.
        for nf in resp.get("not_found", []):
            if isinstance(nf, dict) and "id" in nf:
                not_found.append(nf["id"])
        if progress:
            progress(min(batch_start + batch_size, total), total,
                     f"Got {len(found)} so far")

    return {"found": found, "not_found": not_found}
