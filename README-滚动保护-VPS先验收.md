# portbw 5.2.2：双指标滚动保护，VPS优先测试候选版

**尚未在真实 VPS 上验收；请勿立即推送到 `linshi` 或 `zuizhongheji`。**

来源：在此前已经通过服务器现场测试的 **v5.2.1** 基础上，仅迭代自动降速保持逻辑，并保留已修复的 systemd `start-limit-hit`、首次安装 `reset-failed unit not loaded` 问题。`nft/tc` 端口规则及 VLESS/Reality 节点不修改，`tc police` 是丢包限速而非排队整形。

## 设计含义

基础下载100 Mbps；下载超过60 Mbps连续1秒后降至20 Mbps；保持时间120秒；恢复100 Mbps后冷却60秒。

**不同于v5.2.1**：降速后不会到120秒就无条件放行。watch继续从**443下载方向共享的单个 tc police action**读取每次取样的 `Sent bytes`、`dropped`、`overlimits`。只要持续满足任一条件，并经过两个连续可靠采样窗口（通常约2秒）确认，就继续20 Mbps滚动保护：

- 进入 tc police 的字节流量按**保守采样间隔**计算，达到当前 **20 Mbps限定值的85%** 以上。
- 流量至少达到限定值的 **20%**，且**平均每秒至少3次超限或丢包**。要求同时有实质流量，避免偶发一两个丢包触发无限续期。

`Sent` 反映 action 看到的流量，不等于客户端成功收到的净速率。这两个指标是高负载/拥塞**证据**，并不是精准交付测速。如果流量已经稳定低于上述压力条件，或统计丢失/计数器重建，便停止续期。低负载开始后，按**最后一次已确认繁忙采样时间 + 120秒**恢复基础速率。出现监测失败时不会凭空继续无限延长，既防止永久卡在20 Mbps，也遵守旧版限速恢复能力。

不在每次1秒采样时写永久状态：持续高负载时，只有原截止时间即将接近半程才批量持久化；负载转低时补写最新截止时间。内存态缓存仍存 `/run/portbw/auto`。保留每端口、上传下载方向独立的配置与计数。最短1秒触发仍是尽力调度，不承诺毫秒级实时性。

**已有 `auto set 443` 的策略会在安装更新时保留原参数，升级后其120秒保持自动采用滚动语义；如要返回旧版固定时间语义，需恢复旧版程序，而不是误以为 `auto set` 可以关闭滚动。**

## 执行顺序：只在 VPS 上先验收

1. Windows 下载此 ZIP（`portbw-v5.2.2-rolling-vps-test.zip`）后，在本地 PowerShell 上传：

   ```powershell
   scp -i "$env:USERPROFILE\.ssh\vps_root_ed25519" "$env:USERPROFILE\Downloads\portbw-v5.2.2-rolling-vps-test.zip" root@162.211.231.219:/root/
   ```

2. SSH 在 VPS（Debian 12）执行：

   ```bash
   mkdir -p /root/portbw-v522-test
   python3 -m zipfile -e /root/portbw-v5.2.2-rolling-vps-test.zip /root/portbw-v522-test
   cd /root/portbw-v522-test
   sha256sum -c SHA256SUMS
   python3 -m unittest -q test_portbw
   bash -n portbw-install.sh
   bash ./portbw-install.sh
   portbw audit
   portbw auto status 443
   systemctl show portbw-watch.service -p StartLimitIntervalUSec -p Result
   ```

   安装从**本地同目录两个文件**读取源码；不会使用 GitHub 上仍为旧版的远程脚本。已有 `443` 策略应该保留。

3. **滚动保护实测（重点）**：在 VPS 运行只读监控（不要 Ctrl+C 提前结束）：

   ```bash
   python3 /root/portbw-v522-test/portbw_443_monitor.py \
     --port 443 --seconds 450 --interval 1
   ```

   Windows使用VLESS+Reality的443节点持续下载大文件，流量明显超过60 Mbps，**至少180秒**，且应在20 Mbps降速后仍能保持有效下载；在持续下载期间不能出现20→100 Mbps。随后停止下载并保持停止超过**125秒**，应看到20→100 Mbps（120秒目标加少量监测调度余量）。CSV默认保存在 `/root/portbw-443-monitor.csv`。

   如需明确对照日志：

   ```bash
   journalctl -u portbw-watch.service --since '10 minutes ago' -o short-iso-precise --no-pager \
     | grep -E '443 down:|Failed|错误'
   grep -E 'RATE_CHANGED|ERROR' /root/portbw-443-monitor.csv
   portbw auto status 443
   portbw audit
   ```

4. 多连接共享测试仍属正式发布前验收：通过VLESS并行下载多个任务时，查看同一个 `tc police 0x6d000377` 计数，结合客户端合计 `bytes_acked`/下载吞吐，确认聚合而不是每个连接各20 Mbps；`action_seen` 可能高于20，因为部分报文会被丢弃。原有VPS重启恢复已由5.2.1验证，升级后建议再次审计一次。

5. 只有收到这一候选版的**VPS真实日志和CSV**，确认持续高负载不提前恢复、停止后按期恢复及没有新错误，才向 `linshi` 上传经过验证的 `portbw.py` 与 `portbw-install.sh`；最后再移植到 `zuizhongheji`。

## 注意

- 不提供SSH私钥，也不要把SSH私钥上传聊天。
- 版本仅在本地回归环境使用mock通过85项测试；安装到你真实VPS的成功与效果必须看服务器反馈。
- 若更新失败，安装器包含原有文件回滚流程；内核当前策略与状态应使用 `portbw audit` 现场核对。
- 此安装会改变VPS上的portbw程序和systemd单位文件；不会主动修改Xray、VLESS、Reality、Root qdisc或其他节点。
