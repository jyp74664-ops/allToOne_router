# FlowGate - AI 模型网关

> 高性能 AI 模型聚合网关，支持多上游提供商智能轮询、自动熔断、负载均衡。

## 快速开始

```bash
# 使用项目自带的虚拟环境（系统 Python 未安装 uvicorn）
.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8777

# 或双击 start-switch.bat（已内置 venv 路径）
```

访问 `http://localhost:8777` 打开管理面板。**本机免登录**；从局域网其他设备访问需输入管理密码（见下方鉴权说明）。

## 鉴权说明（本机信任 + 局域网密码 + 客户端 key）

| 密钥 | 用途 | 位置 | 能否访问管理接口 |
|---|---|---|---|
| `admin_token` | 局域网访问管理面板的登录密码 | `config.json` | ✅（局域网） |
| `local_api_key` | 客户端调用 `/v1/*` | `config.json` | ❌ |

- **本机免登录**：请求来自 `127.0.0.1` / `::1` 时，所有管理接口直接放行（判定只看 TCP 对端地址，不认 `X-Forwarded-For` 等可伪造的请求头）。
- **局域网需登录**：其他 IP 访问时用 `admin_token` 登录，签发 12 小时有效的会话 token（存浏览器 localStorage）。
- 现有客户端（WorkBuddy 等）继续用 `sk-local-...` 调用 `/v1/*`，**不受影响**；但该 key 不能访问管理接口。
- 管理面板首页不下发任何密钥；通过 `/api/client-key` 获取客户端 key 供复制（本机免登录，局域网需已登录）。
- `admin_token` 和 `local_api_key` 首次启动自动生成。忘记管理密码可查看 `config.json`。
- ⚠️ 本机免登录意味着**本机上任何进程**都能调用管理接口；若将来套反向代理，代理机地址会变成 `127.0.0.1` 而触发本机信任，需重新评估。

## 功能特性

- **多源聚合**：支持任意 OpenAI 兼容格式的 LLM 提供商
- **智能轮询**：按延迟/质量分自动择优，故障无感切换
- **熔断器**：失败模型自动冷却，避免雪崩；provider 级限流自动识别 429/503
- **实时监控**：SLA 可用率、延迟分布、Token 消耗
- **路由组**：自定义模型分组，一键调度
- **Webhook 通知**：模型故障/恢复时推送通知
- **Prometheus 指标**：`/metrics`（需管理鉴权）
- **SQLite 存储**：支持健康检查历史与用量统计，自动清理 30 天前记录

## 项目结构

```
flowgate/
├── app.py                 # 应用入口与生命周期
├── api/                   # API 路由（auth/admin/proxy/responses/webhooks/metrics）
├── config/                # 配置管理（settings.py）
├── database/              # 数据库层（engine/models）
├── services/              # 业务逻辑（health/router/usage/meta/rate_limit）
├── templates/index.html   # 管理面板 HTML 骨架
├── static/                # 前端 CSS/JS（从原单文件拆出）
├── backup_db.py           # 数据库备份工具
├── backups/               # 备份目录（gitignore）
└── requirements.txt
```

## 配置

首次启动自动生成 `config.json`，字段说明见 `config.example.json`：

```json
{
  "local_api_key": "sk-local-xxxx",
  "admin_token": "adm-xxxx",
  "auto_validate": false,
  "auto_validate_interval": 1800,
  "merge_reasoning": false,
  "verify_ssl": true
}
```

- `verify_ssl`: 默认 `true` 校验上游 TLS 证书。若某个上游用自签证书导致失败，可临时设为 `false`（有中间人风险）。
- `auto_validate_interval`: 自动验证间隔，最小 30 秒。

## 数据库维护

```bash
# 手动备份（SQLite 在线备份，服务运行时可执行）
.venv\Scripts\python.exe backup_db.py

# 备份到指定目录（自动保留最近 7 份）
.venv\Scripts\python.exe backup_db.py D:\backups\flowgate
```

建议用 Windows 计划任务每天执行一次。

## 许可证

MIT License - 见 [LICENSE](LICENSE) 文件

## 致谢

本项目基于 [Router AI Model Gateway](https://github.com/thy-chan/model-gateway) 开发。
