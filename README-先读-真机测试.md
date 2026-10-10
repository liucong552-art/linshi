# portbw 第5版：仅供独立测试 VPS 验证（2026-10-10）

**状态：离线回归通过；尚未在真实 Linux VPS 上验证。不要直接替换承载业务的生产限速器。**

## 一、为什么不直接用第四版

第四版 `records_for_watch()` 能绕过损坏的其他端口文件做采样，但在自动降速需要写入 tc 时，`ensure_tc_prefs()` 再次调用严格的 `records()`，一个坏文件就可能导致健康端口保留 `entering`/`pending`，实际内核不降速。

本第5版是对第四版的**最小定向修补**：

1. watchdog 已验证的健康端口如果已有自己的 4 个 `tc_prefs` 槽位，在其他端口配置损坏时也能进入后续 tc/nft 严格归属验证并尝试真正执行降速或恢复；不会改动坏端口。
2. **禁止**在其他端口配置损坏时为任何端口分配**新的** tc 槽位；并且在这种情况下提前拒绝，确保不会先修改 nft 再卡在 tc 分配步骤。
3. CLI 的普通交互式改速仍使用全量严格配置清单，拒绝在无法确认所有配置时隐式抢占槽位；watchdog 不会修改未验证的损坏端口。
4. 安装器同时支持本地两文件安装，或从 `linshi/main` 远程一键安装；无论哪种模式都强制核验完整 `portbw.py` 的 SHA256。远程安装需先将匹配的两文件上传到测试仓库。
5. 没有重写原有的双方向自动状态机、tc 共享 police action、nft 规则、systemd 工作方式和持久格式。

## 二、离线检查

```bash
unzip portbw-v5-realhost-candidate.zip -d /root/portbw-v5
cd /root/portbw-v5
sha256sum -c SHA256SUMS
python3 -m unittest -v test_portbw
python3 -m py_compile portbw.py test_portbw.py
bash -n portbw-install.sh
```

交付前结果：**57/57** 项标准库 unittest 通过，包括第四版原有51项和新增6项损坏配置隔离测试。所有 tc/nft 实际系统命令均被 mock；这**不表示**已经通过真机性能、协议、平台或恢复验收。

## 三、建议先做的真机安全准备

选用**可通过云控制台登录、可重装/还原快照**的独立 Debian/Ubuntu + systemd 测试 VPS；不要使用带生产 SSH 唯一接入、WireGuard 出口或正在提供重要代理服务的机器。先在云服务商创建快照，并记录真实物理出口网卡。

如果服务器已有第四版/旧版，请先保存：

```bash
sudo mkdir -p /root/portbw-v5-backup
sudo tar --ignore-failed-read -czf /root/portbw-v5-backup/files-before-v5.tgz -C / \
  etc/portbw var/lib/portbw usr/local/lib/portbw usr/local/sbin/portbw \
  etc/systemd/system/portbw-restore.service \
  etc/systemd/system/portbw-watch.service \
  etc/systemd/system/portbw-watch.timer
sudo nft -j -a list ruleset > /root/portbw-v5-backup/nft-before-v5.json
ip -4 route get 1.1.1.1
ip -br link
# 确定真实出口网卡，比如 eth0；随后另外保存该网卡 tc 状态：
sudo tc -s qdisc show dev eth0 > /root/portbw-v5-backup/tc-qdisc-before-v5.txt
sudo tc -s filter show dev eth0 ingress > /root/portbw-v5-backup/tc-ingress-before-v5.txt
sudo tc -s filter show dev eth0 egress > /root/portbw-v5-backup/tc-egress-before-v5.txt
```

上述 eth0 是示例！请换成实际出口接口。若你用远程 SSH 操作，先确认控制台救援路径可用。

### 测试仓库的一键安装（先发布，再运行）

```bash
cd /root/portbw-v5
sudo bash ./portbw-install.sh install --iface eth0
sudo portbw status
sudo portbw audit
systemctl is-active portbw-watch.timer
```

