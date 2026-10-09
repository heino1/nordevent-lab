"""NordEvent Lab - BRONEERIMINE (booking)

Vastutus: piletite kinnihoidmine, tellimuse olekumudel, maksetulemuse käsitlemine,
pileti väljastamine. Andmeomand: saadavus (inventory) ja tellimused (orders).

Tellimuse olekud:
  RESERVED -> PENDING_PAYMENT -> PAID
           -> PAYMENT_UNKNOWN (maksepartner ei vastanud ajalimiidi jooksul) -> PAID / EXPIRED
           -> FAILED          (makse ebaõnnestus)
           -> EXPIRED         (broneering aegus enne makset)
  EXPIRED  -> LATE_PAYMENT    (makse saabus pärast aegumist - vajab tagasimakset)

Olulised seaded (vt .env): IDEMPOTENCY, PAY_TIMEOUT_MS, PAY_RETRIES, PAY_BACKOFF,
RESERVATION_TTL_S, NOTIFY_MODE (async|sync), OUTBOX, BOOKING_SCHEMA (v1|v2)
"""
import random
import sqlite3
import threading
import time
import uuid

from nelib import (App, HttpError, INSTANCE, Latency, UpstreamTimeout, env_bool, env_int,
                   env_str, http_json, log, read_secret)

DB_PATH = env_str("BOOKING_DB_PATH", "/data/booking.db")
SCHEMA = env_str("BOOKING_SCHEMA", "v1")
IDEMPOTENCY = env_bool("IDEMPOTENCY", True)
PAY_URL = env_str("PAY_URL", "http://paybaltic:8000")
PAY_TIMEOUT = env_int("PAY_TIMEOUT_MS", 2000) / 1000.0
PAY_RETRIES = env_int("PAY_RETRIES", 0)
PAY_BACKOFF = env_str("PAY_BACKOFF", "fixed")  # fixed | exponential
TTL = env_int("RESERVATION_TTL_S", 60)
NOTIFY_MODE = env_str("NOTIFY_MODE", "async")  # async | sync
NOTIFY_URL = env_str("NOTIFY_URL", "http://notification:8000")
NOTIFY_TIMEOUT = env_int("NOTIFY_TIMEOUT_MS", 2000) / 1000.0
QUEUE_URL = env_str("QUEUE_URL", "http://queue:8000")
OUTBOX = env_bool("OUTBOX", False)
CALLBACK_URL = env_str("CALLBACK_URL", "http://booking:8000/payments/callback")
PRICES = {"GA": 59, "VIP": 149}
SEED = [("soor-2027", "GA", 1500), ("soor-2027", "VIP", 100), ("jazz-2026", "GA", 150),
        ("rock-2027", "GA", 300), ("rock-2027", "VIP", 30)]

API_KEY, KEY_SOURCE = read_secret("PAYBALTIC_API_KEY")

# Skeem v1: inventory(remaining); v2: ticket_inventory(remaining_qty) - simuleerib omaniku
# sisemist skeemimuudatust, mis murrab kõik, kes loevad tabelit otse.
INV_T, REM_C, INI_C = ("inventory", "remaining", "initial") if SCHEMA == "v1" else \
    ("ticket_inventory", "remaining_qty", "initial_qty")

lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=5)
db.execute("PRAGMA journal_mode=WAL")
db.row_factory = sqlite3.Row
res_lat = Latency()
counters = {"notify_failures": 0, "duplicate_callbacks": 0, "idempotent_replays": 0,
            "payment_timeouts": 0, "payment_retries": 0, "late_payments": 0,
            "events_lost": 0}


def bump(k, n=1):
    with lock:
        counters[k] += n


