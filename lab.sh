#!/usr/bin/env bash
# =====================================================================
# NordEvent Lab - praktikumi tööriist (ICM0014 Mikroteenused ja konteinerarhitektuur)
# Kasutus: ./lab.sh <käsk> [argumendid]      Abi: ./lab.sh help
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"

DC=(docker compose -f docker-compose.yml)
[ -f .cloudwatch ] && DC+=(-f docker-compose.cloudwatch.yml)
G="http://localhost"

need_env() { [ -f .env ] || { cp scenarios/00-baas.env .env; echo "(.env puudus - kasutan stsenaariumi 00-baas)"; }; }
pretty()   { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }
get()      { curl -s -m 10 "$1" | pretty || echo "(ei vastanud: $1)"; }
header()   { awk '/^$/{exit} {sub(/^# ?/,""); print}' "$1"; }

classic_off_if_unused() {
  if ! grep -q '^COMPOSE_PROFILES=.*classic' .env 2>/dev/null; then
    "${DC[@]}" --profile classic rm -sf classic >/dev/null 2>&1 || true
  fi
}

up() {
  need_env
  "${DC[@]}" up -d --build --remove-orphans || return 1
  classic_off_if_unused
  echo "Ootan, kuni teenused käivituvad..."; sleep 6
  "${DC[@]}" ps --format 'table {{.Service}}\t{{.Name}}\t{{.Status}}'
  echo; curl -s -m 5 "$G/" || echo "Gateway ei vasta veel - proovi mõne sekundi pärast: ./lab.sh status"
}

