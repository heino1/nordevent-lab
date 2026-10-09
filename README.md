# NordEvent Lab

Praktikumikeskkond aines **ICM0014 Mikroteenused ja konteinerarhitektuur** (TalTech, 2026/2027).

NordEvent Lab on NordEventi piletiplatvormi miniatuurne, kuid **päriselt töötav** mudel. See käivitub ühel AWS EC2 masinal Docker Compose'i abil. Programmeerida ei ole vaja. Sa muudad **käituskonfiguratsiooni** (fail `.env`) ja tõlgendad **tagajärgi** logide, mõõdikute ja äriliste näitajate põhjal.

> NordEvent on väljamõeldud ettevõte. Kõik andmed on simuleeritud. PayBaltic on maksepartneri simulaator: päris raha ega isikuandmeid ei kasutata.

---

## 1. Mis siin töötab?

```
                    Internet (port 80)
                          │
                    ┌─────▼─────┐
                    │  gateway  │  koormusjaotur / API-värav (≈ AWS ALB)
                    └──┬─────┬──┘
          /api/catalog │     │ /api/booking
               ┌───────▼┐   ┌▼─────────┐  maksealgatus (sünkroonne)  ┌───────────┐
               │catalog │──►│ booking  │────────────────────────────►│ paybaltic │ väline
               │ ×N     │API│ olekuga  │◄────────────────────────────│ simulaator│ partner
               └────────┘   └────┬─────┘  maksetulemus (callback)    └───────────┘
                                 │ order-paid sündmus (asünkroonne)
                            ┌────▼────┐        ┌──────────────┐
                            │  queue  │───────►│ notification │  ostukinnitus
                            │ (≈ SQS) │        └──────────────┘
                            └─────────┘
   NE-Classic (valikuline): catalog + booking ÜHES konteineris ja ühes töölõimede kogumis
```

| Teenus | Vastutus | Omab andmeid | Märkus |
|---|---|---|---|
| `gateway` | liikluse suunamine | – | ainus internetti avatud osa |
| `catalog` | ürituste otsing ja detailvaade | ürituste info | skaleeritav (`CATALOG_REPLICAS`) |
| `booking` | broneering, tellimuse olek, pileti väljastus | saadavus, tellimused | olekuga, oma andmeköide |
| `queue` | sõnumijärjekord | sõnumid | SQS-i semantika: visibility timeout, DLQ |
| `notification` | ostukinnitused | saadetud kirjad | tarbib `order-paid` sündmusi |
| `paybaltic` | **väline** maksepartner | maksed | simulaator, käitumist muudad `.env` kaudu |
| `classic` | NE-Classic | kõik ühes | ainult stsenaariumides `09-classic` ja `15-*` |

### Vastavus AWS ECS-i mõistetega

| AWS ECS / Fargate | NordEvent Labis |
|---|---|
| Konteinerkujund ECR-is | `nordevent/catalog:1.0` (valikuliselt `./lab.sh ecr-push`) |
| Task definition | teenuse plokk failis `docker-compose.yml` (kujund, CPU, mälu, env, healthcheck, logimine) |
| Service + desired count | `deploy.replicas` / `CATALOG_REPLICAS` / `./lab.sh scale` |
| Task CPU / memory | `deploy.resources.limits` / `CATALOG_CPUS`, `CATALOG_MEMORY` |
| ALB + target group + health check | `gateway` + Dockeri DNS + `healthcheck` |
| CloudWatch Logs (awslogs) | `./lab.sh cloudwatch on` → logigrupp `/nordevent-lab` |
| Secrets Manager / SSM | `secrets/paybaltic_api_key.txt` → `/run/secrets/...` |
| Task role (IAM) | EC2 instance profile `LabInstanceProfile` (kogu masina roll!) |
| SQS | `queue` |

**Oluline erinevus:** ECS asendab mittetervisliku (*unhealthy*) ülesande automaatselt. Docker Compose ühel masinal seda ei tee, vaid ainult taaskäivitab kokku jooksnud konteineri. Kõik teenused jooksevad ühel EC2-l, nii et kui masin kaob, kaob kogu süsteem.

---

