"""NordEvent Lab - PAYBALTIC (väline maksepartner, simulaator)

NB! See ei ole NordEventi teenus, vaid väline sõltuvus. Tudeng ei muuda selle koodi,
vaid ainult simulaatori käitumist .env-faili kaudu:
  PAYBALTIC_LATENCY_MS            maksealgatuse vastuseaeg
  PAYBALTIC_LATENCY_JITTER_MS     juhuslik kõikumine
  PAYBALTIC_FAIL_PCT              mitu % algatustest vastab 503
  PAYBALTIC_OUTAGE                on -> kõik päringud 503 (partneri katkestus)
  PAYBALTIC_SUCCESS_PCT           mitu % maksetest õnnestub
  PAYBALTIC_CALLBACK_DELAY_MS     kui kaua pärast algatust saadetakse tulemus (callback)
  PAYBALTIC_DUPLICATE_CALLBACK_PCT mitu % callback'e saadetakse kaks korda

PayBaltic on ise idempotentne: sama Idempotency-Key -> sama makse, raha ei võeta topelt.
Kui klient (booking) saadab igal korduskatsel uue võtme, tekib topeltmakse.
"""
import random
import threading
import time
import uuid

from nelib import (App, HttpError, UpstreamTimeout, env_bool, env_int, http_json, log,
                   read_secret)

LAT = env_int("PAYBALTIC_LATENCY_MS", 300)
JIT = env_int("PAYBALTIC_LATENCY_JITTER_MS", 100)
FAIL = env_int("PAYBALTIC_FAIL_PCT", 0)
OUTAGE = env_bool("PAYBALTIC_OUTAGE", False)
SUCCESS = env_int("PAYBALTIC_SUCCESS_PCT", 97)
CB_DELAY = env_int("PAYBALTIC_CALLBACK_DELAY_MS", 1500) / 1000.0
DUP = env_int("PAYBALTIC_DUPLICATE_CALLBACK_PCT", 0)
CB_RETRIES = env_int("PAYBALTIC_CALLBACK_RETRIES", 3)
API_KEY, KEY_SOURCE = read_secret("PAYBALTIC_API_KEY")

lock = threading.Lock()
payments = {}          # idempotency key -> payment
charges = {}           # order_id -> mitu korda raha võeti
stats_c = {"initiations": 0, "rejected_503": 0, "rejected_401": 0, "payments_created": 0,
           "idempotent_replays": 0, "callbacks_sent": 0, "callbacks_failed": 0,
           "duplicate_callbacks_sent": 0, "amount_charged_eur": 0}


def bump(k, n=1):
    with lock:
        stats_c[k] += n


def send_callback(p, rid, duplicate=False):
    body = {"order_id": p["order_id"], "payment_id": p["payment_id"], "result": p["result"]}
    for attempt in range(CB_RETRIES + 1):
        try:
            st, _ = http_json("POST", p["callback_url"], body, {"X-Request-ID": rid}, timeout=5)
            if 200 <= st < 300:
                bump("callbacks_sent")
                if duplicate:
                    bump("duplicate_callbacks_sent")
                log("INFO", "callback_delivered", rid, order_id=p["order_id"],
                    result=p["result"], duplicate=duplicate, attempt=attempt + 1)
                return
            log("WARN", "callback_rejected", rid, order_id=p["order_id"], http_status=st,
                attempt=attempt + 1, message="NordEvent ei võtnud callback'i vastu - proovin uuesti")
        except (UpstreamTimeout, ConnectionError) as e:
            log("WARN", "callback_failed", rid, order_id=p["order_id"], error=str(e),
                attempt=attempt + 1)
        bump("callbacks_failed")
        time.sleep(2)
    log("ERROR", "callback_gave_up", rid, order_id=p["order_id"])


def callback_worker(p, rid):
    time.sleep(CB_DELAY)
    send_callback(p, rid)
    if random.randint(1, 100) <= DUP:
        time.sleep(1)
        log("INFO", "sending_duplicate_callback", rid, order_id=p["order_id"])
        send_callback(p, rid, duplicate=True)


app = App()


@app.route("GET", "/health")
def health(req):
    return 200, {"status": "ok"}


@app.route("POST", "/payments")
def create(req):
    bump("initiations")
    time.sleep(max(0, LAT + random.randint(-JIT, JIT)) / 1000.0)
    if API_KEY and req.headers.get("X-Api-Key") != API_KEY:
        bump("rejected_401")
        log("WARN", "invalid_api_key", req.rid)
        raise HttpError(401, "invalid_api_key")
    if OUTAGE or random.randint(1, 100) <= FAIL:
        bump("rejected_503")
        raise HttpError(503, "paybaltic_unavailable")
    b = req.body or {}
    key = req.headers.get("Idempotency-Key") or uuid.uuid4().hex
    with lock:
        if key in payments:
            stats_c["idempotent_replays"] += 1
            p = payments[key]
            log("INFO", "payment_replayed", req.rid, order_id=p["order_id"],
                payment_id=p["payment_id"])
            return 200, {"payment_id": p["payment_id"], "replayed": True}
        p = {"payment_id": "PB-" + uuid.uuid4().hex[:10].upper(), "order_id": b.get("order_id"),
             "amount": int(b.get("amount", 0)), "callback_url": b.get("callback_url"),
             "result": "success" if random.randint(1, 100) <= SUCCESS else "declined"}
        payments[key] = p
        charges[p["order_id"]] = charges.get(p["order_id"], 0) + 1
        stats_c["payments_created"] += 1
        if p["result"] == "success":
            stats_c["amount_charged_eur"] += p["amount"]
        n = charges[p["order_id"]]
    if n > 1:
        log("WARN", "double_charge", req.rid, order_id=p["order_id"], charges=n,
            message="Sama tellimuse eest loodi mitu makset (klient maksab topelt)")
    log("INFO", "payment_created", req.rid, order_id=p["order_id"], payment_id=p["payment_id"])
    threading.Thread(target=callback_worker, args=(p, req.rid), daemon=True).start()
    return 202, {"payment_id": p["payment_id"],
                 "redirect_url": f"https://pay.paybaltic.example/{p['payment_id']}"}


@app.route("GET", "/stats")
def stats(req):
    with lock:
        s = dict(stats_c)
        dbl = sum(1 for v in charges.values() if v > 1)
    s["orders_charged_more_than_once"] = dbl
    s["config"] = {"LATENCY_MS": LAT, "FAIL_PCT": FAIL, "OUTAGE": OUTAGE, "SUCCESS_PCT": SUCCESS,
                   "CALLBACK_DELAY_MS": int(CB_DELAY * 1000), "DUPLICATE_CALLBACK_PCT": DUP}
    s["service"] = "paybaltic (väline simulaator)"
    return 200, s


if __name__ == "__main__":
    log("INFO", "config_loaded", latency_ms=LAT, fail_pct=FAIL, outage=OUTAGE,
        callback_delay_ms=int(CB_DELAY * 1000), duplicate_callback_pct=DUP)
    app.serve(8000)
