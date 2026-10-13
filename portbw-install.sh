#!/usr/bin/env bash
# portbw C.2.2 -- portable installer / updater for Linux VPS with systemd.
# Pair with the complete portbw.py next to this script.
# No fixed IP/hostname/NIC/port/policy; uses only its own nft table inet pbw_policy,
# tc clsact filters allocated by portbw, and portbw-* units. Does NOT touch Xray,
# WireGuard, SSH, firewall rules in other nft tables, or the root qdisc.
#
# Usage (as root):
#   bash portbw-install.sh check [--iface ens3] [--install-deps]
#   bash portbw-install.sh install [--iface ens3] [--nft-only] [--install-deps]
#   bash portbw-install.sh upgrade [--iface ens3] [--nft-only] [--install-deps]
#   `update` is an alias for `upgrade`, compatible with the repository installer.
#   bash portbw-install.sh verify
#
# install: brand-new deployment only; zero port policies are created.
# upgrade: explicit opt-in for an existing compatible /usr/local/lib/portbw/
#          installation. Always backs up installed code/config/policies/units.
# verify:  read-only audit and service state check (does not verify a 1Hz load test).
# check:   checks host, candidate source, selected interface and nft access.
# Dependencies: Linux, systemd PID 1, Python 3.9+, nftables, iproute2 (tc/ip/ss).
# Limited-kernel VPS/containers lacking CAP_NET_ADMIN cannot be supported.
# The portbw.py implementation owns INPUT/OUTPUT port policies for this host;
# FORWARD/NAT traffic requires separate design and is NOT covered.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

PREFIX='[portbw-install]'
CODE='/usr/local/lib/portbw/portbw.py'
CLI='/usr/local/sbin/portbw'
CONF='/etc/portbw/config.json'
STATE_DIR='/var/lib/portbw'
UNIT_DIR='/etc/systemd/system'
UNITS=(portbw-restore.service portbw-watch.service portbw-watch.timer)
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE="$SELF_DIR/portbw.py"

log() { printf '%s %s\n' "$PREFIX" "$*"; }
fail() { printf '%s ERROR: %s\n' "$PREFIX" "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Usage: bash portbw-install.sh {check|install|upgrade|verify} [options]
  check       Read-only host/source prerequisites (unless --install-deps).
  install     Initial installation ONLY; creates no port policies.
  upgrade     Explicit upgrade of existing, standard-layout portbw.
  verify      Read-only checks for installed portbw and 1Hz service.
Options for check/install/upgrade:
  --iface NAME       Egress network device for tc; auto-detect from default route.
                     Specify explicitly if the default interface is a tunnel.
  --nft-only         Static upload limits only; download/auto require tc.
                     Not suitable when tc is already active on existing policies.
  --install-deps     Allow package manager to install nftables/iproute2/python3.
  --help             Show usage.

Requirements: Linux + systemd PID1, root, Python >=3.9, nftables, iproute2,
CAP_NET_ADMIN; compatible named nft limit objects. Supports common package
managers: apt-get, dnf, yum, zypper, pacman. Does not assume any ports, IP,
node software or WireGuard. No implicit migration of an unknown installation.

After install, configure explicitly, for example:
  portbw set 23456 40 40
  portbw auto set 23456 --up 40 --down 40 --trigger 100 \
     --after 30s --auto-up 18 --auto-down 18 --hold 120s \
     --cooldown 0s --resilient
  portbw audit

Note: "resilient" is optional per port; omitted mode defaults to strict.
Do not apply this sample policy to your SSH port or a production port without
planning your rollback. For FORWARD/transit traffic, inspect the implementation.
EOF
}

