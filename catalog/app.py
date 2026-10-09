"""NordEvent Lab - KATALOOG (catalog)

Vastutus: ürituste avalik otsing ja detailvaade.
Andmeomand: ürituste info. Saadavust (vabu kohti) EI oma - selle omanik on broneerimine.

Olulised seaded (vt .env):
  CATALOG_DATA_VERSION          kohustuslik; puudumisel teenus ei käivitu
  CATALOG_WORK_MS               protsessoriaeg (ms) ühe päringu kohta; kalibreeritakse käivitusel
  CATALOG_AVAILABILITY_SOURCE   api | shared_db | none  - kust detailvaade saadavuse võtab
  CATALOG_AVAILABILITY_TIMEOUT_MS  kui kaua oodatakse broneerimisteenuse vastust
"""
import hashlib
import time
import sqlite3
import threading

from nelib import (App, HttpError, INSTANCE, Latency, UpstreamTimeout, env_int, env_str,
                   http_json, log)

DATA_VERSION = env_str("CATALOG_DATA_VERSION", required=True)
WORK_MS = env_int("CATALOG_WORK_MS", 15)        # protsessoriaeg ühe päringu kohta (ms)
SOURCE = env_str("CATALOG_AVAILABILITY_SOURCE", "api")
BOOKING_URL = env_str("BOOKING_URL", "http://booking:8000")
AVAIL_TIMEOUT = env_int("CATALOG_AVAILABILITY_TIMEOUT_MS", 800) / 1000.0
SHARED_DB = env_str("BOOKING_DB_PATH", "/shared/booking.db")

if SOURCE not in ("api", "shared_db", "none"):
    log("ERROR", "config_invalid", variable="CATALOG_AVAILABILITY_SOURCE", value=SOURCE)
    raise SystemExit(1)

EVENTS = [
    {"id": "soor-2027", "name": "Festival Sõõr 2027", "city": "tallinn", "date": "2027-06-16",
     "venue": "Lauluväljak", "ticket_types": ["GA", "VIP"]},
    {"id": "jazz-2026", "name": "Sügisjazz 2026", "city": "tartu", "date": "2026-11-20",
     "venue": "Vanemuise kontserdimaja", "ticket_types": ["GA"]},
    {"id": "rock-2027", "name": "Rannarock 2027", "city": "parnu", "date": "2027-07-24",
     "venue": "Vallikäär", "ticket_types": ["GA", "VIP"]},
]
BY_ID = {e["id"]: e for e in EVENTS}

def calibrate():
    """Mõõdab masina kiiruse, et üks päring maksaks igal masinal ~WORK_MS ms protsessoriaega.
    Väikesed mõõtetükid (~1 ms) ei jää konteineri CPU-piiri (cgroup quota) taha."""
    best = float("inf")
    for _ in range(30):
        t = time.perf_counter()
        burn(1000)
        best = min(best, time.perf_counter() - t)
    per_unit_ms = best * 1000 / 1000
    return max(100, int(WORK_MS / per_unit_ms)), per_unit_ms


lat = Latency()
counters = {"requests": 0, "availability_degraded": 0, "shared_db_errors": 0}
c_lock = threading.Lock()


def burn(units):
    """Simuleerib otsingu/renderduse protsessoritööd. Sama töö hulk igal päringul."""
    h = b"nordevent"
    for _ in range(units):
        h = hashlib.sha256(h).digest()


def count(key):
    with c_lock:
        counters[key] += 1


def availability(event_id, rid):
    if SOURCE == "none":
        return None, "none"
    if SOURCE == "api":
        try:
            st, d = http_json("GET", f"{BOOKING_URL}/availability/{event_id}",
                              headers={"X-Request-ID": rid}, timeout=AVAIL_TIMEOUT)
            if st == 200:
                return d["ticket_types"], "booking_api"
            log("WARN", "availability_degraded", rid, reason=f"http_{st}")
        except UpstreamTimeout:
            log("WARN", "availability_degraded", rid, reason="timeout",
                timeout_ms=int(AVAIL_TIMEOUT * 1000))
        except ConnectionError as e:
            log("WARN", "availability_degraded", rid, reason="unreachable", error=str(e))
        count("availability_degraded")
        return None, "degraded"
    # SOURCE == shared_db: loeme otse broneerimise andmebaasi faili (jagatud andmebaas!)
    try:
        con = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute("SELECT ticket_type, remaining FROM inventory WHERE event_id = ?",
                           (event_id,)).fetchall()
        con.close()
        return [{"ticket_type": r[0], "remaining": r[1]} for r in rows], "shared_db"
    except sqlite3.Error as e:
        count("shared_db_errors")
        log("ERROR", "shared_db_read_failed", rid, error=str(e),
            message="Kataloog loeb broneerimise tabelit otse - skeemi muutus murdis kataloogi")
        raise HttpError(500, "availability_source_failed", detail=str(e))


_override = env_int("CATALOG_WORK_UNITS", 0)
if _override > 0:
    WORK_UNITS, PER_UNIT_MS = _override, None
else:
    WORK_UNITS, PER_UNIT_MS = calibrate()

app = App()


@app.route("GET", "/health")
def health(req):
    return 200, {"status": "ok", "instance": INSTANCE, "data_version": DATA_VERSION}


@app.route("GET", "/events")
def list_events(req):
    count("requests")
    t0 = time.perf_counter()
    burn(WORK_UNITS)
    q = (req.query.get("query") or "").lower()
    city = (req.query.get("city") or "").lower()
    res = [e for e in EVENTS if (not q or q in e["id"] or q in e["name"].lower())
           and (not city or city == e["city"])]
    lat.add((time.perf_counter() - t0) * 1000)
    return 200, {"served_by": INSTANCE, "data_version": DATA_VERSION, "events": res}


@app.route("GET", "/events/<event_id>")
def event_detail(req, event_id):
    count("requests")
    t0 = time.perf_counter()
    burn(WORK_UNITS)
    ev = BY_ID.get(event_id)
    if not ev:
        raise HttpError(404, "event_not_found")
    avail, src = availability(event_id, req.rid)
    lat.add((time.perf_counter() - t0) * 1000)
    out = dict(ev)
    out.update({"served_by": INSTANCE, "availability": avail, "availability_source": src})
    if src == "degraded":
        out["availability_note"] = "Saadavus on hetkel teadmata - ostu saab siiski alustada"
    return 200, out


@app.route("GET", "/stats")
def stats(req):
    with c_lock:
        c = dict(counters)
    return 200, {"service": "catalog", "instance": INSTANCE, "data_version": DATA_VERSION,
                 "availability_source": SOURCE, "work_ms_target": WORK_MS, "work_units": WORK_UNITS,
                 "latency_ms": lat.summary(), "counters": c,
                 "note": "Iga koopia (instance) loeb ainult enda päringuid"}


if __name__ == "__main__":
    log("INFO", "config_loaded", data_version=DATA_VERSION, work_ms=WORK_MS, work_units=WORK_UNITS,
        calibrated_unit_us=round(PER_UNIT_MS * 1000, 2) if PER_UNIT_MS else None,
        availability_source=SOURCE, availability_timeout_ms=int(AVAIL_TIMEOUT * 1000))
    if SOURCE == "shared_db":
        log("WARN", "shared_database_coupling",
            message="Kataloog loeb broneerimise andmebaasi otse (jagatud andmebaas)")
    app.serve(8000)
