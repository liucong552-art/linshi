#!/usr/bin/env bash
# GitHub linshi testing: pinned-source deployment plus isolated real-machine acceptance.
# Use with S5QA-Linshi.ps1; do not upload to the formal zuizhongheji repo.
# NO changes to portbw, VLESS, WG, router, firewall defaults, or existing accounts.
set -Eeuo pipefail
umask 077
[[ "$(id -u)" == 0 ]] || { echo 'root required' >&2; exit 2; }
BASE=/root/s5qa-linshi
FILE="$BASE/account.json"
RAW="https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main"
DUTCH_VPS="162.211.231.219"
TS="$BASE/stopped-at"
CORE=/usr/local/lib/socks5-manager/socks5.py
EXPECTED='2ad7c0ce5c3f0c0614140f68951111d43ddc0a739eebd52a058576b0ee6fac0c'
EXPECTED_TRAFFIC='3c0a882871903d71e74953f46d3181191c84d38bfd08f71a9333dcfed1291dbd'
EXPECTED_INSTALLER='fd0bd33f5cd126ec34c700d05e0bd8c289ff29c2e1f6f47f9953b29b292bd213'
PORT="${S5QA_PORT:-41006}"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT>=1024 && PORT<=65535)) || { echo 'invalid S5QA_PORT' >&2;exit 2; }
err(){ echo "S5QA ERROR: $*" >&2;exit 1; }
source_check(){
    command -v curl >/dev/null || err 'missing curl'
    command -v sha256sum >/dev/null || err 'missing sha256sum'
    command -v python3 >/dev/null || err 'missing python3'
    SOURCE_DIR=$(mktemp -d /tmp/s5qa-linshi-source.XXXXXXXX)
    chmod 0700 "$SOURCE_DIR"
    for file in socks5.py socks5-traffic.py socks5-install.sh; do
        curl -fLsS --retry 3 --connect-timeout 10 --max-time 90 "$RAW/$file" -o "$SOURCE_DIR/$file" || err "failed downloading $file"
    done
    [[ $(sha256sum "$SOURCE_DIR/socks5.py" | cut -d' ' -f1) == "$EXPECTED" ]] || err 'linshi socks5.py SHA mismatch'
    [[ $(sha256sum "$SOURCE_DIR/socks5-traffic.py" | cut -d' ' -f1) == "$EXPECTED_TRAFFIC" ]] || err 'linshi traffic SHA mismatch'
    [[ $(sha256sum "$SOURCE_DIR/socks5-install.sh" | cut -d' ' -f1) == "$EXPECTED_INSTALLER" ]] || err 'linshi installer SHA mismatch'
    python3 -B - "$SOURCE_DIR/socks5.py" "$SOURCE_DIR/socks5-traffic.py" <<'PYCHECK'
import sys
for name in sys.argv[1:]:
    with open(name, 'rb') as f: compile(f.read(),name,'exec')
print('PASS: Python source syntax')
PYCHECK
    bash -n "$SOURCE_DIR/socks5-install.sh"
    echo 'PASS: linshi three sources pinned SHA256 and syntax'
}
settings_value(){
    python3 - "$1" <<'PYSETTING'
import json,sys
from pathlib import Path
p=Path('/etc/socks5-manager/settings.json')
try: print(str(json.loads(p.read_text()).get(sys.argv[1],'')))
except (OSError,ValueError,TypeError): print('')
PYSETTING
}
choose_wan(){
    local wan
    wan=$(settings_value wan_if)
    if [[ -z $wan ]]; then
        wan=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++)if($i=="dev"){print $(i+1);exit}}')
    fi
    [[ $wan =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || err 'cannot automatically identify physical WAN interface'
    printf '%s' "$wan"
}
choose_host(){
    local host
    host=$(settings_value host)
    if [[ -z $host || $host == AUTO ]]; then host=$DUTCH_VPS; fi
    [[ $host =~ ^[A-Za-z0-9._-]{1,253}$ ]] || err 'invalid saved public host'
    printf '%s' "$host"
}

require_core(){
    [[ -f "$CORE" && ! -L "$CORE" ]] || err 'core missing or symlink';
    [[ "$(sha256sum "$CORE" | cut -d' ' -f1)" == "$EXPECTED" ]] || err 'installed core differs from linshi version';
    command -v socks5 >/dev/null || err 'socks5 command missing';
}
read_metadata(){
    [[ -f "$FILE" && ! -L "$FILE" ]] || err 'test metadata missing';
    # Validate metadata against persisted node, no password output.
    python3 - "$FILE" "$PORT" <<'PY'
import json,re,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text())
assert re.fullmatch(r'qa[0-9]{14}[0-9a-f]{4}',m['id']), 'invalid QA ID'
assert m['port']==int(sys.argv[2]),'QA port mismatch'
n=json.loads((Path('/var/lib/socks5-manager/nodes')/(m['id']+'.json')).read_text())
assert n['instance']==m['instance'] and n['port']==m['port'],'QA instance mismatch'
print(m['id'])
PY
}
case "${1:-help}" in
  check|patch)
    action="$1"
    SOURCE_DIR=''
    trap '[[ -z "${SOURCE_DIR:-}" ]] || rm -rf -- "$SOURCE_DIR"' EXIT
    source_check
    if [[ $action == patch ]]; then
        [[ ! -e "$FILE" ]] || err 'a QA account exists; cleanup before patch'
        wan=$(choose_wan)
        host=$(choose_host)
        echo "Installing from VERIFIED linshi, detected WAN_IF=$wan and HOST=$host"
        # Local installer sees both source files and cannot silently fetch
        # a different version from the formal zuizhongheji repository.
        AUTO_DEPS=1 bash "$SOURCE_DIR/socks5-install.sh" install --wan-if "$wan" --host "$host"
        [[ $(sha256sum "$CORE" | cut -d' ' -f1) == "$EXPECTED" ]] || err 'post-install core hash mismatch'
        # Daily collection is an independent optional component; enable on QA host.
        /usr/local/sbin/socks5-traffic --install
        systemctl is-active --quiet socks5-watch.timer || err 'Watchdog timer inactive after patch'
        systemctl is-active --quiet socks5-traffic.timer || err 'Daily traffic timer inactive after patch'
        echo 'PASS: exact linshi SOCKS5 installed, Watchdog and daily traffic timers active'
    fi
    ;;
  inspect)
    require_core
    [[ -f "$FILE" ]] && err 'another QA account is recorded; cleanup first'
    command -v nft >/dev/null || err 'nft missing'
    command -v 3proxy >/dev/null || err '3proxy missing'
    systemctl is-active --quiet socks5-watch.timer || err 'watch timer not active'
    [[ -z "$(ss -lntH "sport = :$PORT" 2>/dev/null)" ]] || err "QA port ${PORT} already in use"
    [[ ! -e "/var/lib/socks5-manager/nodes/qa${PORT}.json" ]] || err 'collision'
    echo "PASS: installed source, commands, watch timer; QA port $PORT is available"
    ;;
  prepare)
    require_core
    [[ ! -e "$FILE" ]] || err 'test account already recorded; do cleanup first'
    systemctl is-active --quiet socks5-watch.timer || err 'watch timer inactive'
    [[ -z "$(ss -lntH "sport = :$PORT" 2>/dev/null)" ]] || err "port $PORT occupied"
    install -d -m 0700 "$BASE"
    testid="qa$(date -u +%Y%m%d%H%M%S)$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
    log=$(mktemp "$BASE/add.XXXXXXXX")
    if ! socks5 add --id "$testid" --seconds 7200 --port "$PORT" --ip-limit 1 --sticky 120 --quota-gib 0.25 >"$log" 2>&1; then
        rm -f -- "$log"; err 'QA account creation failed; credentials omitted; consult system journal on host'
    fi
    rm -f -- "$log"
    if ! S5QA_ID="$testid" S5QA_TEST_PORT="$PORT" python3 - "$FILE" <<'PY'
