"""NordEvent Lab - KOORMUSGENERAATOR ("müügi avanemine")

Simuleerib külastajaid: kataloogi otsingud ja detailvaated ning broneerimiskatsed.
Profiilid (LOAD_PROFILE):
  calm     - rahulik liiklus, 60 s
  opening  - müügi avanemine: 20 s eelmüük, 60 s tipp, 20 s rahunemine (vaikimisi)
  short    - lühike tipp kiireks kontrolliks (30 s)

Seaded: CATALOG_RPS_BASE, CATALOG_RPS_PEAK, BOOKING_RPS_BASE, BOOKING_RPS_PEAK,
CLIENT_TIMEOUT_S (kaua külastaja ootab), PANIC_RETRY (on -> ajalõpu järel vajutab uuesti),
CLIENT_SENDS_KEY (on -> korduskatse saadab sama Idempotency-Key).
"""
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

T = os.environ.get("TARGET", "http://gateway").rstrip("/")
CAT = os.environ.get("CATALOG_BASE", T + "/api/catalog")
BOOK = os.environ.get("BOOKING_BASE", T + "/api/booking")
PROFILE = os.environ.get("LOAD_PROFILE", "opening")
C_BASE = float(os.environ.get("CATALOG_RPS_BASE", 5))
C_PEAK = float(os.environ.get("CATALOG_RPS_PEAK", 65))
B_BASE = float(os.environ.get("BOOKING_RPS_BASE", 0.5))
B_PEAK = float(os.environ.get("BOOKING_RPS_PEAK", 6))
TIMEOUT = float(os.environ.get("CLIENT_TIMEOUT_S", 4))
PANIC = os.environ.get("PANIC_RETRY", "on").lower() in ("1", "on", "true", "yes")
SAME_KEY = os.environ.get("CLIENT_SENDS_KEY", "on").lower() in ("1", "on", "true", "yes")

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
lock = threading.Lock()
win = None


def new_window():
    return {"cat_n": 0, "cat_err": 0, "cat_ms": [], "book_n": 0, "book_err": 0, "book_ms": [],
            "book_timeouts": 0, "unknown": 0, "retries": 0}


def phases():
    if PROFILE == "calm":
        return [("rahulik", 60, C_BASE, B_BASE)]
    if PROFILE == "short":
        return [("tipp", 30, C_PEAK, B_PEAK)]
    return [("eelmüük", 20, C_BASE, B_BASE), ("TIPP", 60, C_PEAK, B_PEAK),
            ("rahunemine", 20, C_BASE * 2, B_BASE * 2)]


def call(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return r.status, json.loads(raw or b"null"), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read() or b"null")
        except ValueError:
            d = None
        return e.code, d, (time.perf_counter() - t0) * 1000
    except Exception:  # ajalõpp või ühenduse viga
        return 0, None, (time.perf_counter() - t0) * 1000


def catalog_visit():
    if random.random() < 0.6:
        url = CAT + "/events?query=soor-2027"
    else:
        url = CAT + "/events/soor-2027"
    st, _, ms = call("GET", url)
    with lock:
        w = win
        w["cat_n"] += 1
        w["cat_ms"].append(ms)
        if st == 0 or st >= 500:
            w["cat_err"] += 1


def booking_attempt():
    cust = "c-%05d" % random.randint(1, 20000)
    body = {"event_id": "soor-2027", "ticket_type": "VIP" if random.random() < 0.1 else "GA",
            "qty": random.choice([1, 1, 2]), "customer": cust}
    key = uuid.uuid4().hex
    tries = 2 if PANIC else 1
    for i in range(tries):
        st, d, ms = call("POST", BOOK + "/reservations", body, {"Idempotency-Key": key})
        with lock:
            w = win
            w["book_n"] += 1
            w["book_ms"].append(ms)
            if i:
                w["retries"] += 1
            if st == 0:
                w["book_timeouts"] += 1
            elif st >= 500:
                w["book_err"] += 1
            elif d and d.get("status") == "PAYMENT_UNKNOWN":
                w["unknown"] += 1
        if st != 0:
            return
        # Külastaja ei saanud vastust: ootab hetke ja vajutab uuesti
        time.sleep(1.0)
        if not SAME_KEY:
            key = uuid.uuid4().hex


def pct(vals, p):
    if not vals:
        return 0
    v = sorted(vals)
    return v[min(len(v) - 1, int(p / 100 * (len(v) - 1)))]


def report(label, elapsed):
    global win
    with lock:
        w, win = win, new_window()
    secs = 10.0
    print("%-11s %4ds | %6.1f  %6.0f  %5.1f%% | %5.1f  %6.0f  %4d  %4d  %4d  %4d" % (
        label, elapsed, w["cat_n"] / secs, pct(w["cat_ms"], 95),
        100.0 * w["cat_err"] / max(1, w["cat_n"]), w["book_n"] / secs, pct(w["book_ms"], 95),
        w["book_err"], w["book_timeouts"], w["unknown"], w["retries"]), flush=True)


def get(url):
    st, d, _ = call("GET", url)
    return d if st == 200 else None


def main():
    global win
    win = new_window()
    print(f"NordEvent Lab koormus | profiil={PROFILE} | sihtmärk={T} | klient ootab {TIMEOUT}s"
          f" | paanikakordus={'jah' if PANIC else 'ei'} | sama võti={'jah' if SAME_KEY else 'ei'}")
    print("faas        aeg  | kat/s  kat p95  kat 5xx | bron/s bron p95  5xx  ajal. tead. kord.")
    print("                 |        (ms)             |        (ms)        lõpp  mata  used")
    pool = ThreadPoolExecutor(max_workers=400)
    start = time.time()
    next_report = start + 10
    for name, dur, crps, brps in phases():
        p_end = time.time() + dur
        acc_c = acc_b = 0.0
        while time.time() < p_end:
            tick = time.time()
            acc_c += crps / 10.0
            acc_b += brps / 10.0
            while acc_c >= 1:
                pool.submit(catalog_visit)
                acc_c -= 1
            while acc_b >= 1:
                pool.submit(booking_attempt)
                acc_b -= 1
            if time.time() >= next_report:
                report(name, int(time.time() - start))
                next_report += 10
            time.sleep(max(0, 0.1 - (time.time() - tick)))
    pool.shutdown(wait=True)
    report("lõpp", int(time.time() - start))
    time.sleep(3)
    b = get(BOOK + "/stats") or {}
    p = get(T + "/api/paybaltic/stats") or {}
    n = get(T + "/api/notification/stats") or {}
    q = get(T + "/api/queue/stats") or {}
    print("\n=== ÄRILISED NÄITAJAD (pärast koormust) ===")
    print("Tellimused olekute kaupa:      ", b.get("orders_by_status"))
    print("Vanim makseta tellimus (s):    ", b.get("oldest_open_payment_age_s"))
    print("Topelt väljastatud pileteid:   ", b.get("duplicate_tickets_issued"))
    print("Kliente mitme avatud tellimusega:", b.get("customers_with_multiple_open_orders"))
    print("Topelt makstud tellimusi:      ", p.get("orders_charged_more_than_once"))
    print("Kinnituse ooteaeg (ms):        ", n.get("confirmation_delay_ms"))
    print("Järjekorrad:                   ", q.get("queues"))
    print("(Täpsemalt: ./lab.sh stats)")


if __name__ == "__main__":
    main()