def init_db():
    with lock:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        other_t = "ticket_inventory" if INV_T == "inventory" else "inventory"
        if other_t in tables and INV_T not in tables:
            o_rem, o_ini = ("remaining_qty", "initial_qty") if other_t == "ticket_inventory" \
                else ("remaining", "initial")
            db.execute(f"ALTER TABLE {other_t} RENAME TO {INV_T}")
            db.execute(f"ALTER TABLE {INV_T} RENAME COLUMN {o_rem} TO {REM_C}")
            db.execute(f"ALTER TABLE {INV_T} RENAME COLUMN {o_ini} TO {INI_C}")
            log("WARN", "schema_migrated", to_schema=SCHEMA, table=INV_T,
                message="Broneerimine muutis oma sisemist andmeskeemi")
            tables.add(INV_T)
        if INV_T not in tables:
            db.execute(f"CREATE TABLE {INV_T} (event_id TEXT, ticket_type TEXT, {REM_C} INTEGER,"
                       f" {INI_C} INTEGER, PRIMARY KEY(event_id, ticket_type))")
            for e, t, n in SEED:
                db.execute(f"INSERT INTO {INV_T} VALUES (?,?,?,?)", (e, t, n, n))
        db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, idem_key TEXT, customer TEXT, event_id TEXT, ticket_type TEXT,
            qty INTEGER, amount INTEGER, status TEXT, created_at REAL, expires_at REAL,
            updated_at REAL, paid_at REAL, payment_id TEXT, tickets_issued INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0)""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_idem ON orders(idem_key)")
        db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, payload TEXT, created_at REAL,
            sent_at REAL)""")


MESSAGES = {
    "RESERVED": "Pilet on ajutiselt kinni hoitud",
    "PENDING_PAYMENT": "Ootame makse kinnitust",
    "PAYMENT_UNKNOWN": "Makse kinnitamisel - palun ära alusta uut ostu",
    "PAID": "Makstud, pilet on väljastatud",
    "FAILED": "Makse ebaõnnestus, pilet vabastati",
    "EXPIRED": "Broneering aegus, pilet vabastati",
    "LATE_PAYMENT": "Makse saabus pärast broneeringu aegumist - vajab tagasimakset",
}


def order_dict(row):
    d = dict(row)
    d["customer_message"] = MESSAGES.get(d["status"], d["status"])
    return d


def get_order(oid):
    with lock:
        r = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return r


def set_status(oid, status, **cols):
    sets = ", ".join(["status=?", "updated_at=?"] + [f"{k}=?" for k in cols])
    with lock:
        db.execute(f"UPDATE orders SET {sets} WHERE id=?",
                   [status, time.time()] + list(cols.values()) + [oid])


def release(row):
    with lock:
        db.execute(f"UPDATE {INV_T} SET {REM_C}={REM_C}+? WHERE event_id=? AND ticket_type=?",
                   (row["qty"], row["event_id"], row["ticket_type"]))


def backoff(attempt):
    base = 0.3
    d = base if PAY_BACKOFF == "fixed" else base * (2 ** attempt)
    return d + (random.uniform(0, d) if PAY_BACKOFF == "exponential" else 0)


# ------------------------------------------------------------- teavitamine
def publish_order_paid(row, rid):
    msg = {"order_id": row["id"], "customer": row["customer"], "event_id": row["event_id"],
           "qty": row["qty"], "paid_at": row["paid_at"] or time.time(), "request_id": rid}
    if NOTIFY_MODE == "sync":
        try:
            st, _ = http_json("POST", f"{NOTIFY_URL}/send", msg, {"X-Request-ID": rid},
                              timeout=NOTIFY_TIMEOUT)
            if st != 200:
                raise ConnectionError(f"http_{st}")
        except (UpstreamTimeout, ConnectionError) as e:
            bump("notify_failures")
            log("ERROR", "notification_failed", rid, order_id=row["id"], mode="sync",
                error=str(e), message="Sünkroonne teavitus ebaõnnestus - makse callback saab vea")
            raise HttpError(502, "notification_failed")
        with lock:
            db.execute("UPDATE orders SET notified=notified+1 WHERE id=?", (row["id"],))
        return
    # async
    if OUTBOX:
        import json
        with lock:
            db.execute("INSERT INTO outbox(topic,payload,created_at) VALUES(?,?,?)",
                       ("order-paid", json.dumps(msg), time.time()))
        log("INFO", "outbox_written", rid, order_id=row["id"])
        return
    try:
        st, _ = http_json("POST", f"{QUEUE_URL}/queues/order-paid/messages", {"body": msg},
                          {"X-Request-ID": rid}, timeout=1.0)
        if st not in (200, 201):
            raise ConnectionError(f"http_{st}")
        with lock:
            db.execute("UPDATE orders SET notified=notified+1 WHERE id=?", (row["id"],))
        log("INFO", "event_published", rid, topic="order-paid", order_id=row["id"])
    except (UpstreamTimeout, ConnectionError) as e:
        bump("events_lost")
        log("ERROR", "event_publish_failed", rid, topic="order-paid", order_id=row["id"],
            error=str(e), message="Sündmus läks kaduma: tellimus on PAID, kuid teavitust ei tule")


def outbox_relay():
    import json
    while True:
        time.sleep(1)
        with lock:
            rows = db.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 50"
                              ).fetchall()
        for r in rows:
            msg = json.loads(r["payload"])
            try:
                st, _ = http_json("POST", f"{QUEUE_URL}/queues/{r['topic']}/messages",
                                  {"body": msg}, {"X-Request-ID": msg.get("request_id")},
                                  timeout=1.0)
                if st not in (200, 201):
                    raise ConnectionError(f"http_{st}")
            except (UpstreamTimeout, ConnectionError):
                log("WARN", "outbox_relay_retry_later", outbox_id=r["id"])
                break
            with lock:
                db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (time.time(), r["id"]))
                db.execute("UPDATE orders SET notified=notified+1 WHERE id=?", (msg["order_id"],))
            log("INFO", "event_published", msg.get("request_id"), topic=r["topic"],
                order_id=msg["order_id"], via="outbox")


def expiry_sweeper():
    while True:
        time.sleep(2)
        now = time.time()
        with lock:
            rows = db.execute("SELECT * FROM orders WHERE status IN "
                              "('RESERVED','PENDING_PAYMENT','PAYMENT_UNKNOWN') AND expires_at<?",
                              (now,)).fetchall()
            for r in rows:
                db.execute("UPDATE orders SET status='EXPIRED', updated_at=? WHERE id=?",
                           (now, r["id"]))
                release(r)
        for r in rows:
            log("INFO", "reservation_expired", order_id=r["id"], previous_status=r["status"])


# ------------------------------------------------------------------ API
app = App()


@app.route("GET", "/health")
def health(req):
    with lock:
        db.execute("SELECT 1")
    return 200, {"status": "ok", "instance": INSTANCE}


@app.route("GET", "/availability/<event_id>")
def availability_api(req, event_id):
    with lock:
        rows = db.execute(f"SELECT ticket_type, {REM_C} FROM {INV_T} WHERE event_id=?",
                          (event_id,)).fetchall()
    if not rows:
        raise HttpError(404, "event_not_found")
    return 200, {"event_id": event_id,
                 "ticket_types": [{"ticket_type": r[0], "remaining": r[1]} for r in rows]}


@app.route("POST", "/reservations")
def reserve(req):
    t0 = time.perf_counter()
    rid = req.rid
    b = req.body or {}
    key = req.headers.get("Idempotency-Key")
    if IDEMPOTENCY and key:
        with lock:
            ex = db.execute("SELECT * FROM orders WHERE idem_key=?", (key,)).fetchone()
        if ex:
            bump("idempotent_replays")
            log("INFO", "idempotent_replay", rid, order_id=ex["id"], idempotency_key=key,
                message="Korduspäring - tagastan olemasoleva tellimuse, uut ei loo")
            return 200, dict(order_dict(ex), replayed=True)
    ev, tt = b.get("event_id"), b.get("ticket_type", "GA")
    qty = int(b.get("qty", 1))
    if not ev or qty < 1 or qty > 6:
        raise HttpError(400, "invalid_request")
    oid = "ORD-" + uuid.uuid4().hex[:8].upper()
    now = time.time()
    with lock:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"SELECT {REM_C} FROM {INV_T} WHERE event_id=? AND ticket_type=?",
                         (ev, tt)).fetchone()
        if row is None or row[0] < qty:
            db.execute("ROLLBACK")
            log("INFO", "sold_out", rid, event_id=ev, ticket_type=tt)
            raise HttpError(409, "sold_out")
        db.execute(f"UPDATE {INV_T} SET {REM_C}={REM_C}-? WHERE event_id=? AND ticket_type=?",
                   (qty, ev, tt))
        db.execute("INSERT INTO orders(id,idem_key,customer,event_id,ticket_type,qty,amount,status,"
                   "created_at,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, key if IDEMPOTENCY else None, b.get("customer"), ev, tt, qty,
                    qty * PRICES.get(tt, 50), "RESERVED", now, now + TTL, now))
        db.execute("COMMIT")
    log("INFO", "reservation_created", rid, order_id=oid, event_id=ev, ticket_type=tt, qty=qty)

    pay_key = oid if IDEMPOTENCY else None
    timed_out, outcome = False, None
    for attempt in range(PAY_RETRIES + 1):
        if attempt:
            bump("payment_retries")
        try:
            st, d = http_json("POST", f"{PAY_URL}/payments",
                              {"order_id": oid, "amount": qty * PRICES.get(tt, 50),
                               "callback_url": CALLBACK_URL},
                              {"X-Request-ID": rid, "X-Api-Key": API_KEY,
                               "Idempotency-Key": pay_key or uuid.uuid4().hex},
                              timeout=PAY_TIMEOUT)
            if st in (200, 201, 202):
                with lock:  # callback võis juba enne vastust saabuda - ära kirjuta PAID üle
                    db.execute("UPDATE orders SET status='PENDING_PAYMENT', payment_id=?, "
                               "updated_at=? WHERE id=? AND status IN ('RESERVED','PAYMENT_UNKNOWN')",
                               (d.get("payment_id"), time.time(), oid))
                outcome = "PENDING_PAYMENT"
                break
            if st == 401:
                log("ERROR", "payment_auth_failed", rid, order_id=oid,
                    message="Maksepartner ei aktsepteeri API võtit")
                outcome = "FAILED"
                break
            log("WARN", "payment_initiation_failed", rid, order_id=oid, http_status=st,
                attempt=attempt + 1)
        except UpstreamTimeout:
            timed_out = True
            bump("payment_timeouts")
            log("WARN", "payment_timeout", rid, order_id=oid, gateway="PayBaltic",
                timeout_ms=int(PAY_TIMEOUT * 1000), attempt=attempt + 1)
        except ConnectionError as e:
            log("WARN", "payment_unreachable", rid, order_id=oid, error=str(e),
                attempt=attempt + 1)
        if attempt < PAY_RETRIES:
            time.sleep(backoff(attempt))
    if outcome is None:
        outcome = "PAYMENT_UNKNOWN" if timed_out else "FAILED"
        r = get_order(oid)
        if r["status"] == "RESERVED" and outcome == "PAYMENT_UNKNOWN":
            set_status(oid, outcome)  # callback võib hiljem siiski saabuda
    if outcome == "FAILED":
        cur = get_order(oid)
        if cur["status"] in ("RESERVED", "PAYMENT_UNKNOWN"):
            set_status(oid, "FAILED")
            release(cur)
    if outcome == "PAYMENT_UNKNOWN":
        log("WARN", "order_payment_unknown", rid, order_id=oid,
            message="Kliendile kuvatakse 'makse kinnitamisel'")
    res_lat.add((time.perf_counter() - t0) * 1000)
    final = get_order(oid)
    return 202, order_dict(final)


@app.route("POST", "/payments/callback")
def callback(req):
    rid = req.rid
    b = req.body or {}
    oid, result = b.get("order_id"), b.get("result")
    r = get_order(oid)
    if not r:
        raise HttpError(404, "order_not_found")
    if r["status"] == "PAID":
        bump("duplicate_callbacks")
        if IDEMPOTENCY:
            log("INFO", "duplicate_callback_ignored", rid, order_id=oid,
                payment_id=b.get("payment_id"))
            return 200, {"order_id": oid, "status": "PAID", "duplicate": True}
        log("WARN", "duplicate_callback_processed", rid, order_id=oid,
            message="Topelt-callback töödeldi uuesti: pilet väljastati teist korda")
        with lock:
            db.execute("UPDATE orders SET tickets_issued=tickets_issued+qty WHERE id=?", (oid,))
        publish_order_paid(get_order(oid), rid)
        return 200, {"order_id": oid, "status": "PAID", "duplicate": True}
    if r["status"] in ("EXPIRED", "LATE_PAYMENT"):
        if result == "success":
            bump("late_payments")
            set_status(oid, "LATE_PAYMENT")
            log("WARN", "late_payment_after_expiry", rid, order_id=oid,
                message="Raha võeti, aga broneering oli aegunud - vajab kompensatsiooni")
        return 200, {"order_id": oid, "status": get_order(oid)["status"]}
    if r["status"] == "FAILED":
        log("WARN", "callback_for_failed_order", rid, order_id=oid, result=result)
        return 200, {"order_id": oid, "status": "FAILED"}
    if result == "success":
        now = time.time()
        with lock:
            db.execute("UPDATE orders SET status='PAID', paid_at=?, updated_at=?, payment_id=?,"
                       " tickets_issued=tickets_issued+qty WHERE id=?",
                       (now, now, b.get("payment_id"), oid))
        log("INFO", "order_paid", rid, order_id=oid, previous_status=r["status"])
        publish_order_paid(get_order(oid), rid)
        return 200, {"order_id": oid, "status": "PAID"}
    set_status(oid, "FAILED")
    release(r)
    log("INFO", "order_payment_failed", rid, order_id=oid)
    return 200, {"order_id": oid, "status": "FAILED"}


@app.route("GET", "/orders/<oid>")
def order(req, oid):
    r = get_order(oid)
    if not r:
        raise HttpError(404, "order_not_found")
    return 200, order_dict(r)


@app.route("GET", "/stats")
def stats(req):
    now = time.time()
    with lock:
        by = {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")}
        oldest = db.execute("SELECT MIN(created_at) FROM orders WHERE status IN "
                            "('PENDING_PAYMENT','PAYMENT_UNKNOWN')").fetchone()[0]
        dup_tickets = db.execute("SELECT COALESCE(SUM(tickets_issued-qty),0) FROM orders "
                                 "WHERE tickets_issued>qty").fetchone()[0]
        issued = db.execute("SELECT COALESCE(SUM(tickets_issued),0) FROM orders").fetchone()[0]
        dup_cust = db.execute("SELECT COUNT(*) FROM (SELECT customer FROM orders WHERE customer IS "
                              "NOT NULL AND status IN ('PAID','PENDING_PAYMENT','PAYMENT_UNKNOWN',"
                              "'RESERVED') GROUP BY customer, event_id HAVING COUNT(*)>1)"
                              ).fetchone()[0]
        inv = [dict(ticket_type=r[0] + "/" + r[1], remaining=r[2], initial=r[3]) for r in
               db.execute(f"SELECT event_id, ticket_type, {REM_C}, {INI_C} FROM {INV_T}")]
        outbox_pending = db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL"
                                    ).fetchone()[0]
        c = dict(counters)
    return 200, {
        "service": "booking", "instance": INSTANCE,
        "orders_by_status": by,
        "oldest_open_payment_age_s": round(now - oldest, 1) if oldest else 0,
        "tickets_issued_total": issued,
        "duplicate_tickets_issued": dup_tickets,
        "customers_with_multiple_open_orders": dup_cust,
        "reservation_latency_ms": res_lat.summary(),
        "inventory": inv,
        "outbox_pending": outbox_pending,
        "counters": c,
        "config": {"IDEMPOTENCY": IDEMPOTENCY, "PAY_TIMEOUT_MS": int(PAY_TIMEOUT * 1000),
                   "PAY_RETRIES": PAY_RETRIES, "PAY_BACKOFF": PAY_BACKOFF,
                   "RESERVATION_TTL_S": TTL, "NOTIFY_MODE": NOTIFY_MODE, "OUTBOX": OUTBOX,
                   "BOOKING_SCHEMA": SCHEMA, "api_key_source": KEY_SOURCE},
    }


if __name__ == "__main__":
    init_db()
    log("INFO", "config_loaded", idempotency=IDEMPOTENCY, pay_timeout_ms=int(PAY_TIMEOUT * 1000),
        pay_retries=PAY_RETRIES, pay_backoff=PAY_BACKOFF, reservation_ttl_s=TTL,
        notify_mode=NOTIFY_MODE, outbox=OUTBOX, schema=SCHEMA, api_key_source=KEY_SOURCE)
    if API_KEY is None:
        log("ERROR", "secret_missing", secret="PAYBALTIC_API_KEY",
            message="Makseliidese võti puudub - maksed ebaõnnestuvad")
    threading.Thread(target=expiry_sweeper, daemon=True).start()
    if OUTBOX:
        threading.Thread(target=outbox_relay, daemon=True).start()
    app.serve(8000)
