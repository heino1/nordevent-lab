# NordEvent Lab – näidisväljundid (C-rada)

Need väljundid pärinevad õppejõu testkäivitustest (2 vCPU masin, 09.10.2026). Neid saab tõlgendada ka siis, kui oma keskkonda ei õnnestu käivitada. AWS-is on arvud teistsugused, kuid muster on sama.

## 1. Kohtumine 9 – sama koormus (`./lab.sh load short`, kataloog 60 p/s tipus)

**09-classic** (kataloog ja broneerimine ühes protsessis, 16 jagatud töölõime)
```
faas        aeg  | kat/s  kat p95  kat 5xx | bron/s bron p95  5xx  ajal. tead. kord.
tipp          10s |   46.9    2885    1.9% |   3.8    2485     0     1     0     0
tipp          20s |   53.7    4010   58.3% |   6.5    4009     0    37     0    15
lõpp          39s |   78.8    4012   84.8% |  13.0    4011     0   110     0    69
NE-Classic: max ootamine vaba töölõime järel 34961 ms; päringuid, mis ootasid >1 s: 2014
```

**09-eraldatud-1** (eraldi teenused, kataloogil 1 koopia)
```
tipp          10s |   36.5    4012   43.6% |   4.8     562     0     0     0     0
tipp          20s |   71.1    4012  100.0% |   5.3     558     2     8     0     3
lõpp          33s |   71.8    4011  100.0% |   6.5     566     5    12     0    14
```

**09-eraldatud-3** (kataloogil 3 koopiat)
```
tipp          10s |   60.0     307    0.0% |   4.8     581     0     0     0     0
tipp          20s |   60.0     323    0.0% |   5.0     554     0     0     0     0
lõpp          30s |   59.4     220    0.0% |   5.1     564     0     0     0     0
```

Küsimused: mis aeglustas NE-Classicus ostu? Miks broneerimine eraldatud variandis peaaegu ei kannata? Mis on kolme koopia hind?

## 2. Kohtumine 10 – PayBaltic vastab 3,5 s

**10-paybaltic-aeglane-naiivne** (ajalimiit 2 s, 3 kordust, idempotentsus VÄLJAS)
```
lõpp          39s |   10.0      91    0.0% |   9.7    4008     0    97     0    56
Tellimused olekute kaupa:        {'FAILED': 6, 'PAID': 172}
Topelt väljastatud pileteid:     794
Kliente mitme avatud tellimusega: 83
Topelt makstud tellimusi:        178
```

**10-paybaltic-aeglane-kaitstud** (ajalimiit 5 s, 1 kordus, idempotentsus SEES)
```
lõpp          33s |    9.9      77    0.0% |   4.0    3652     0     0     0     0
Tellimused olekute kaupa:        {'FAILED': 1, 'PAID': 88}
Topelt väljastatud pileteid:     0
Kliente mitme avatud tellimusega: 0
Topelt makstud tellimusi:        0
```

Küsimused: miks naiivne variant tegi rohkem tellimusi, aga kahjustas kliente? Miks p95 kaitstud variandis ei paranenud?

## 3. Kohtumine 11 – ühe ostu jälg (`./lab.sh trace ORD-2210BE19`)
```
07:44:50.818 INFO  booking       reservation_created   order_id=ORD-2210BE19 event_id=soor-2027 ticket_type=GA qty=1
07:44:51.258 INFO  paybaltic     payment_created       order_id=ORD-2210BE19 payment_id=PB-86DB0C34D3
07:44:51.259 INFO  paybaltic     request_end           route=/payments http_status=202 duration_ms=438
07:44:51.262 INFO  booking       request_end           route=/reservations http_status=202 duration_ms=445
07:44:52.764 INFO  booking       order_paid            order_id=ORD-2210BE19 previous_status=PENDING_PAYMENT
07:44:52.767 INFO  queue         request_end           route=/queues/order-paid/messages http_status=201
07:44:52.769 INFO  booking       event_published       topic=order-paid order_id=ORD-2210BE19
07:44:52.770 INFO  paybaltic     callback_delivered    order_id=ORD-2210BE19 result=success attempt=1
07:44:52.970 INFO  notification  confirmation_sent     order_id=ORD-2210BE19 via=queue confirmation_delay_ms=207
```
Küsimus: kus on sünkroonne ja kus asünkroonne osa? Kui kaua ootas klient vastust ja kui kaua kinnitust?

## 4. Kohtumine 7 – teavitusteenus on maas
| Režiim | Tulemus teavituse katkestuse ajal | Pärast teavituse taastumist |
|---|---|---|
| sync | 3 tellimust PAID, `notify_failures: 3`, `duplicate_callbacks: 3` | saadetud kinnitusi: **0** (kadusid jäädavalt) |
| async | 3 tellimust PAID, järjekorras 3 sõnumit | saadetud kinnitusi: **3**, ooteaeg 9–20 s |

## 5. Kohtumine 8 – andmed
| Stsenaarium | Tulemus |
|---|---|
| 08-skeemimuutus (jagatud andmebaas) | kataloog: `500 {"error": "availability_source_failed", "detail": "no such table: inventory"}` |
| 08-skeemimuutus-api | kataloog töötab, saadavus tuleb booking’u API-st |
| 08-outbox-puudub (järjekord maas) | `events_lost: 2`, kinnitusi 0 ka pärast taastumist |
| 08-outbox (järjekord maas) | `outbox_pending: 2` → pärast taastumist 0, kinnitusi 2 |
| 10-hiline-makse | `{'LATE_PAYMENT': 1}` – raha võetud, broneering aegunud |
