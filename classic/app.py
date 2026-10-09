"""NE-Classic - NordEventi praegune rakendus (simulatsioon).

SAMA kood mis eraldi teenustes (catalog, booking), kuid:
  * üks protsess, üks konteiner, üks CPU/mälu piir;
  * üks jagatud töölõimede kogum (SERVER_WORKERS, vaikimisi 16), mida kasutavad
    nii kataloogi päringud, broneeringud kui ka maksepartneri callback'id.
Kui kataloogi tipp hõivab kõik töölõimed, jäävad ka ostud ja callback'id ootama.
Logis näitab seda väli waited_for_worker_ms.
"""
import os
import threading

os.environ.setdefault("SERVER_WORKERS", "16")
os.environ.setdefault("SERVICE_NAME", "ne-classic")

import booking_app as booking  # noqa: E402
import catalog_app as catalog  # noqa: E402
from nelib import log  # noqa: E402

if __name__ == "__main__":
    booking.init_db()
    threading.Thread(target=booking.expiry_sweeper, daemon=True).start()
    if booking.OUTBOX:
        threading.Thread(target=booking.outbox_relay, daemon=True).start()
    log("INFO", "ne_classic_started", worker_pool=os.environ["SERVER_WORKERS"],
        message="Kataloog (8000) ja broneerimine (8002) jagavad sama töölõimede kogumit")
    threading.Thread(target=booking.app.serve, args=(8002,), daemon=True).start()
    catalog.app.serve(8000)