[[ $# -gt 0 ]] || { usage; exit 2; }
ACTION="$1"; shift
case "$ACTION" in check|install|upgrade|verify) ;; update) ACTION=upgrade ;; -h|--help|help) usage; exit 0 ;; *) usage >&2; fail "Unknown action: $ACTION" ;; esac
IFACE=''
NFT_ONLY=0
INSTALL_DEPS=0
while (($#)); do
    case "$1" in
        --iface) (($# >= 2)) || fail '--iface needs a device name'; IFACE="$2"; shift 2 ;;
        --nft-only) NFT_ONLY=1; shift ;;
        --install-deps) INSTALL_DEPS=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
if [[ "$ACTION" == verify && ( -n "$IFACE" || "$NFT_ONLY" == 1 || "$INSTALL_DEPS" == 1 ) ]]; then
    fail 'verify is read-only and takes no options'
fi
[[ "$(uname -s)" == Linux ]] || fail 'Linux only'
[[ -d /run/systemd/system ]] || fail 'PID1 must be systemd'
[[ "${EUID:-$(id -u)}" == 0 ]] || fail 'Please run as root (sudo bash portbw-install.sh ...)'

install_dependencies() {
    log 'Package installation explicitly authorized (--install-deps)'
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 nftables iproute2
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 nftables iproute
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 nftables iproute
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 nftables iproute2
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --needed --noconfirm python nftables iproute2
    else
        fail 'No supported package manager; install python3, nftables and iproute2 yourself'
    fi
}
if ((INSTALL_DEPS)); then install_dependencies; fi
for cmd in python3 nft tc ip ss systemctl sha256sum awk grep flock install cp mv; do
    command -v "$cmd" >/dev/null 2>&1 || fail "Missing program '$cmd'. Try --install-deps or install dependencies manually"
done
python3 - <<'PY'
import sys
if sys.version_info < (3,9):
    raise SystemExit('Python 3.9 or newer is required')
print('[portbw-install] Python:',sys.version.split()[0])
PY

read_existing_iface() {
    [[ -r "$CONF" ]] || return 0
    python3 - "$CONF" <<'PY'
import json,sys
try:
    obj=json.load(open(sys.argv[1],encoding='utf-8'))
    if isinstance(obj,dict) and isinstance(obj.get('iface'),str):
        print(obj['iface'])
except (OSError,ValueError):
    pass
PY
}
autodetect_iface() {
    local found
    found="$(ip -4 route show default | awk '{ for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit} }')"
    if [[ -z "$found" ]]; then
        found="$(ip -6 route show default | awk '{ for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit} }')"
    fi
    printf '%s\n' "$found"
}
validate_iface() {
    [[ "$1" =~ ^[a-zA-Z0-9_.-]{1,15}$ && "$1" != '.' && "$1" != '..' ]] || fail "Invalid interface name: $1"
    ip link show dev "$1" >/dev/null 2>&1 || fail "Interface does not exist: $1"
}
check_nft_access() {
    nft -j list tables >/dev/null 2>&1 || fail 'Cannot query nftables; check root/CAP_NET_ADMIN and kernel support'
}

if [[ "$ACTION" == verify ]]; then
    [[ -f "$CODE" && -x "$CLI" ]] || fail 'portbw is not installed in the standard layout'
    python3 -B - "$CODE" <<'PYAST' || fail 'Installed Python fails syntax check'
import ast,sys
with open(sys.argv[1],encoding='utf-8') as f:
    ast.parse(f.read(),filename=sys.argv[1])
PYAST
    [[ -r "$CONF" ]] || fail 'Missing installed global config'
    log "Installed source SHA256: $(sha256sum "$CODE" | awk '{print $1}')"
    "$CLI" status
    "$CLI" audit
    systemctl is-active --quiet portbw-watch.service || fail '1Hz watchd service not active'
    systemctl is-enabled --quiet portbw-restore.service || fail 'boot restore not enabled'
    systemctl show portbw-watch.service -p MainPID -p Type -p ActiveState
    # A successful audit alone is not a real 1Hz test; expose heartbeat evidence.
    python3 - <<'PY'
import json, pathlib, subprocess, time
p=pathlib.Path('/run/portbw/watchd.json')
if not p.exists():raise SystemExit('Missing watchd heartbeat')
h=json.loads(p.read_text())
pid=int(subprocess.check_output(['systemctl','show','portbw-watch.service','-p','MainPID','--value'],text=True))
last=h.get('last_success')
if type(last) not in (int,float) or pid<=0 or h.get('pid')!=pid:
    raise SystemExit('watchd heartbeat does not belong to the current service')
age=time.clock_gettime(time.CLOCK_BOOTTIME)-last
print(f'[portbw-install] heartbeat age={age:.2f}s; attempts={h.get("attempts")}; successes={h.get("successes")}; missed={h.get("missed")}; error={h.get("error")}')
if h.get('error') or not 0<=age<5:
    raise SystemExit('watchd heartbeat not healthy')
PY
    log 'VERIFY PASS: service, heartbeat, audit; real load/throughput still needs testing'
    exit 0
fi

[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || fail "Place the complete regular file portbw.py beside this script: $SOURCE"
# Update this manifest whenever the paired Python payload changes.
EXPECTED_PORTBW_SHA256='0cfd7ab21a8fcd4acd0f75b4cd223d6662757df43fe87f117c8570c58482f9a4'
[[ "$(sha256sum "$SOURCE" | awk '{print $1}')" == "$EXPECTED_PORTBW_SHA256" ]] || fail 'portbw.py SHA256 mismatch; use the matching two-file bundle'
python3 -B - "$SOURCE" <<'PY'
import ast,sys
with open(sys.argv[1], encoding='utf-8') as f:
    ast.parse(f.read(), filename=sys.argv[1])
print('[portbw-install] Candidate Python syntax: PASS')
PY
log "Candidate source SHA256: $(sha256sum "$SOURCE" | awk '{print $1}')"
check_nft_access
existing=0
[[ -e "$CODE" || -L "$CODE" ]] && existing=1
if [[ -e "$CLI" || -L "$CLI" ]] && (( !existing )); then
    fail "Existing /usr/local/sbin/portbw without installed source; refusing to overwrite a foreign tool"
fi
if ((existing)); then
    [[ -f "$CODE" && ! -L "$CODE" ]] || fail 'Installed source is not a regular non-symlink file'
    [[ -x "$CLI" ]] || fail 'Installed /usr/local/sbin/portbw missing or not executable'
    # The repository v5.2.2 installs a regular Bash wrapper; C.2 installs a
    # symlink. Accept BOTH verified launchers so a real v5.2.2 upgrade works.
    python3 - "$CLI" "$CODE" <<'PYLAUNCHER' || fail 'Existing launcher is not a recognized portbw launcher; manual migration required'
from pathlib import Path
import sys
launcher,code=map(Path,sys.argv[1:])
official='#!/usr/bin/env bash\nset -Eeuo pipefail\nexec /usr/bin/python3 /usr/local/lib/portbw/portbw.py "$@"\n'
if launcher.is_symlink():
    valid=launcher.resolve()==code.resolve()
else:
    valid=launcher.is_file() and launcher.read_text(encoding='utf8')==official
if not valid:raise SystemExit('Unrecognized launcher')
PYLAUNCHER
fi
if [[ "$ACTION" == install && "$existing" == 1 ]]; then
    if cmp -s "$SOURCE" "$CODE"; then
        log 'Already installed at exactly this source; not overwriting. Use verify to inspect it.'
        exit 0
    fi
    fail 'Existing installation found. Use explicit upgrade after backup review, not install.'
fi
if [[ "$ACTION" == upgrade && "$existing" == 0 ]]; then
    fail 'No existing standard-layout portbw; use install for a fresh VPS'
fi
if [[ "$ACTION" == install ]]; then
    # Do not adopt an existing foreign nft table on a clean install.
    if nft list table inet pbw_policy >/dev/null 2>&1; then
        fail 'nft table inet pbw_policy already exists without standard portbw install; manual ownership check required'
    fi
    for unit in "${UNITS[@]}"; do
        [[ ! -e "$UNIT_DIR/$unit" && ! -L "$UNIT_DIR/$unit" ]] || fail "Pre-existing unit $unit without standard portbw; refusing to overwrite"
    done
fi
if [[ -z "$IFACE" && "$existing" == 1 ]]; then IFACE="$(read_existing_iface)"; fi
if [[ -z "$IFACE" ]]; then IFACE="$(autodetect_iface)"; fi
[[ -n "$IFACE" ]] || fail 'No default-route interface found; specify --iface DEVICE'
validate_iface "$IFACE"
log "Selected tc interface: $IFACE (explicitly override with --iface)"
# Public-traffic port policing should normally attach to the physical ingress
# NIC. A default route through VPN may point at the WRONG device.
if [[ "$IFACE" =~ ^(wg|tun|tap|tailscale|zt|utun) ]] && [[ "$ACTION" != check ]]; then
    log 'WARNING: selected interface resembles a tunnel; verify actual inbound interface before installing'
fi
if ((NFT_ONLY)); then log 'Mode: nft-only (no tc secondary enforcement)'; fi

if [[ "$ACTION" == check ]]; then
    if [[ "$existing" == 1 ]]; then
        log "Existing installed code SHA256: $(sha256sum "$CODE" | awk '{print $1}')"
        log 'To replace it, use the explicit upgrade action.'
    else
        log 'No standard-layout portbw installation; fresh install appears appropriate.'
    fi
    log 'CHECK PASS: no kernel or service modifications were made'
    exit 0
fi

# Lock only our installer; the Python CLI has a separate manager lock.
install -d -m 0700 /run/portbw
exec 9>/run/portbw/portbw-installer.lock
flock -w 30 9 || fail 'Another portbw installer holds the lock'

# We only manage portbw-owned paths, and never flush unrelated kernel rules.
BACKUP=''
DID_START=0
OK=0
had_code=$existing
old_watch_active=0
old_timer_active=0
if systemctl is-active --quiet portbw-watch.service; then old_watch_active=1; fi
if systemctl is-active --quiet portbw-watch.timer; then old_timer_active=1; fi
BACKUP="$(mktemp -d /var/tmp/portbw-backup-XXXXXXXX)"
chmod 0700 "$BACKUP"
if ((existing)); then
    cp -a -- "$CODE" "$BACKUP/portbw.py"
    [[ -d /etc/portbw ]] && cp -a -- /etc/portbw "$BACKUP/etc-portbw"
    [[ -d "$STATE_DIR" ]] && cp -a -- "$STATE_DIR" "$BACKUP/var-lib-portbw"
    for unit in "${UNITS[@]}"; do
        [[ -f "$UNIT_DIR/$unit" ]] && cp -a -- "$UNIT_DIR/$unit" "$BACKUP/$unit"
    done
fi
log "Recovery backup: $BACKUP"

recover() {
    local exitcode=$?
    trap - EXIT ERR INT TERM HUP
    if ((OK)); then return 0; fi
    if ((DID_START)); then
        set +e
        log 'Install failed/interrupted: restoring owned files and service states; backup retained'
        systemctl stop portbw-watch.timer portbw-watch.service >/dev/null 2>&1
        if ((had_code)); then
            cp -a -- "$BACKUP/portbw.py" "$CODE"
            [[ -d "$BACKUP/etc-portbw" ]] && { rm -rf -- /etc/portbw; cp -a -- "$BACKUP/etc-portbw" /etc/portbw; }
            [[ -d "$BACKUP/var-lib-portbw" ]] && { rm -rf -- "$STATE_DIR"; cp -a -- "$BACKUP/var-lib-portbw" "$STATE_DIR"; }
        else
            rm -f -- "$CODE" "$CLI"
            rm -f -- "$CONF"
            for unit in "${UNITS[@]}"; do
                systemctl disable "$unit" >/dev/null 2>&1 || true
            done
        fi
        for unit in "${UNITS[@]}"; do
            if [[ -f "$BACKUP/$unit" ]]; then
                cp -a -- "$BACKUP/$unit" "$UNIT_DIR/$unit"
            else
                rm -f -- "$UNIT_DIR/$unit"
            fi
        done
        systemctl daemon-reload >/dev/null 2>&1
        if ((had_code)); then
            "$CLI" repair >/dev/null 2>&1 || log 'WARNING: old policy repair needs manual inspection'
            ((old_watch_active)) && systemctl start portbw-watch.service >/dev/null 2>&1
            ((old_timer_active)) && systemctl start portbw-watch.timer >/dev/null 2>&1
        else
            log 'NOTE: nft table/clsact created before failure, if any, is left for manual owner-safe inspection'
        fi
        log "Inspect: $BACKUP"
        set -e
    fi
    return "$exitcode"
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

DID_START=1
if ((existing)); then
    log 'Stopping ONLY portbw-watch.service and portbw-watch.timer; kernel rules stay in place'
    systemctl stop portbw-watch.timer portbw-watch.service
fi
install -d -m 0755 /usr/local/lib/portbw /usr/local/sbin
install -m 0755 -- "$SOURCE" "$CODE.tmp.$$"
mv -f -- "$CODE.tmp.$$" "$CODE"
if (( !existing )); then
    ln -s -- "$CODE" "$CLI"
fi

args=(install --iface "$IFACE")
if ((NFT_ONLY)); then args+=(--nft-only); fi
"$CLI" "${args[@]}"
# An enabled timer is a backup; start the nominal 1s resident daemon explicitly.
systemctl start portbw-watch.service
"$CLI" audit
systemctl is-active --quiet portbw-watch.service || fail 'watchd failed to become active'

# Verify that real kernel probes eventually succeed after service startup.
python3 - <<'PY'
import json, pathlib, subprocess, time
p=pathlib.Path('/run/portbw/watchd.json')
for _ in range(50):
    try:
        h=json.loads(p.read_text())
        pid=int(subprocess.check_output(['systemctl','show','portbw-watch.service','-p','MainPID','--value'],text=True))
        age=time.clock_gettime(time.CLOCK_BOOTTIME)-h['last_success']
        if pid>0 and h.get('pid')==pid and h.get('error') is None and 0<=age<3 and h.get('successes',0)>=2:
            print(f'[portbw-install] watchd PASS: pid={pid} heartbeat={age:.2f}s')
            break
    except (OSError, ValueError, TypeError, KeyError):
        pass
    time.sleep(.3)
else:
    raise SystemExit('No healthy resident watchd heartbeat within 15s')
PY
OK=1
log 'INSTALL/UPGRADE PASS: portbw source, kernel audit, and resident service healthy'
log "Iface=$IFACE; existing policy count will be shown by: portbw status"
log "Backup retained: $BACKUP"
log 'Next: configure your OWN ports using portbw set / portbw auto set, then portbw audit'