## 2. Käivitamine AWS Academy Learner Labis (~10 min)

1. **Learner Lab → Start Lab**. Oota, kuni täpp on roheline, ja vajuta **AWS**.
2. Ava **EC2 → Launch instance**:
   - **Name:** `nordevent-lab-<sinu nimi>`
   - **AMI:** Amazon Linux 2023 (vaikimisi)
   - **Instance type:** `t3.medium`
   - **Key pair:** *Proceed without a key pair* (ühendame brauseri kaudu)
   - **Network settings:** luba **SSH** ja **HTTP** (port 80)
   - **Advanced details → IAM instance profile:** `LabInstanceProfile`
   - **Advanced details → User data:** kleebi kogu faili `install-ec2.sh` sisu (õppejõu antud versioon)
3. **Launch instance**. Oota 4–5 minutit, sest paigaldus käib taustal.
4. Ava brauseris `http://<Public IPv4 address>/`. Peaksid nägema teksti `NordEvent Lab gateway`.
5. Vali instance → **Connect → EC2 Instance Connect → Connect**. Avaneb terminal:
   ```bash
   cd nordevent-lab
   ./lab.sh help
   ./lab.sh buy
   ```

Kui leht ei avane, kontrolli `cat /var/log/nordevent-install.log` ja seda, kas turberühmas on port 80 lahti.

### Järgmisel korral
Learner Labi seanss peatab EC2 automaatselt. Uuel korral tee nii: **Start Lab → EC2 → Start instance**. NB! Avalik IP muutub. Seejärel:
```bash
cd nordevent-lab && ./lab.sh up
```

### Töö lõpus (kuluhügieen!)
```bash
./lab.sh down
```
Seejärel **EC2 → Instance state → Stop**. Kursuse lõpus tee **Terminate**.

---

## 3. Käsud

| Käsk | Mida teeb |
|---|---|
| `./lab.sh scenarios` | stsenaariumide nimekiri |
| `./lab.sh scenario 09-classic` | võtab stsenaariumi kasutusele (kopeerib `.env` ja käivitab) |
| `./lab.sh buy` | ostab ühe pileti ja näitab tellimuse olekut |
| `./lab.sh load` | „müügi avanemine“ (~100 s); `short` = 30 s, `calm` = rahulik |
| `./lab.sh stats` | ärilised ja tehnilised näitajad |
| `./lab.sh status` | konteinerite seis, CPU ja mälu |
| `./lab.sh config` | tegelik käituskonfiguratsioon (**konfiguratsioonikaardi** jaoks) |
| `./lab.sh logs booking payment_timeout` | ühe teenuse logid, filtriga |
| `./lab.sh trace ORD-1A2B3C4D` | ühe ostu teekond läbi kõigi teenuste (korrelatsioonitunnus) |
| `./lab.sh scale catalog 3` | koopiate arv (desired count) |
| `./lab.sh stop notification` / `start` | tõrke simuleerimine |
| `./lab.sh reset` | puhas algus, andmed kustutatakse |
| `./lab.sh cloudwatch on` | logid AWS CloudWatchi |

Brauseris: `http://<IP>/api/catalog/events`, `http://<IP>/api/booking/stats`, `http://<IP>/api/notification/stats`.

### Koormuse väljundi lugemine
```
faas        aeg  | kat/s  kat p95  kat 5xx | bron/s bron p95  5xx  ajal. tead. kord.
TIPP          30s |   60.0     307    0.0% |   5.0     554     0     0     0     0
```
`kat` = kataloog, `bron` = broneerimine, `p95` = 95% päringutest oli sellest kiirem (ms), `ajal. lõpp` = külastaja ei saanud vastust, `tead.mata` = makse kinnitamisel (PAYMENT_UNKNOWN), `kord.used` = külastaja vajutas uuesti.

---

## 4. Ohutus ja tehisaru
- Ära kopeeri AWS Academy ligipääsuandmeid, IP-aadresse ega võtmeid avalikku tehisaru tööriista.
- `secrets/paybaltic_api_key.txt` on õppevõti, kuid käitu sellega nagu päris saladusega.
- Kõik ainekava jaotise 14 tehisaru reeglid kehtivad ka siin.
