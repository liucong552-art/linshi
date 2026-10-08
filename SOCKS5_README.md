# SOCKS5 独立测试版（用于 `linshi`）

本包只增加 **4 个新文件**，不覆盖 `linshi` 原有 `README.md`、`install.sh`、`portbw.py`，也不修改 `zuizhongheji` 正式仓库。基于之前经过代码审查修复的 `socks5_review_fixed_20261008` 版本；**33 项离线单元测试通过，但尚未在 Ubuntu / Debian VPS 做 SOCKS5 实机验收**。

## 文件

- `socks5-install.sh`：独立安装器，支持 GitHub 在线安装和本地安装，源码 SHA256 锁定。
- `socks5.py`：管理 SOCKS5 客户、随机用户名密码、有效期、独立监听端口、IP 来源数量、总流量配额、定时恢复/清理、可选 DDNS。
- `socks5-traffic.py`：可选每日流量记录，安装后命令是 `socks5-traffic`。
- `SOCKS5_README.md`：本说明；不要改动仓库原来的 `README.md`。

安装后服务通过 **3proxy + nftables + systemd** 工作；当前只支持 **SOCKS5 TCP CONNECT**，明确阻止 SOCKS UDP ASSOCIATE/BIND（不是 UDP 代理）。**不自带带宽限速**；与 `portbw` 完全独立，默认不会控制 `portbw` 配置。每个 SOCKS5 用户有自己的账号、密码和 TCP 端口。

## Ubuntu 26.04 / Debian 12 安装与首次测试

确保 4 个文件全部上传到 `https://github.com/liucong552-art/linshi` 的 `main` 分支根目录后，在 VPS 上（root）：

```bash
apt-get update && apt-get install -y curl ca-certificates
AUTO_DEPS=1 bash <(curl -fsSL 'https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/socks5-install.sh') install --wan-if eth0 --host 162.211.231.219
```

`AUTO_DEPS=1` 缺少依赖时才安装 `3proxy` / `nftables` / `iproute2` 等。3proxy 优先通过现有 Ubuntu apt 源安装；如果找不到会添加 3proxy 官网 `lts` 签名 apt 源、**核对预期签名指纹** `FC12214499FCC7BA1CFF6CDC0312384E3A73940B`。它不会自动清空 nftables 或删除其他项目的表。`--wan-if` 请选择真实对外网络接口，不能指向 `wg`、`tun` 等隧道网卡；`--host` 请按 VPS 的公网 IPv4 更换。

如果你是 **把文件解压到 VPS 同一目录**，也可运行：

```bash
AUTO_DEPS=1 bash socks5-install.sh install --wan-if eth0 --host 162.211.231.219
```

查看状态并创建测试客户（默认端口范围 40000～50050）：

```bash
socks5 status
socks5 add --id test001 --seconds 3600 --port 41003 --ip-limit 1 --sticky 120
socks5 show test001 --credentials
ss -lntp | grep ':41003'
```

如果 41003 被其他服务占用，换一个空闲端口；用户密码是自动随机生成的。保存凭据，不要公开发送。

在 **Windows PowerShell（不要 SSH）** 里，替换用户名密码测试 TCP 代理出口：

```powershell
curl.exe -4 -v --connect-timeout 10 --max-time 25 --socks5-hostname 162.211.231.219:41003 --proxy-user '这里填用户名:这里填密码' https://api.ipify.org
```

成功应该返回 VPS 的公网出口 IP；再用错误密码测试应当拒绝连接。确认 VPS 防火墙 / 服务商安全组已允许需要的 **TCP 41003**（不要额外开放 UDP），并仅在可信网络测试，勿随意公开 SOCKS5 账号。SOCKS5 用户名和密码**不加密传输**，建议使用受保护的接入环境。

## 管理命令

```bash
socks5 list
socks5 status
socks5 show test001 --credentials
socks5 ip-set test001 2 120
socks5 ip-show test001
socks5 pq-set test001 10 --confirm-reset
socks5 pq-del test001
socks5 gc
socks5 del test001
```

如果要设置总流量上限，可以在创建时加 `--quota-gib 10`，这是累计流量额度而不是 Mbps 速率。`--seconds` 是账号寿命（秒），`--ip-limit` 是同时来源公网 IP 的数量，0 表示不限。长效账号（有效期超过 30 天）且启用总额度时，按原有规则尝试每 30 天重新开始一个额度周期。

可选每日流量统计：

```bash
socks5-traffic --install
socks5-traffic
socks5-traffic test001
```

确认管理服务、timer：

```bash
systemctl is-enabled socks5-restore.service socks5-watch.timer
systemctl is-active socks5-watch.timer
socks5 status
```

## 升级 / 注意事项

- 同一 `linshi` 仓库原来的 `install.sh` 是 **portbw 安装器**，不要拿它安装 SOCKS5；SOCKS5 的专用入口叫 `socks5-install.sh`。
- 不建议此测试版直接用于大量不可信公网客户：DNS 重新解析/本机公网 IPv4 访问等绕过场景还须真机验证；配额计量包括 TCP 握手而不只用户应用数据。
- 如果 VPS **已有旧版 SOCKS5 客户**，特别是之前 3proxy 配置含 `bandlimin/out` 时，**禁止直接覆盖升级**，应先备份并逐个迁移。旧版本的迁移机制保留在 `socks5.py migrate-bandlim` 中。
- SOCKS5 删除用户**不自动删除**对应端口的 `portbw` 规则；如你手动为其添加端口限速，需单独 `portbw del <端口>`。
- SOCKS5 安装过程会创建独立 `s5m_ip`、`s5m_quota` 等 nft 表和自己命名的 systemd 单元，不清空整个 nft ruleset，也不修改网卡 root qdisc。
- 首次请先测认证、TCP 连接和独立账号隔离；之后再测 IP 限制、配额、到期和重启恢复。**未经过真机验证之前不宣称通过。**
