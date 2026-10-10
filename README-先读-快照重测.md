# portbw 第5.2版：修复 systemd 1秒检测启动限流 / linshi 真机验证包

**2026-10-10 | 测试候选版，不是已发布的正式版。** 仅在可快照恢复、有带外控制台的 Debian/Ubuntu VPS 上验证；不修改 `zuizhongheji` 正式仓库。

## 一、这次改了什么？为什么？

在第5.1版 Debian 12 真机测试中，`OnUnitInactiveSec=1s` 周期的 `portbw-watch.service` 多次出现 `start-limit-hit`。手工添加

```ini
[Unit]
StartLimitIntervalSec=0
```

后，真实内核确认443端口 100→20 Mbps 降速、120秒后恢复，以及冷却后再次触发均成功。第5.2版把这个修复直接合入 `portbw.py` 自动生成的 `portbw-watch.service`，**快照恢复后的全新安装无需手工创建 drop-in**。

同时增强安装防护：

1. `portbw install` 在 `daemon-reload` 后读取 `systemctl show portbw-watch.service -p StartLimitIntervalUSec`，只有**实际生效值为0**才继续；否则回滚此前改动的配置及 unit，避免误报成功。
2. 通过检查后执行 `systemctl reset-failed portbw-watch.service`，清除升级自第5.1版时可能残留的 `start-limit-hit` 状态。
3. 外层 `portbw-install.sh` 在审计通过、timer 运行后，再独立核实同一属性；若不符，触发外层文件回滚。
4. 保留第5.1版 `--after 1s` 起、`OnUnitInactiveSec=1s`、0.5秒最短有效采样窗口与适应性采样间隔检查；**未变更**两方向、四过滤器共享 tc police、nft、状态机、持久化状态格式、Xray/Reality 服务或 root qdisc。

特别注意：`StartLimitIntervalSec=0` 仅作用于 `portbw-watch.service`，**不是关闭 systemd 全局保护**。1秒轮询不是硬实时。关闭启动频率限制后，要监控系统资源和服务失败日志；如程序频繁出错，应及时停用 timer 查错。若你安装了其他 drop-in 覆盖这个设置，安装器会拒绝宣称成功。

## 二、发布包文件说明

- `portbw.py`：**完整**第5.2版独立端口限速主程序，包含 `portbw install / set / auto / audit / repair / del`，不是增量补丁。
- `portbw-install.sh`：**完整**GitHub远程/本地双模式安装器，内置 `portbw.py` SHA256，自动装缺少的依赖，保留已有配置，支持文件回滚。
- `test_portbw.py`：71项离线回归测试。
- `portbw_443_monitor.py`：此前在真机上使用过的443逐秒只读监测脚本；本包附带以方便本轮重测，**安装器不会自动安装或执行它**。
- `changes_v5.1_to_v5.2.patch`：相对已测通第5.1版的源码/安装器/回归测试差异。
- `SHA256SUMS`：包内各文件哈希；`test-results.txt`：本次测试与工具检查记录。

**GitHub 最小上传集：仅 `portbw.py` 与 `portbw-install.sh` 两个文件，但必须一起替换成第5.2版。** 推荐将其他文件一起传到 `linshi/main` 根目录供后续复盘，`README` 不参与安装。

## 三、操作步骤：恢复快照后重新安装

1. 在云服务商恢复你准备好的测试机快照，并确认 **SSH 22** 与 VLESS+Reality **443** 都正常。
2. 把第5.2版 ZIP 解压，将 `portbw.py`、`portbw-install.sh` **同时上传**到仓库 `https://github.com/liucong552-art/linshi` 的 `main` 根目录；若仓库已有同名文件，在网页端覆盖旧版。注意 ZIP 本身不能当作脚本上传后直接一键安装。
3. 可以先在服务器核对 raw 源码确实可访问（不需要克隆 Git）：

```bash
curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw-install.sh' | grep -m 1 'v5.2'
```

4. Debian 12 root 执行熟悉的一键安装命令：

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/portbw-install.sh')
```

如果两个文件的版本不匹配，安装器会报 SHA256 错并停止；**切勿删除哈希校验绕过它**。

5. 立即核对安装结果：

```bash
portbw audit
systemctl show portbw-watch.service -p StartLimitIntervalUSec -p Result
systemctl cat portbw-watch.service
systemctl cat portbw-watch.timer
systemctl is-active portbw-watch.timer
```

**必须**看到 `StartLimitIntervalUSec=0`、watch service 的 `[Unit]` 内 `StartLimitIntervalSec=0`、timer 内 `OnUnitInactiveSec=1s`，并且定时器处于 `active`。第一次安装、无端口策略时 `portbw audit` 只有表头是正常的。

6. 等待15–20秒后检查是否仍有新的启动失败：

```bash
journalctl -u portbw-watch.service --since '20 seconds ago' --no-pager | tail -60
```

`Finished`/`Deactivated successfully` 正常；若有新的 `start-limit-hit` 或 Python 异常，先停止压测并保留日志。

## 四、443真机限速验证：4秒触发、2分钟恢复

在 VPS 设置（测试的是 `VPS → Windows` 下载方向）：

```bash
portbw auto set 443 \
  --up 1000 --down 100 --trigger 60 \
  --after 4s --auto-up 999 --auto-down 20 \
  --hold 2m --cooldown 60s
