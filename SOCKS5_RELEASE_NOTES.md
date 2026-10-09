# SOCKS5 真机测试版发布说明（linshi 测试仓库）

本次文件来源：`socks5-tested.py`，即 Debian 12 VPS 上真机验收后上传的版本。
尚未提交到 GitHub，也没有重新安装到 VPS。

## 本次需要提交的文件

1. `socks5.py`：原样复制用户上传的已测版本。
2. `socks5-install.sh`：以当前 `linshi/main` 安装器为基准，只更新 `expected_core` 的 SHA256。
3. `socks5-traffic.py`：保持 GitHub 现有版本，不做修改。

**socks5.py SHA256：** `58edf5d8a2b06281ea4b41675a41656b0091396eaa6551529cd466ed2f9007fd`
**socks5-install.sh SHA256：** `af000a928b3cc9045aff28d97fcc9e04d9957c0ef4ec10f7340b0cbf9b05eceb`
**安装器现有 traffic 固定 SHA256（不变）：** `3c0a882871903d71e74953f46d3181191c84d38bfd08f71a9333dcfed1291dbd`

已验证：上传版与候选 `socks5.py` 字节一致；安装器在还原旧核心哈希后，Git blob 与 `linshi/main` 原安装器完全一致。

## 已包含的服务器本地修复

- systemd 服务名称和 `add` 路由问题的 GitHub 已有修复。
- Debian 12 nft JSON 中动态超时集合 flags 兼容。
- SOCKS5 自检对 UDP ASSOCIATE、回环地址被主动断开的处理。
- 实际公网 CONNECT 阶段校验正确密码与错误密码；协议字节采用明确的二进制字节值。
- 手动 `save/restore` 不能因管理锁繁忙静默跳过。
- 开机恢复只停止已运行代理，避免取消正在排队的启动任务。

## 已执行检查

- `python3 -m py_compile socks5.py`：通过。
- `bash -n socks5-install.sh`：通过。
- 模拟 3proxy 认证时序的 6 个离线 SOCKS5 握手回归场景：通过。
- 来自之前对话的 Debian 12 真机测试：账号认证、ACL、IP 限制、配额计量与超额阻断、手动保存、重启与到期清理通过。

离线检查**不是**全量的安全审计；新版本尚未重新走完整安装器部署测试。

## GitHub 更新顺序

在测试仓库 `liucong552-art/linshi` 的同一个提交中，替换根目录的 `socks5.py` 与 `socks5-install.sh`。其他文件不要改动，尤其不要修改 `zuizhongheji` 或 `portbw`。

由于安装器的 SHA256 锁定，**必须把这两个文件作为同一套发布内容**，不要只替换一个。提交完成前，不要用新安装器覆盖当前正常运行的 VPS。