case "${1:-help}" in
  up)
    up ;;

  scenario)
    [ -n "${2:-}" ] || { "$0" scenarios; exit 1; }
    f="scenarios/$2.env"
    [ -f "$f" ] || { echo "Stsenaariumi '$2' ei ole."; "$0" scenarios; exit 1; }
    cp "$f" .env
    echo "================ STSENAARIUM: $2 ================"
    header "$f"
    echo "================================================="
    up ;;

  scenarios)
    echo "Saadaolevad stsenaariumid (./lab.sh scenario <nimi>):"
    for f in scenarios/*.env; do printf "  %-32s %s\n" "$(basename "$f" .env)" "$(head -1 "$f" | sed 's/^# //')"; done ;;

  scale)
    [ $# -eq 3 ] || { echo "Kasutus: ./lab.sh scale catalog 3"; exit 1; }
    "${DC[@]}" up -d --no-recreate --scale "$2=$3" "$2"
    sleep 6; "${DC[@]}" ps "$2" --format 'table {{.Name}}\t{{.Status}}'
    echo "NB! Gateway leiab uued koopiad ~5 sekundi jooksul (DNS)." ;;

  load)
    need_env
    echo "Koormusprofiil: ${2:-opening}  (opening ~100 s | short 30 s | calm 60 s)"
    "${DC[@]}" --profile tools run --rm --build -e LOAD_PROFILE="${2:-opening}" loadgen ;;

  buy)
    key="kasutaja-$(date +%s%N | tail -c 7)"
    echo "Ostan 1 GA pileti (Idempotency-Key: $key)"
    r=$(curl -s -m 30 -X POST "$G/api/booking/reservations" -H "Content-Type: application/json" \
         -H "Idempotency-Key: $key" -d "{\"event_id\":\"soor-2027\",\"ticket_type\":\"GA\",\"qty\":1,\"customer\":\"$key\"}")
    echo "$r" | pretty
    oid=$(echo "$r" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
    [ -n "$oid" ] || exit 0
    for s in 3 6; do sleep 3; echo "--- $s s hiljem:"; curl -s "$G/api/booking/orders/$oid" | python3 -c \
      "import json,sys; d=json.load(sys.stdin); print(d['id'], d['status'], '|', d['customer_message'], '| pileteid väljastatud:', d['tickets_issued'], '| teavitusi:', d['notified'])"; done
    echo "Kogu teekond logides: ./lab.sh trace $oid" ;;

  stats)
    for s in booking paybaltic notification queue; do echo "======== $s"; get "$G/api/$s/stats"; done
    echo "======== catalog (üks koopia - korda, et näha teisi)"; get "$G/api/catalog/stats" ;;

  status)
    "${DC[@]}" ps -a --format 'table {{.Service}}\t{{.Name}}\t{{.Status}}'
    echo; docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' ;;

  logs)
    svc="${2:-}"; filt="${3:-}"
    if [ -n "$filt" ]; then "${DC[@]}" logs --no-color --no-log-prefix $svc | grep -- "$filt" | tail -40
    else "${DC[@]}" logs --no-color --tail 40 $svc; fi ;;

  trace)
    [ -n "${2:-}" ] || { echo "Kasutus: ./lab.sh trace <tellimuse-id või request_id>"; exit 1; }
    "${DC[@]}" logs --no-color --no-log-prefix 2>/dev/null | python3 -c '
import json, sys
needle = sys.argv[1]
rows = []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"): continue
    try: rows.append(json.loads(line))
    except ValueError: pass
rids = {r.get("request_id") for r in rows if needle in json.dumps(r, ensure_ascii=False)} - {None}
sel = sorted([r for r in rows if r.get("request_id") in rids or needle in json.dumps(r, ensure_ascii=False)],
             key=lambda r: r.get("ts", ""))
if not sel: print("Ei leidnud logidest:", needle); sys.exit()
print("korrelatsioonitunnused:", ", ".join(sorted(rids)))
for r in sel:
    extra = {k: v for k, v in r.items() if k not in ("ts","level","service","instance","event","request_id")}
    print(r.get("ts","")[11:23], r.get("level","").ljust(5), r.get("service","").ljust(20), r.get("event","").ljust(28),
          " ".join(f"{k}={v}" for k, v in extra.items())[:110])
' "$2" ;;

  config)
    need_env
    "${DC[@]}" config --format json 2>/dev/null | python3 -c '
import json, sys
c = json.load(sys.stdin)
for name, s in c["services"].items():
    d = s.get("deploy", {}) or {}
    lim = (d.get("resources", {}) or {}).get("limits", {}) or {}
    print("=" * 70); print("TEENUS:", name)
    print("  kujund       :", s.get("image"))
    mem = lim.get("memory", "-")
    mem = f"{int(mem)//1048576} MB" if str(mem).isdigit() else mem
    print("  koopiaid     :", d.get("replicas", 1), "   CPU:", lim.get("cpus", "-"), "   mälu:", mem)
    print("  pordid välja :", ", ".join(str(p.get("published")) + "->" + str(p.get("target")) for p in s.get("ports", [])) or "puuduvad (ainult sisevõrk)")
    hc = s.get("healthcheck", {}).get("test")
    print("  tervisekontr.:", "jah" if hc else "puudub")
    print("  logimine     :", (s.get("logging") or {}).get("driver", "json-file (lokaalne)"))
    print("  saladused    :", ", ".join(x["source"] for x in s.get("secrets", [])) or "-")
    print("  andmeköited  :", ", ".join(v.get("source", "") + ":" + v.get("target", "") for v in s.get("volumes", []) if v.get("type") == "volume") or "-")
    env = s.get("environment", {}) or {}
    for k in sorted(env):
        v = env[k]
        if "KEY" in k and not k.endswith("_FILE"):
            v = "***PEIDETUD*** (saladus keskkonnamuutujas!)" if v else "(tühi - võti tuleb saladuse failist)"
        print(f"    {k} = {v}")
' ;;

  stop)  "${DC[@]}" stop "${2:?teenuse nimi}"; echo "Peatatud: $2 (taaskäivitus: ./lab.sh start $2)" ;;
  start) "${DC[@]}" start "${2:?teenuse nimi}" ;;

  reset)
    echo "Kustutan andmed (tellimused, järjekorrad) ja käivitan uuesti..."
    "${DC[@]}" --profile classic --profile tools down -v --remove-orphans
    up ;;

  down)
    "${DC[@]}" --profile classic --profile tools down --remove-orphans
    echo "Konteinerid peatatud. Ära unusta EC2 instance'it peatada (Instance state -> Stop)!" ;;

  cloudwatch)
    case "${2:-}" in
      on)  touch .cloudwatch; DC+=(-f docker-compose.cloudwatch.yml)
           echo "Logid -> CloudWatch Logs, grupp /nordevent-lab (eeldab LabInstanceProfile rolli)"; up ;;
      off) rm -f .cloudwatch; DC=(docker compose -f docker-compose.yml); echo "Logid -> lokaalselt"; up ;;
      *)   echo "Kasutus: ./lab.sh cloudwatch on|off" ;;
    esac ;;

  ecr-push)
    region="${AWS_REGION:-us-east-1}"
    acct=$(aws sts get-caller-identity --query Account --output text) || { echo "AWS CLI ei tööta (kas LabInstanceProfile on lisatud?)"; exit 1; }
    reg="$acct.dkr.ecr.$region.amazonaws.com"
    aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$reg" || exit 1
    for s in catalog booking notification; do
      aws ecr describe-repositories --repository-names "nordevent/$s" --region "$region" >/dev/null 2>&1 || \
        aws ecr create-repository --repository-name "nordevent/$s" --image-scanning-configuration scanOnPush=true --region "$region" >/dev/null
      docker tag "nordevent/$s:1.0" "$reg/nordevent/$s:1.0" && docker push "$reg/nordevent/$s:1.0"
    done
    echo "Valmis. Vaata AWS konsoolis: ECR -> Repositories -> nordevent/* (ka turvaskaneeringu tulemused)" ;;

  help|*)
    cat <<'EOF'
NordEvent Lab - käsud
  ./lab.sh up                    käivita (või uuenda) süsteem praeguse .env järgi
  ./lab.sh scenarios             näita kõiki stsenaariume
  ./lab.sh scenario <nimi>       võta stsenaarium kasutusele ja käivita
  ./lab.sh buy                   osta üks pilet ja jälgi tellimuse olekut
  ./lab.sh load [opening|short|calm]   käivita koormus (müügi avanemine)
  ./lab.sh stats                 ärilised ja tehnilised näitajad kõigist teenustest
  ./lab.sh status                konteinerite seis + CPU/mälu
  ./lab.sh config                tegelik käituskonfiguratsioon (konfiguratsioonikaardi jaoks)
  ./lab.sh logs [teenus] [filter]      logid, nt: ./lab.sh logs booking payment_timeout
  ./lab.sh trace <ORD-id|request_id>   ühe ostu teekond läbi kõigi teenuste
  ./lab.sh scale catalog <n>     muuda koopiate arvu (ECS: desired count)
  ./lab.sh stop|start <teenus>   peata / käivita üks teenus (tõrke simuleerimine)
  ./lab.sh reset                 kustuta andmed ja alusta puhtalt
  ./lab.sh cloudwatch on|off     saada logid AWS CloudWatchi
  ./lab.sh ecr-push              lae kujundid oma AWS ECR registrisse (valikuline)
  ./lab.sh down                  peata kõik
Veebis:  http://<EC2 avalik IP>/api/catalog/events      http://<IP>/api/booking/stats
EOF
    ;;
esac
