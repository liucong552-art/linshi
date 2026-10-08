# portbw — TCP/UDP 共享总限速（linshi 测试版）

**本版仅用于恢复快照后的测试机；尚未通过 Debian 12 真机混合 TCP+UDP 压测。** 不需要兼容已经安装过旧 portbw 的服务器。

将压缩包内 `install.sh`、`portbw.py`、`README.md` **三个文件**上传到 `liucong552-art/linshi` 的 `main` 分支根目录（不是上传 ZIP）。`zuizhongheji` 正式仓库暂时不动。

## 一键安装

先恢复到**未安装过 portbw** 的 VPS 快照，以 root 执行：

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/install.sh')
```

安装器自动安装缺少的依赖、识别真实出口网卡、校验 `portbw.py` SHA256，并创建独立 nftables 规则、tc 共享 policer、开机恢复和约 30 秒定时检查，不修改 Xray/VLESS、WireGuard 或网卡 root qdisc。

## 使用方法（命令保持不变）

```bash
portbw set 45123 10 20  # 45123：上传 TCP+UDP 共用 10Mbps，下载共用 20Mbps
portbw up 45123 15      # 只调整上传
portbw down 45123 30    # 只调整下载
portbw show 45123       # 查看单端口限速及监听
portbw audit            # 检查规则
portbw del 45123        # 仅删除该端口限速，不删除节点
```

上传与下载各有**一份共享额度**，不是上下行合计；同端口的 TCP/UDP、IPv4/IPv6 各自竞争该方向的**同一个**限速桶。其他端口不会分摊这份额度。限速采取超额丢包（policing），允许少量短时突发；不是“每毫秒绝对不超”的整形器。仅匹配 VPS 本机 `input dport`、`output sport`，不包含所有 WG/NAT `FORWARD` 转发流量。

## 测试验收

先检查：

```bash
portbw set 45123 10 20
portbw audit
portbw show 45123
systemctl is-enabled portbw-watch.timer
systemctl is-active portbw-watch.timer
```

真正验收时需对**同一个测试端口**分别进行 TCP、UDP、TCP+UDP 并发测速，确认上行合计约 10Mbps、下行合计约 20Mbps（考虑协议开销与测量波动）。再验证 `reboot` 后仍然生效、其他端口不受影响、`portbw del 45123` 后 nft/tc 无残留。不要直接在付费客户端口上做破坏性测试。

如命令失败，把完整报错和 `portbw audit` 输出发回；**不要清空 nftables 全局规则，不要删除 `eth0` 的 root qdisc**。实机混合测试通过前，不要将本测试版覆盖到正式仓库。
