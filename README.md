# portbw — TCP 端口独立上传/下载限速（GitHub 三文件一键版）

**发布日期：2026-10-08。只需要向 GitHub 仓库 `liucong552-art/linshi` 的 `main` 分支根目录上传三个文件：**

```text
install.sh   # 一键安装入口：自动依赖、下载、校验、部署、systemd
portbw.py    # 完整的端口限速管理器（含 Debian 12 真机修复）
README.md    # 本说明
```

**不要只上传 ZIP，也不要上传多一层目录。** 原仓库如仍有 `install-core.sh`、`SHA256SUMS`、`test_*.py`、补丁、日志等旧文件，不再是本版本运行依赖，可从 GitHub 删除。以后要更新程序时，`install.sh` 内置的 SHA256 必须随 `portbw.py` 版本一起更新，否则安装器会安全拒绝混用旧/新版本。

## 恢复快照后，一条命令完成部署

恢复到未安装过 portbw 的 Debian/Ubuntu VPS 快照，以 root 登录并运行：

```bash
apt-get update && apt-get install -y curl ca-certificates && bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/install.sh')
```

安装器自动完成以下工作，无需另行 `git clone` 或安装补丁：

- 检查 Debian/Ubuntu、root 和 systemd；自动用 apt 安装缺少的 `curl`、`ca-certificates`、`python3`、`nftables`、`iproute2`（含 `tc`、`ip`、`ss`）、`util-linux`（`flock`）、`coreutils`。
- 从固定仓库 main 根目录下载 **仅一个必需的远程主体文件 `portbw.py`**；以 `install.sh` 内置 SHA256 校验，并检查 Python 语法。校验失败即停止，避免混版。
- 根据默认路由检测网卡（如 `eth0`、`ens3`），拒绝自动选择常见的 WG/TUN 等虚拟/隧道接口；已有配置则优先沿用以前的网卡与限速模式。
- 通过安装器的内置部署流程安装 `/usr/local/sbin/portbw`、`/usr/local/lib/portbw/portbw.py`，创建本工具自己的 nftables 表 `inet pbw_policy`，并在网卡上附加 `tc clsact`；保留现有 root `fq`。
- 通过 systemd 安装/启用 `portbw-restore.service` 和 `portbw-watch.timer`（约每 30 秒检查），最后调用 `portbw audit` 并确认 timer 已启用且运行。升级时备份程序文件并在安装失败时尝试回滚，不清空其他软件的 nft/tc 规则。

不会修改 Xray/VLESS、3proxy/SOCKS5、WireGuard、BBR、iptables、非本模块 nft 表或网卡 root qdisc。不会自行给现有客户端口施加限速。

> 支持边界：当前针对 Debian/Ubuntu + systemd，尤其在 Debian 12、Linux 6.1、nftables 1.0.6 的真机上验证了限速主体。**三文件一键安装器是离线校验版本，恢复快照后的首次部署仍需实际运行验证。** 如果底层内核不支持功能、现有 tc 过滤器冲突、网络/apt 故障，它会退出提示，不会静默降级到单层限速，也不会为了“自动安装成功”清空第三方规则。

## 安装后管理命令

```bash
portbw set 40001 10 20  # TCP 40001：客户端上传合计 10 Mbps，下载合计 20 Mbps
portbw set 40002 5 50   # 另一个端口，独立限速
portbw set 40003 0 20   # 只限下载
portbw up 40001 15      # 只调整上传
portbw down 40001 30    # 只调整下载
portbw list             # 列出所有保存的限速端口
portbw show 40001       # 查看端口状态
portbw audit            # 检查规则健康度
portbw status           # 显示配置及策略数量
portbw repair           # 必要时手动修复（通常由 watchdog 自动完成）
portbw del 40001        # **仅此命令**删除对应端口策略，不删除节点
```

上传/下载为十进制 Mbps（1 Mbps = 125000 bytes/s）。`portbw` 规则独立于节点生命周期：即使 VLESS/SOCKS5 客户端删除、过期或端口不再监听，规则依然保留，只有主动 `portbw del 端口` 才撤销。采用 nft 限速桶与 tc `flower police` 双层机制，超限通过丢包执行，短时测速可能存在突发与 TCP 重传。

**这里只针对服务器本机 TCP `input dport` 与 `output sport`，不是 UDP 或任意 NAT/WG `FORWARD` 转发流量限速器。** nft 双栈共用对应方向的限速对象，tc IPv4/IPv6 使用各自的过滤器；在真实网卡未配置可路由 IPv6 的 VPS 上只验证过 IPv4 的吞吐。

## 已修复的真实 Debian 12 问题

1. **nftables 1.0.6 字段差异**：实际 JSON 使用 `rate_unit=bytes`，并把突发量表示为 `burst=128, burst_unit=kbytes`；新版统一换算再严格审计，不会因格式差异误判失效。
2. **tc 双栈 `pref` 冲突**：Linux 6.1 的 flower IPv4/IPv6 在相同优先级下可能无法共存；新版为端口分配并持久保存两个独立优先级，防止覆盖其他客户或第三方过滤器，也能从部分安装状态继续 `repair`。

在独立 TCP 端口 `45123` 真实 iperf3 四流并发测试，限速前上传/下载为 **45.5 / 299 Mbps**；配置 **10 / 20 Mbps** 后合计接收端实测 **9.73 / 19.1 Mbps**。`tc` 内核计数也确认超限丢包，测试期间原 VLESS `443`、`40002` 保持活跃。

未测试完的项目：公网 IPv6 真实吞吐、服务器重启后的长期恢复、大规模多端口、不同 NAT 拓扑、完全关闭一层后的另一层单独执法。原测试套件与审查差异留在开发验证过程，不需放入最小发布仓库。

## 选项与更新

同一条一键安装命令以后可用于更新；更新前建议备份 VPS 快照。首次安装通常不需要手动指定网卡，如果自动检测不安全/不成功，可以明确指定：

```bash
bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/install.sh') --iface ens3
```

如你**明确希望只使用 nft 主层**，可以加 `--nft-only`，否则默认必须成功安装双层保护：

```bash
bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/install.sh') --nft-only
```

本地使用时，把三个文件放同一个目录，执行 `bash install.sh`。此模式也会核对 `portbw.py` 哈希。查看 systemd：`systemctl status portbw-watch.timer --no-pager`。不要运行 `nft flush ruleset` 或删除网卡 root qdisc。
