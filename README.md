# portbw — 完全独立的 TCP 端口双向限速管理器

**状态：可供测试机安装的完整源码，尚未经目标 VPS / 家宽 NAT 机真实验收。请勿将离线测试结果当成商用保证。**

这套管理器不读取或删除 VLESS、SOCKS5、3proxy、WireGuard 的客户或节点；**节点不存在时端口限速仍永久保存**。只有手动 `portbw del <端口>` 才撤销。所有数据独立存放 `/etc/portbw/config.json`、`/var/lib/portbw/ports/*.json`、`/run/portbw/manager.lock`，nftables 只操作 `inet pbw_policy`，tc 只操作指定网卡的 **`clsact` filter 优先级 = TCP 端口、保留 handle 命名空间 `0x0b700000|端口`**；**不会清空 root qdisc、修改 BBR、WG 配置、VLESS 服务、全局 nftables ruleset、iptables**。

## 原理与严格边界

- `portbw set 40001 10 30`：从客户端视角，流入服务器该端口的 TCP 流量上传总额度 10 Mbps；从该服务端口流出的下载总额度 30 Mbps（十进制 megabits/second）。所有并发连接共用一个 nft **named limit object**，IPv4 + IPv6 也共用。`0` 代表该方向无限速。
- nftables 使用 `inet input/output` 两条链，匹配 TCP `dport` / `sport`，超出速率时丢包；**不是**“每个 TCP 连接一份速度”。`tc` 在所选网卡的 ingress / egress 上添加独立 `flower + police` 保护，**不接管 root qdisc**。
- `tc` 备用层的 IPv4 / IPv6 flower 使用不同的警察桶；双栈**严格共享总额度以 nft 主层为准**。`tc` 只是第二道保障，不是另一个总额度协议保证。
- `tc` 和 nft 都是**丢包式限速**，不是 HTB 平滑整形。突发包可超过瞬间显示速度；TCP 会重传，实测吞吐和丢包率受 MTU、RTT、BBR、负载影响。
- 如果整张 nftables 表和所有 `tc` qdisc 同时被删掉，**本工具不可能瞬间阻断现有 VLESS/SOCKS5 连接**。watchdog 负责定期恢复并报告，而不是虚称 100% 物理 fail-closed。要保证零窗口，必须额外让各服务主动依赖启动门禁并防止外部删除两道规则，这会修改原版服务，当前版本按你的要求不这样做。
- 防护以本地**服务器监听端口**为匹配条件；不是按 SOCKS5 **远端出口网站端口**限速，也不会把经过 WG 的转发流量误当作本地监听端口。若节点公网端口经 NAT 映射到本机 `41001`，规则应该配置在本机 `41001` 上。
- tc 层只管理选择的 **一个网卡**（安装 `--iface`）；nft 层覆盖全部本地 TCP input/output。多网卡、多隧道或包含其他服务端口号的流量，需要先评估匹配范围。

## 依赖和安装

Debian 11+ 或 Ubuntu 20.04+，root、systemd、Linux nftables、iproute2。先在**测试机**安装；依赖应先由系统管理员准备（不擅自修改系统 apt）。

```bash
sudo apt-get install -y python3 nftables iproute2 util-linux
cd portbw
sudo bash install.sh install --iface eth0
# 明确只安装 nft 单层，不启用 tc：
sudo bash install.sh install --iface eth0 --nft-only
```

正常默认是 **nft + tc 两层**。如果网卡已有 `ingress` qdisc 或同一个 tc `pref/handle` 的其他规则，工具应拒绝覆盖；`--nft-only` 可明确降级为单层。`clsact` 若原本存在则复用，若缺失则添加，但不删除既有 `clsact`。**不会安装/重启任何 VLESS、3proxy 或 WG 服务。**

> 本版要求 `tc -j` 能提供 flower handle / 匹配条件；审核 police 的实际速率时读取 `tc -s` 的相应 filter 文本。不同内核/iproute2 打印格式可能不同，识别不到会显示 `STALE` 并拒绝报告成功，不能把不认识的输出当成功。

## 日常命令

```bash
portbw set 40001 10 30        # 端口 40001：UP=10Mbps / DOWN=30Mbps
portbw set 40002 5 50
portbw set 40003 0 20         # 上传无限速、下载20Mbps
portbw up 40001 15            # 只改上传
portbw down 40001 60          # 只改下载
portbw set 40001 2.5 6.25     # 小数 Mbps

portbw list                   # 一键列出所有保存的端口与限速状态
portbw show 40001             # 某端口内核双层生效状态
portbw audit                  # 一键校验所有端口，异常时退出非零
portbw repair                 # 修复偏离状态的内核规则
portbw status                 # 全局配置状态

portbw del 40001              # 唯一正常取消规则的方式
```

`portbw list` 的 LISTEN 只是用 `ss` 粗略判断**当前端口是否监听**，没有监听并不删限速。永久的配置是硬盘上的元数据；发生中断后 `pending=true` 会保留，并在 watchdog 后续尝试恢复。每 30 秒检查一次，恢复时只处理**实际异常端口**，不会反复清空所有令牌桶。

