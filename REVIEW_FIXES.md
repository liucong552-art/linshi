# portbw 用户修改版安全修复记录（2026-10-08）

本修复以用户上传的 `portbw(1).py`、`install(2).sh`、`README(10).md` 和 `test_portbw(1).py` 为基础，不退回早期 portbw 版本；**没有修改原版 VLESS、SOCKS5 或 WireGuard 的任何文件**。

## 已修复

1. `tc_find` 不再以 `pref=port/handle=port` 认定所有权。新过滤器采用 `pref=端口`、`handle=0x0b700000|端口` 的保留命名空间，必须核验准确的 family、TCP、端口、flower keys、chain。不自动认领/修改旧版或外部规则。保留唯一显式迁移工具 `migrate-legacy`，先确认 nft 主保护与四个 legacy 过滤器的完整匹配、burst、超限动作，再执行删除和替换。
2. nft 健康审计新增 named limit `burst`、准确速率/单位/超限方向，drop+counter 表达式、原始 TCP 匹配、规则先后关系校验；模块表出现额外/未知规则、链、对象立即视为异常；对其他端口伪造的 `pbw-*` 注释也校验完整动作，防止跨端口提前放行。不再出现 `accept` 插在前面仍显示 OK 的情形。
3. tc 健康审计同时验证端口、IP 协议、`skip_hw`、无额外 source/destination 筛选、police 速率、burst 和 `action drop`，只读取匹配当前 handle 的 action 片段；无法解析显示 STALE，不把未知结果解释成成功。兼容部分 `tc -j` 输出在 keys 中包含 `eth_type` 的情况。
4. 若 nft 表还在但 `inbound` 或 `outbound` 自有链缺失，`ensure_base()` 只补该链；现有链属性错误、出现外部规则时拒绝触碰。
5. `tc_filters` 查询失败直接报错，不再把执行失败当成无过滤器。
6. 安装/升级在失败时恢复这次写入的配置、unit 文件，并尽力恢复旧 unit 的 enabled/active 状态。Shell 的 trap 在 mktemp 前注册；先备份并完成后才允许回滚程序文件。新增 timeout 依赖检测；Python 检查不生成 `__pycache__`；暂存文件始终在目标目录，rename 不跨文件系统。
7. `list/audit/watch` 复用一次 nft 表快照和 tc 每方向快照，减少大量端口造成的命令进程开销；仍然只修复实际异常端口。
8. 原有业务约定不变：端口限速在节点删除后保留；仅 `portbw del PORT` 移除；不安装或修改 VLESS、SOCKS5、WG；不替换 root qdisc。

## 从旧版 portbw 的迁移方法

**不要直接覆盖运行中的旧版而期待它自动认领旧 tc 过滤器。** 新版的 reserved handle 与旧版不同，遇到旧 `handle=PORT` 会明确报冲突。为保护其他管理器的 tc 过滤器，这一拒绝行为是故意设计的。

1. 维护窗口内先备份 `cp -a /var/lib/portbw /etc/portbw /root/`（备份目的路径可自行选择），查看 `portbw list`、`tc -s filter show dev 网卡 ingress/egress`、`nft -a list table inet pbw_policy`，记录所有端口。
2. 先执行 `systemctl stop portbw-watch.timer` 和 `systemctl stop portbw-watch.service`，确认没有旧工作进程。**不停止 VLESS、SOCKS5 或 WG。**
3. 进入本修复包目录，对每个有 tc 限速的旧端口执行 `python3 -B ./portbw.py migrate-legacy PORT --confirm`。它不会接受无限速方向多余的未知规则，也不会在 nft 主限速失效时删除任何旧 tc。
4. 全部成功后，执行 `bash install.sh update`。已有 iface/tc 模式从 `/etc/portbw/config.json` 自动保留；若内核行为与检测器不匹配，会失败而非冒称成功。
5. 执行 `portbw audit && portbw list`，再测试 IPv4/IPv6、并发聚合限速、重启恢复。

如果迁移失败，**不要**用 `tc qdisc del` 或 `nft flush ruleset` 强行清理。先保存 `tc -j filter show` / `tc -s filter show` 输出、复核端口及规则归属，再决定手动处理。旧版本监控器可能恢复 legacy handle，因此必须保持停用直到完成升级。

**迁移也不是全系统原子事务**：在删除老 tc 到添加新 tc 的瞬间，nft 仍为主保护；如同时遭外部删除，无法保证零窗口。

## 必须真机验证而非声称已验证

- Debian/Ubuntu 当前内核及 iproute2 上，IPv4/IPv6 两条 `flower` 不同协议同一 pref 的接受程度；新的较大 handle 是否被该内核接受。
- nft JSON / tc JSON 和 `tc -s` 实际输出格式与本解析器一致；无法识别时会拒绝宣称正常。
- `clsact + police` 对上传/下载的限制准确度、双栈聚合及 burst；tc 的两族是两个桶，主 nft 才共享桶。
- 本机 DNAT 与上游路由器映射情况下，tc ingress 看到的是公网端口还是映射后的本机端口。
- 与实际 BBR、已有 qdisc 和 WireGuard NAT 共存，冷重启、手动删除自有规则、自有单链丢失后的恢复。
- 如 nft 与 tc 防护同时被清空，保持原版服务文件不修改的约束下，watchdog 不能保证瞬时零窗口阻断。

### 测试范围

新增 `test_security.py` 提供完全模拟的安全回归和故障注入。执行：

```bash
python3 -B -m unittest discover -p 'test_*.py' -v
bash -n install.sh
```

测试仅检查逻辑，不会触碰当前机器的真实内核规则或服务。