import json,os,pathlib,subprocess,sys
n=json.loads(subprocess.check_output(['socks5','show',os.environ['S5QA_ID']],text=True))
assert n['port']==int(os.environ['S5QA_TEST_PORT']) and n['protection_ready'] and n['unit_active']
assert n['quota']['remaining_estimate']>0 and n['ip_limit']==1
p=pathlib.Path(sys.argv[1]);p.write_text(json.dumps({'id':n['id'],'port':n['port'],'instance':n['instance']})+'\n');p.chmod(0o600)
print('PASS: isolated QA account created; ID',n['id'],'port',n['port'],'guard=True, quota=yes')
PY
    then
        socks5 del "$testid" >/dev/null 2>&1 || true
        err 'QA post-create inspection failed; attempted to remove only the QA account'
    fi
    # Initialize this short-lived QA account's daily counters before the
    # authentication/connect test, so the later traffic assertion is meaningful.
    if systemctl is-active --quiet socks5-traffic.timer; then
        socks5-traffic --collect
        echo 'PASS: isolated daily traffic counters initialized'
    fi
    ;;
  stop)
    id=$(read_metadata | tail -n1)
    [[ ! -e "$TS" ]] || err 'stop already performed; probe or cleanup first'
    systemctl is-active --quiet "socks5-@$id.service" || err 'QA unit inactive before test'
    date +%s.%N > "$TS"
    systemctl stop "socks5-@$id.service"
    echo "PASS: intentionally stopped QA unit $id; wait for timer self recovery"
    ;;
  probe)
    id=$(read_metadata | tail -n1)
    [[ -f "$TS" ]] || err 'stop has not been executed'
    S5QA_ID="$id" S5QA_PORT="$PORT" python3 - "$TS" <<'PY'
