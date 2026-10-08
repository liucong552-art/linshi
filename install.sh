#!/usr/bin/env bash
# Standalone portbw installer; never modifies original VLESS/SOCKS5/WG units.
set -Eeuo pipefail
umask 077
[[ ${EUID:-999} -eq 0 ]] || { echo '请使用 root' >&2; exit 1; }
DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$DIR/portbw.py" ]] || { echo '缺少 portbw.py' >&2; exit 1; }
ACTION="${1:-install}"
[[ "$ACTION" == install || "$ACTION" == update ]] || { echo '用法: bash install.sh install --iface eth0 [--nft-only] | update' >&2; exit 2; }
shift || true
if [[ "${AUTO_DEPS:-0}" == 1 ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -o Acquire::Retries=3
  apt-get install -y python3 nftables iproute2 util-linux coreutils
fi
for name in python3 nft tc systemctl ip ss flock timeout; do
  command -v "$name" >/dev/null || { echo "缺少命令: $name (AUTO_DEPS=1 可自动安装)" >&2; exit 1; }
done
python3 -B -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf8").read(),filename=sys.argv[1])' "$DIR/portbw.py"
install -d -m 0755 /usr/local/lib/portbw /usr/local/sbin
install -d -m 0700 /run/portbw
exec 9>/run/portbw/install.lock
flock -w 120 9 || { echo '安装锁繁忙' >&2; exit 1; }
OLD_DIR=""
PROGRAM=/usr/local/lib/portbw/portbw.py
WRAPPER=/usr/local/sbin/portbw
SUCCESS=0
ARMED=0
STAGED=()
PRESENT=()
ACTIVE_TIMER=0
systemctl is-active --quiet portbw-watch.timer && ACTIVE_TIMER=1 || true
WAS_ACTIVE_WORKER=0
systemctl is-active --quiet portbw-watch.service && WAS_ACTIVE_WORKER=1 || true
on_exit(){
  rc=$?
  trap - EXIT ERR INT TERM HUP
  if [[ "$SUCCESS" != 1 && "$ARMED" == 1 ]]; then
    for idx in "${!PRESENT[@]}"; do
      local_path="${PRESENT[$idx]}"
      if [[ -f "$OLD_DIR/$idx.present" ]]; then
        cp -a -- "$OLD_DIR/$idx.old" "$local_path" || echo "回滚失败: $local_path" >&2
      else
        rm -f -- "$local_path" || true
      fi
    done
    echo 'portbw 文件已尝试回滚。内核中的旧规则保持不清空；请检查 portbw audit。' >&2
  fi
  if (( ${#STAGED[@]} > 0 )); then rm -f -- "${STAGED[@]}" || true; fi
  if [[ -n "$OLD_DIR" ]]; then rm -rf -- "$OLD_DIR" || true; fi
  if [[ "$SUCCESS" != 1 && "$ACTIVE_TIMER" == 1 ]]; then
    systemctl start portbw-watch.timer >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
# Trap MUST be armed before any backups are taken.
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
OLD_DIR="$(mktemp -d /var/tmp/portbw-install.XXXXXX)"
for path in "$PROGRAM" "$WRAPPER"; do
  idx="${#PRESENT[@]}"
  if [[ -e "$path" || -L "$path" ]]; then
    cp -a -- "$path" "$OLD_DIR/$idx.old"
    : >"$OLD_DIR/$idx.present"
  fi
  PRESENT+=("$path")
done
ARMED=1
systemctl stop portbw-watch.timer >/dev/null 2>&1 || true
if [[ "$WAS_ACTIVE_WORKER" == 1 ]]; then
  timeout 30 systemctl stop portbw-watch.service >/dev/null 2>&1 || {
    echo '旧 portbw 工作进程仍在运行，拒绝替换' >&2; exit 1;
  }
fi
STAGED=("$PROGRAM.new.$$" "$WRAPPER.new.$$")
install -m 0700 "$DIR/portbw.py" "${STAGED[0]}"
mv -f "${STAGED[0]}" "$PROGRAM"
cat >"${STAGED[1]}" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
exec /usr/bin/python3 /usr/local/lib/portbw/portbw.py "$@"
EOF
chmod 0755 "${STAGED[1]}"
mv -f "${STAGED[1]}" "$WRAPPER"
"$WRAPPER" install "$@"
SUCCESS=1
