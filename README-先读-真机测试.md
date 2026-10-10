# portbw 第5.1版：1秒起步自动限速 / linshi 真机测试候选包

> 2026-10-10。**仅在可快照恢复、可带外登录的 VPS 上验证。** 已完成67项离线模拟测试，尚未在真实 Linux 网络栈中完成吞吐/响应时间/资源开销验收。不要把离线通过当作生产验收。

## 修改内容（相对已安装第5版）

- `--after` 接受从 `1s` 到30天之间的整数秒/分钟/小时/天；`0s` 拒绝。原有60秒和更长记录保持兼容，数据格式和 `AUTO_VERSION` 不变。
- 监测从 `OnUnitInactiveSec=30s` 改为 `1s`；`AccuracySec=100ms`。这是 **1秒一轮的尽力调度**，并非硬实时，也非固定每秒完成一次采样。系统忙或 tc/nft 命令执行较慢时，实际间隔可能超过1秒。
- 最短有效测量窗口从15秒改为0.5秒。短阈值使用更严格的最大窗口：`after <= 5s` 时上限8秒；更大的短阈值随 `after` 增长；原来的 `after >= 60s` 仍接受最多75秒窗口。过短、过长、计数器重建及计数异常时清除连续进度，不会假装已持续超过阈值。
- 未修改 tc police 共享限速架构、nftables 规则、双方向独立控制、状态/保护/冷却/防冲突逻辑，也不修改 Xray、WireGuard、root qdisc。
- 安装器仍支持 `linshi/main` 同版本远程拉取，两文件必须一起上传；安装器中的 SHA256 已更新。升级时保留端口策略及其它服务。

**注意：设置 `--after 1s` 仅代表「在有效的采样窗口中累计至少1秒超过阈值」，不代表达到阈值1秒后必然已经降速。** 首次观察需先建立基线，读写内核对象和 systemd 调度都会产生延迟。应记录流量开始、进入 hold 和客户端速度变化的实际时间。

## 先上传 GitHub，再执行远程安装/更新

把此 ZIP **解压**，把其中 `portbw.py` 和 `portbw-install.sh` 上传到 GitHub 测试仓库 [liucong552-art/linshi](https://github.com/liucong552-art/linshi) 的 **main 根目录**（其他文件可一并上传）。上传时必须 **同时更新两个文件**。确认 raw 地址能打开源码：

- `https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw.py`
- `https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw-install.sh`

在 Debian/Ubuntu、已安装第5版的测试 VPS（root）执行：

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw-install.sh')
```

安装器自动核验下载的 Python 文件 SHA256；若 GitHub 两个文件尚未同步，安装会拒绝混用。也可以本地把两文件放在同一目录，用 `bash ./portbw-install.sh install --iface eth0` 安装；`eth0` 要换成正确的出口网卡。

### 安装后先确认新版 systemd 定时器

```bash
portbw audit
systemctl cat portbw-watch.timer
systemctl is-active portbw-watch.timer
systemctl list-timers --all portbw-watch.timer
journalctl -u portbw-watch.service -n 30 --no-pager
```

需要看到新定时器里的 `OnUnitInactiveSec=1s` 和 `AccuracySec=100ms`。如果还是 `30s`，说明实际没有更新成功。请查看安装日志，不要手工修改 timer 试图绕过程序版本检查。

## 443 主节点测试：100 Mbps -> 20 Mbps

**这台测试机的443端口属于 VLESS 主节点，会影响所有在该端口上的客户端。** 测试前务必确认 SSH 22 端口和云服务商控制台可用；如果443已有重要业务，请改用独立测试端口。先备份 `etc/portbw`、`var/lib/portbw`、`/etc/systemd/system/portbw-*` 和 nft/tc 现状，并准备云快照。

在 VPS 上执行（上传和自动上传上限设置为1000/999 Mbps，仅用于减少本轮下载测试中上行方向的干扰，**并非真正关闭上行限制**）：

```bash
portbw auto set 443 \
  --up 1000 --down 100 --trigger 60 \
  --after 4s --auto-up 999 --auto-down 20 \
  --hold 2m --cooldown 60s
portbw auto status 443
portbw show 443
portbw audit
```

此测试相当于：客户端经443代理的 **VPS→客户端方向** 聚合出口流量，超过基础速度的60%（60 Mbps）并满足连续4秒的有效采样后，目标限速降为20 Mbps；核验生效后保持2分钟，恢复100 Mbps，再冷却60秒。多个客户端会共享带宽额度；浏览器测速还受链路和测速端限制。

要测试最短1秒，**完成4秒测试后再执行相同命令，仅把 `--after 4s` 改成 `--after 1s`**；不要在一轮压测中来回改配置，因为每次配置变更会重置采样基线。

Windows 端必须通过 **连接这个 VPS 443 的 VLESS 代理** 持续传输，才能产生受限速的流量；普通 Windows 直连测速不经过443，无法验证。服务器端观察：

```bash
portbw auto status 443
portbw audit
sudo tc -s actions ls action police
journalctl -u portbw-watch.service -n 80 --no-pager
```

建议用另一台机器或独立 SSH 会话每隔1–2秒记录 `portbw auto status 443`（此命令只读，不重置进度），同时在客户端记录实时吞吐曲线。应查看 `down` 从 `monitor` → `hold` → `cooldown` → `monitor` 的转换、实际 `tc` 统计值和客户端真实速率；单看 `当前目标=20` 不足以证明内核已生效。若低流量始终不触发，是正常现象。

### 取消443限速、停止测试

```bash
portbw auto off 443   # 关闭自动模式，恢复静态基础值100 Mbps（仍有限速）
portbw del 443        # 彻底取消443端口的 portbw 限速
portbw audit
```

`portbw del` 不会删除VLESS节点，但它只删除该端口的 portbw 策略，并不等于恢复所有 Linux tc/nft 状态。发生不符合预期的限速、SSH 或网络异常时，应通过带外控制台和测试机快照恢复；不要盲目执行 `nft flush ruleset` 或 `tc qdisc del root`。

## 验收界限与资源开销

- 1秒轮询会反复调用 nft/tc 查询，比第5版30秒轮询的 CPU、fork/exec 和缓存读写开销显著增加。**多端口场景必须额外监测 CPU、系统负载和运行耗时**；不要据此认定适合生产常驻。
- `OnUnitInactiveSec=1s` 从上一轮 service 结束后计时；若一次检查耗时0.4秒，轮询间隔并不是严格1.000秒。Linux tc 查询耗时及舍入也会造成误差。
- 1–4秒模式对短时突刺敏感，但不会用明显延迟的长窗口伪造连续性；发现异常会重建采样基线，可能出现晚触发或不触发，而不是提前恢复保护。
- 不能通过模拟测试验证TCP/UDP、IPv4/IPv6真实共用police额度；必须实机测。仍不涵盖WireGuard FORWARD转发数据。
- 更新完成后先跑 `portbw audit`，再压测；记录测试机内核版本、`tc -V`、`nft -v`、执行日志、客户端测速曲线，以及每个阶段耗时。

## 本地离线验证

```bash
sha256sum -c SHA256SUMS
python3 -m unittest -q test_portbw
python3 -m py_compile portbw.py test_portbw.py
bash -n portbw-install.sh
```

SHA256SUMS 是上传包内一致性检查，不应被视为 GitHub 原始内容的独立信任保证。`test_portbw.py` 的内核调用均使用模拟数据。源码 diff 在 `changes_v5_to_v5.1.patch`。