## 运维、安全性

```bash
systemctl status portbw-watch.timer
journalctl -u portbw-watch.service -n 100 --no-pager
tc -j qdisc show dev eth0
tc -s filter show dev eth0 ingress
tc -s filter show dev eth0 egress
nft -a list table inet pbw_policy
```

1. 不要运行 `tc qdisc del dev eth0 root`、`nft flush ruleset` 来操作本工具。多工具冲突时优先停止安装，不覆盖别人的规则。
2. 本工具把 **tcp 源/目的端口**当作管理键，无法在某端口被复用于其他服务时自动区分旧客户。**这是特意按你的要求保留端口规则的结果**。
3. 对于小于约 0.012 Mbps 的速率会拒绝设置，防止桶太小导致 TCP 包全部丢弃；上限 100000 Mbps。单位为 1 Mbps = 125000 B/s。
4. 当前模式是只匹配 Linux **本机** input/output TCP 端口，不支持把被转发的 WireGuard Peer 流量按 VPS 的远端端口限速。
5. 当前不会读取第三方服务配置判断“哪个进程属于端口”，也不会为了严格模式擅自停止线上 VLESS 节点。
6. 源码变更与升级可直接重新运行安装器，政策文件不会删除。安装失败会尽量回滚程序文件，配置不成功会保留 `pending` 等状态供审计/修复。

## 已知限制与审查备注

- nft 字节模式令牌桶容量约为 `rate + burst`：空闲后的短时突发可按线速通过约 1 秒额度，持续过载才收敛到设定速率；短传输测速会偏高。
- **未验证风险**：tc 的 IPv4 与 IPv6 两条 flower 过滤器共用同一 `pref`（= 端口）。部分内核对「同 pref 不同 protocol」会返回 EINVAL，若真机出现，`tc` 层对 IPv6 会无法添加，需在真机确认后再调整 pref 方案（未擅自改动以保持现有布局兼容）。
- `install` 失败时除回滚程序文件外，也会回滚本次写入的 config.json 与 systemd unit；已保存的端口策略（含 `pending`）不受影响。
- 某方向限速为 0 时，同 pref 上的外部 tc 过滤器不再导致该端口持续修复失败（仅在需要在该方向限速时才拒绝覆盖）。

## 真实机验收必须做

- nft 动态管理：增加、调低、调高、单向取消、手动删除，同一端口 10 条并发连接总速度。
- 删除原版 VLESS 节点后确认限速仍在；同端口创建新 VLESS 节点后旧规则自动继续约束。
- IPv4、IPv6 各自连接，同端口限速；tc clsact 与机器已有 root `fq`、BBR、WG 路由、iptables 共存。
- 删除本工具 **tc flower** 确认 nft 仍限制；删除本工具 **nft 表** 确认 tc 仍有备用限速（双栈 fallback 非精确全局聚合）；watchdog 能恢复。
- 使用 `tc` 规则冲突端口验证它拒绝覆盖外部过滤器；不同 iproute2 版本验证 `tc -j` / `tc -s` 审计兼容。
- 重启/断电后限速策略恢复，已保存的规则没被重启节点误删除。

离线测试：`python3 -m unittest -v test_portbw`。

源码参考 Linux [nftables named limits](https://wiki.nftables.org/wiki-nftables/index.php/Limits) 与 [tc flower](https://man7.org/linux/man-pages/man8/tc-flower.8.html) / [tc police](https://manpages.debian.org/trixie/iproute2/tc-police.8.en.html)。


## 2026-10-08 安全修复版重要说明

本版修复了原有 `tc` 误认第三方规则、nft/tc 审计假阳性、watchdog 不能补建缺失自有基链、tc 查询失败被当空结果、安装回滚状态不全等问题；多端口 watch/audit/list 还复用了 nft + tc 快照。**完整故障路径与升级约束见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。**

### 旧版本（handle=端口）的重要升级步骤

新版**拒绝自动认领**旧版 `tc flower handle=端口`，也不会替你删除外部规则。旧机器如启用了 tc 第二层，请务必在维护窗口执行：

```bash
systemctl stop portbw-watch.timer
systemctl stop portbw-watch.service
# 确认旧的 portbw-watch.service 已停止，且 nft 主限速仍有效
cd /root/portbw_fixed_20261008  # 修改为你解压的实际目录
python3 -B portbw.py migrate-legacy 40001 --confirm  # 每个旧端口逐个执行
bash install.sh update
portbw audit
```

上述步骤要求你先备份 `/etc/portbw`、`/var/lib/portbw` 并通过 `portbw list` 记录限速端口。若 `migrate-legacy` 拒绝执行，请检查对应的外部 `tc` 规则，**不要**执行全局清空命令。新装机器直接 `bash install.sh install --iface eth0` 即可。

> 保持旧版 VLESS/WG/SOCKS5 完全不变仍是强约束，因此双层防护同时被系统或第三方命令删除时没有“零毫秒 fail-closed”保证；任何静态测试也无法代替真实内核及上游端口映射实测。
