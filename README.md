# portbw — TCP/UDP 同端口合计限速（linshi 测试版）

**只适合恢复到从未安装过 portbw 的 VPS 快照。** 仍在实机验证，不要合并到 `zuizhongheji`。旧版共享 tc action 可能有残留，不能原地升级。

把这三个文件上传到 `liucong552-art/linshi` 的 `main` 分支**根目录**：`install.sh`、`portbw.py`、`README.md`。

## 一键安装

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/install.sh')
```

依赖、网卡、systemd 开机恢复及自检自动处理；不改变 Xray/VLESS、WireGuard、root `fq` 队列。

## 使用方法

```bash
portbw set 45123 10 20  # TCP+UDP 合计上传 10 Mbps、合计下载 20 Mbps
portbw up 45123 15      # 修改上传
portbw down 45123 30    # 修改下载
portbw down 45123 0     # 仅关闭下载限速
portbw show 45123       # 查看规则及监听状态
portbw audit            # 审计全部策略
portbw del 45123        # 取消该端口策略（不删除节点）
```

**计量方式**：同一端口，TCP+UDP、IPv4+IPv6，共享每个方向的总额度；上传和下载分别计量。超限丢包，允许短时突发。仅适用于 VPS 本机接收/发送的 TCP、UDP 流量，**不适用于所有 WG/NAT FORWARD 转发**。

**本次修复**：避免 nftables OUTPUT 超速丢包向 UDP 发包程序返回 `EPERM`；下载方向由 **tc egress 单个共享 policer** 限速、nft 只计数；上传由 nft+tc 共同执行。共享 tc action 由第一条 flower 创建；删除全部 flower 后如保留了未绑定 action，先核验其 `ref=1`、`bind=0` 和 `skip_hw`，再仅按本端口的 index 删除，绝不清空第三方规则。`--nft-only` 模式不允许下载限速，避免错误地宣称双向限速有效。

## 真机验收

在 **VPS** 先运行 `portbw set 45123 10 20` 和 `iperf3 -s -p 45123`；然后在 Windows **关闭代理 TUN 或确保该 VPS IP 直连**：

```powershell
iperf3 -4 -c 162.211.231.219 -p 45123 -u -b 30M -t 20 -O 2
iperf3 -4 -c 162.211.231.219 -p 45123 -u -b 50M -R -t 20 -O 2
```

接收端目标为上传约 10、下载约 20 Mbps；还需单独测试 TCP+UDP 同时满载的合计值。测试 `portbw down 45123 0`、`portbw down 45123 20` 和 `portbw del 45123` 的内核残留与审计结果。**离线检查不代表真机已通过。** 不要手动 `nft flush ruleset` 或 `tc qdisc del dev eth0 root`。

**本轮新增验证**：`portbw down` / `portbw del` 的共享 action 精确回收；内核可能在解绑后暂留 `ref=1,bind=0` 的 police action，允许只删除已核实属于本端口的无绑定对象。当前仅离线验证，需 Debian 12 真机验收。