portbw auto status 443
portbw audit
```

含义：100 Mbps基础下载限速，实际负载持续高于60 Mbps的有效采样时间累计4秒，自动下降到20 Mbps；**保持120秒**，自动恢复100 Mbps，然后冷却60秒。上传1000→999 Mbps只是减少对本次下载测试的干扰，**不是上传无限速**。

开启另一个 SSH 窗口：

```bash
python3 /root/portbw_443_monitor.py --port 443 --seconds 240 --interval 1
```

该监测工具没有被自动安装。在 Windows 中下载本包的 `portbw_443_monitor.py` 后，用以下命令上传：

```powershell
scp -i "$env:USERPROFILE\.ssh\vps_root_ed25519" "$env:USERPROFILE\Downloads\portbw_443_monitor.py" root@162.211.231.219:/root/
```

监测运行时，让 Windows VLESS+Reality 客户端持续通过443下载大文件（而不是直连），使下载方向超过60 Mbps。脚本生成 `/root/portbw-443-monitor.csv`，记录每秒实际 `tc police` 内核限速值和计数器变化。

**上次已经测得** `07:32:32` 高流量 → `07:32:36` 100→20 Mbps → `07:34:36` 20→100 Mbps；本次恢复快照是对内置修复的**重新验收**，不能提前假定结果相同。首次切换后应核验 `portbw audit`。内核 `action_seen` 是进入限速器的流量，不等于客户端的净接收量。

完成后可从 Windows 拉取 CSV：

```powershell
scp -i "$env:USERPROFILE\.ssh\vps_root_ed25519" root@162.211.231.219:/root/portbw-443-monitor.csv "$env:USERPROFILE\Downloads\"
```

## 五、额外三项验收（建议执行后再发布正式仓库）

**A. 最短1秒触发：** 重新执行上面的 `portbw auto set`，只把 `--after 4s` 改成 `--after 1s`，重新记录 `RATE_CHANGED` 事件。1秒是**有效采样窗口的最短阈值**，不承诺客户端从流量开始1.000秒内完成内核切换。避免在同一轮传输中频繁修改策略，否则会重建采样基线。

**B. 多连接聚合：** 让多个 Windows 客户端/并发下载同时经过443，观察所有下载流量是否仍共用一个 `0x6d000377` 的 `tc police`；服务端查看 `tc -s actions ls action police`，同时检查客户端合计速度。当前通过的是四过滤器共享同一 action 的离线检查和真机单主连接限速；**尚未完成真实多客户端总吞吐验收**。

**C. 重启恢复：** 在停掉压测并保持443策略的情况下执行 `reboot`，SSH重新连接后运行 `portbw audit`、`portbw auto status 443`、`systemctl is-active portbw-watch.timer`、`systemctl show portbw-watch.service -p StartLimitIntervalUSec`，以及 `tc -s actions ls action police | grep -A 5 'police 0x6d000377 ' `。再次确认443由Xray监听，系统没有丢失策略或错误维持降速。

## 六、故障处置与恢复

```bash
portbw auto off 443   # 停自动，恢复静态基础100 Mbps（仍有限速）
portbw del 443        # 移除443 portbw限速策略，不会删除VLESS节点
portbw audit
```

如果 watch 服务异常但需保留限速规则，可先 `systemctl stop portbw-watch.timer`，收集 `journalctl -u portbw-watch.service -n 120 --no-pager`，再分析原因。**不要**使用全局 `nft flush ruleset` 或删除根 qdisc。测试机若异常，按你的 VPS 快照回滚。

## 七、离线检查

在解压目录中执行：

```bash
sha256sum -c SHA256SUMS
python3 -m unittest -q test_portbw
python3 -m py_compile portbw.py test_portbw.py portbw_443_monitor.py
bash -n portbw-install.sh
```

本次离线模拟及 unit 语法检查已通过；**没有声称在恢复快照后的 VPS 上测试过第5.2版**。正式仓库 `zuizhongheji` 维持不变，待你在 `linshi` 完成下一轮验收后再发布。
