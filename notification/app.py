"""NordEvent Lab - TEAVITUS (notification)

Vastutus: ostukinnituse saatmine kliendile.
Kaks tarbimisviisi (booking'u seade NOTIFY_MODE):
  sync  - booking kutsub POST /send otse ja ootab vastust;
  async - booking avaldab sündmuse 'order-paid' järjekorda, teavitus tarbib seda ise.

Seaded:
  NOTIFY_LATENCY_MS  ühe e-kirja saatmise aeg (e-posti teenusepakkuja aeglus)
  NOTIFY_FAIL_PCT    mitu % saatmistest ebaõnnestub
  NOTIFY_DEDUP       on -> sama tellimuse kinnitust ei saadeta kaks korda

Äriline signaal: confirmation_delay_ms = aeg makse kinnitusest kuni e-kirja saatmiseni.
"""
import random
import threading
import time

from nelib import (App, HttpError, Latency, UpstreamTimeout, env_bool, env_int, env_str,
                   http_json, log)

LAT = env_int("NOTIFY_LATENCY_MS", 200)
FAIL = env_int("NOTIFY_FAIL_PCT", 0)
DEDUP = env_bool("NOTIFY_DEDUP", True)
QUEUE_URL = env_str("QUEUE_URL", "http://queue:8000")
WORKERS = env_int("NOTIFY_WORKERS", 2)

lock = threading.Lock()
sent_by_order = {}
c = {"emails_sent": 0, "send_failures": 0, "duplicates_suppressed": 0,
     "duplicate_emails_sent": 0}
delay = Latency()


def deliver(msg, rid, via):
    """Saadab ostukinnituse. Tõstab RuntimeError, kui saatmine ebaõnnestub."""
    oid = msg.get("order_id")
    with lock:
        already = sent_by_order.get(oid, 0)
    if already and DEDUP:
        with lock:
            c["duplicates_suppressed"] += 1
        log("INFO", "duplicate_suppressed", rid, order_id=oid, via=via)
        return
    time.sleep(LAT / 1000.0)
    if random.randint(1, 100) <= FAIL:
        with lock:
            c["send_failures"] += 1
        log("WARN", "email_send_failed", rid, order_id=oid, via=via)
        raise RuntimeError("email_provider_error")
    d_ms = (time.time() - float(msg.get("paid_at") or time.time())) * 1000
    delay.add(d_ms)
    with lock:
        sent_by_order[oid] = already + 1
        c["emails_sent"] += 1
        if already:
            c["duplicate_emails_sent"] += 1
    log("INFO", "confirmation_sent", rid, order_id=oid, customer=msg.get("customer"), via=via,
        confirmation_delay_ms=round(d_ms), duplicate=bool(already) or None)


def worker():
    while True:
        try:
            st, d = http_json("GET", f"{QUEUE_URL}/queues/order-paid/receive?wait=10", timeout=15)
        except (UpstreamTimeout, ConnectionError):
            time.sleep(2)
            continue
        for m in (d or {}).get("messages", []):
            body = m["body"] or {}
            rid = body.get("request_id")
            try:
                deliver(body, rid, via="queue")
                http_json("POST", f"{QUEUE_URL}/queues/order-paid/messages/{m['message_id']}"
                          "/delete", {}, timeout=5)
            except RuntimeError:
                log("WARN", "message_will_be_retried", rid, message_id=m["message_id"],
                    receive_count=m["receive_count"])


app = App()


@app.route("GET", "/health")
def health(req):
    return 200, {"status": "ok"}


@app.route("POST", "/send")
def send(req):
    try:
        deliver(req.body or {}, req.rid, via="sync")
    except RuntimeError:
        raise HttpError(503, "email_provider_error")
    return 200, {"sent": True}


@app.route("GET", "/stats")
def stats(req):
    with lock:
        s = dict(c)
    s["confirmation_delay_ms"] = delay.summary()
    s["config"] = {"NOTIFY_LATENCY_MS": LAT, "NOTIFY_FAIL_PCT": FAIL, "NOTIFY_DEDUP": DEDUP}
    s["service"] = "notification"
    return 200, s


if __name__ == "__main__":
    log("INFO", "config_loaded", latency_ms=LAT, fail_pct=FAIL, dedup=DEDUP, workers=WORKERS)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    app.serve(8000)