import json,os,subprocess,sys,time
from pathlib import Path
start=float(Path(sys.argv[1]).read_text().strip())
n=json.loads(subprocess.check_output(['socks5','show',os.environ['S5QA_ID']],text=True))
port=os.environ['S5QA_PORT']
assert n['id']==os.environ['S5QA_ID'] and str(n['port'])==port
lst=subprocess.run(['ss','-lntH',f'sport = :{port}'],capture_output=True,text=True)
listen=bool(lst.stdout.strip())
log=subprocess.run(['journalctl','-b','--no-pager','-o','json','-u','socks5-watch.service'],capture_output=True,text=True)
recovery=None
for row in log.stdout.splitlines():
    try:item=json.loads(row)
    except ValueError:continue
    if f'watchdog: recovered {n["id"]} port={port}' in str(item.get('MESSAGE','')):
        seconds=float(item['__REALTIME_TIMESTAMP'])/1e6-start
        if seconds>=0:recovery=round(seconds,2)
passed=bool(n['unit_active']) and bool(n['protection_ready']) and listen and recovery is not None and 0<=recovery<=90
print(json.dumps({'id':n['id'],'active':n['unit_active'],'guard':n['protection_ready'],
                  'listening':listen,'recovery_seconds':recovery,
                  'elapsed_seconds':round(time.time()-start,2),'passed':passed},ensure_ascii=False))
PY
    ;;
  validate)
    id=$(read_metadata | tail -n1)
    systemctl is-active --quiet "socks5-@$id.service" || err 'QA service inactive'
    # This test calls the already shipped security handshake, including wrong-password
    # CONNECT refusal, private-destination block, and UDP-ASSOC rejection.
    S5QA_ID="$id" python3 - <<'PY'
import os,sys
sys.path.insert(0,'/usr/local/lib/socks5-manager')
import socks5
n=socks5.node(os.environ['S5QA_ID'])
assert socks5.ready(n),'nft guards not ready'
socks5.verify_proxycfg(n)
socks5.socks_handshake(n)
print('PASS: real 3proxy authentication, negative password, private-target ACL, UDP rejection, CONNECT to 1.1.1.1:443')
PY
    if command -v socks5-traffic >/dev/null; then
        if systemctl is-enabled --quiet socks5-traffic.timer; then
            socks5-traffic --collect
            socks5-traffic "$id" --json | S5QA_ID="$id" S5QA_PORT="$PORT" python3 -c 'import sys,json,os;d=json.load(sys.stdin);a=d["users"];assert len(a)==1 and a[0]["id"]==os.environ["S5QA_ID"] and int(a[0]["port"])==int(os.environ["S5QA_PORT"]);assert sum(v["upload"]+v["download"] for v in a[0]["days"].values())>0;print("PASS: daily traffic bytes recorded under isolated QA port")'
        else echo 'SKIP: optional daily traffic timer not installed'; fi
    else echo 'SKIP: optional socks5-traffic not installed'; fi
    ;;
  cleanup)
    if [[ ! -e "$FILE" ]]; then echo 'No QA account registered';exit 0;fi
    id=$(read_metadata | tail -n1)
    socks5 del "$id" >/dev/null
    [[ ! -e "/var/lib/socks5-manager/nodes/$id.json" ]] || err 'QA account persisted after deletion'
    rm -f -- "$FILE" "$TS"
    echo "PASS: removed ONLY QA account $id (portbw remains untouched)"
    ;;
  logs)
    journalctl -b --no-pager -u socks5-watch.service -u socks5-gc.service -u socks5-save.service -n 100
    ;;
  *) echo 'Usage: bash s5qa-linshi.sh check|patch|inspect|prepare|stop|probe|validate|cleanup|logs' >&2;exit 2;;
esac