**从测试仓库安装（需先上传 `portbw.py` 与 `portbw-install.sh` 到 `linshi/main` 根目录）：**

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw-install.sh')
```

安装器会从同一仓库下载 `portbw.py` 并校验其固定 SHA256；若两文件版本不匹配将拒绝安装，不会静默使用旧版本。可附加 `--iface ens3` 显式设置物理出口网卡。

**本地离线安装：** 解压包后在该目录运行 `sudo bash ./portbw-install.sh install --iface eth0`。`--iface` 必须是测试服务器正确的物理出口；不要随意指定 `wg*` 或虚拟网卡。

### 先做静态限速，再做自动限速

仅在有真正测试服务监听的临时端口（下例 41001）执行。不要用 SSH、WireGuard、已有代理业务端口代替。

```bash
sudo portbw set 41001 10 20
sudo portbw show 41001
sudo portbw audit

sudo portbw auto set 41001 --up 10 --down 20 \
  --trigger 90 --after 60s --auto-up 5 --auto-down 8 \
  --hold 2m --cooldown 30s
sudo portbw auto status 41001
sudo portbw audit
journalctl -u portbw-watch.service -n 100 --no-pager
```

需要另一台主机持续连接到这个**实际监听端口**才能验证速度阈值。每30秒采样不等于每个30秒窗口必然有有效统计；首次建立统计基线以及短暂的窗口抖动属于正常情况。不要仅凭端口配置的目标Mbps判断 tc 生效，必须核查 `tc -s actions ls action police`、规则、实际应用层吞吐与 UDP 丢包。

## 四、真机验收清单

1. 低负载保持基础速度；持续高负载至少配置的 `after` 时长后进入 `entering → hold`，实际限速接近 `auto-up/auto-down`，不能只显示 pending。
2. 上传/下载分别压测：上传触发不得清零或误触发下载；再验证两边同时触发。
3. 在端口TCP、UDP和IPv4、IPv6（机器确实具备 IPv6 时）分别验证；四类流量共用每个方向一个 policer，不是四份额度。
4. 保护到期能按 `hold` 恢复基础值；升级程序、重启服务、重启整机后能恢复，不能假称已生效。
5. 观察 `portbw audit`、`auto status`、`journalctl`、`tc -s filter show`、`tc -s actions ls action police`；关注 pending、外部 tc 规则冲突、内核命令失败或采样中断。
6. **仅在独立测试 VPS** 用备份可恢复的故障注入模拟另一个端口配置损坏：已有槽位的健康端口应仍可降速、恢复；完全没有槽位的新端口应拒绝占用未知 tc pref。此时 watch 报错退出是预期的异常告警，不能把系统服务的非零返回误认作所有端口停用。
7. 若服务器已运行 `VLESS/3proxy/WireGuard`，必须另外确认原服务仍正常。当前限速器仍不覆盖任意 WireGuard `FORWARD` 传输。

## 五、停止与回退

对单独测试端口：

```bash
sudo portbw auto off 41001
sudo portbw del 41001
sudo portbw audit
```

此操作只移除该端口限速，不等于全系统恢复到测试前所有 `tc/nft` 状态。若第5版安装已经成功，而你想回退代码，请使用备份的第四版安装包（其自身两文件必须匹配）在测试机重新安装并重新审计；**不能**仅用 `cp` 覆盖旧Python文件就认定规则与计数器已回退。发生异常优先依赖云服务器快照恢复，并从带外控制台排查。

## 六、文件与修改边界

- `portbw.py`：完整的第5版限速器。
- `portbw-install.sh`：支持本地安装和同版本 GitHub 远程安装、SHA256 校验、安装事务回滚。
- `test_portbw.py`：57项离线测试，后6项复现/约束损坏兄弟端口情况下的安全行为。
- `changes_v4_to_v5.patch`：仅用于审查变更，不要直接在生产机用 patch 动态修改正在运行的文件。
- `test-results.txt`：本地测试日志。
- `SHA256SUMS`：除自身外的文件哈希。

**测试通过并不等于生产可用。** 首轮请优先记录真实 `tc/nft` 行为和失败日志，不要先追求高吞吐或复杂多端口场景。
