"""NordEvent Lab - QUEUE (sõnumijärjekord, Amazon SQS-i lihtsustatud analoog)

Semantika nagu SQS-il:
  * sõnum säilib (SQLite, püsiv andmeköide), kuni tarbija selle kustutab;
  * vastuvõtmisel muutub sõnum nähtamatuks (visibility timeout);
  * kui tarbija ei kustuta, ilmub sõnum uuesti -> "vähemalt üks kord" kohaletoimetamine;
  * pärast QUEUE_MAX_RECEIVES ebaõnnestunud katset liigub sõnum surnud kirjade
    järjekorda (<nimi>-dlq).
"""
import json
import sqlite3
import threading
import time
import uuid

from nelib import App, HttpError, env_int, env_str, log

DB = env_str("QUEUE_DB_PATH", "/data/queue.db")
MAX_RECEIVES = env_int("QUEUE_MAX_RECEIVES", 5)
DEFAULT_VIS = env_int("QUEUE_VISIBILITY_S", 20)

lock = threading.Lock()
cond = threading.Condition(lock)
db = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, queue TEXT, body TEXT,
              created_at REAL, visible_at REAL, receives INTEGER DEFAULT 0)""")
db.execute("CREATE INDEX IF NOT EXISTS ix_q ON messages(queue, visible_at)")

app = App()


@app.route("GET", "/health")
def health(req):
    return 200, {"status": "ok"}


@app.route("POST", "/queues/<q>/messages")
def send(req, q):
    mid = uuid.uuid4().hex[:12]
    now = time.time()
    with cond:
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,0)",
                   (mid, q, json.dumps((req.body or {}).get("body")), now, now))
        cond.notify_all()
    return 201, {"message_id": mid}


@app.route("GET", "/queues/<q>/receive")
def receive(req, q):
    wait = min(int(req.query.get("wait", 10)), 20)
    vis = int(req.query.get("visibility", DEFAULT_VIS))
    deadline = time.time() + wait
    with cond:
        while True:
            now = time.time()
            r = db.execute("SELECT id, body, receives, created_at FROM messages WHERE queue=? AND "
                           "visible_at<=? ORDER BY created_at LIMIT 1", (q, now)).fetchone()
            if r:
                mid, body, n, created = r
                if n >= MAX_RECEIVES:
                    db.execute("UPDATE messages SET queue=?, visible_at=? WHERE id=?",
                               (q + "-dlq", now, mid))
                    log("WARN", "moved_to_dlq", req.rid, queue=q, message_id=mid, receives=n)
                    continue
                db.execute("UPDATE messages SET visible_at=?, receives=receives+1 WHERE id=?",
                           (now + vis, mid))
                return 200, {"messages": [{"message_id": mid, "body": json.loads(body),
                                           "receive_count": n + 1,
                                           "age_s": round(now - created, 1)}]}
            if now >= deadline:
                return 200, {"messages": []}
            cond.wait(timeout=min(1.0, deadline - now))


@app.route("POST", "/queues/<q>/messages/<mid>/delete")
def delete(req, q, mid):
    with lock:
        cur = db.execute("DELETE FROM messages WHERE id=? AND queue=?", (mid, q))
    if cur.rowcount == 0:
        raise HttpError(404, "message_not_found")
    return 200, {"deleted": mid}


@app.route("GET", "/stats")
def stats(req):
    now = time.time()
    out = {}
    with lock:
        for q, vis, inflight, oldest in db.execute(
                "SELECT queue, SUM(visible_at<=?), SUM(visible_at>?), MIN(created_at) "
                "FROM messages GROUP BY queue", (now, now)):
            out[q] = {"visible": vis, "in_flight": inflight,
                      "oldest_message_age_s": round(now - oldest, 1)}
    return 200, {"service": "queue", "queues": out, "max_receives": MAX_RECEIVES}


if __name__ == "__main__":
    log("INFO", "config_loaded", max_receives=MAX_RECEIVES, visibility_s=DEFAULT_VIS)
    app.serve(8000)
