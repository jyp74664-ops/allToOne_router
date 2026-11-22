# FlowGate 项目体检报告

检查时间：2026-11-22 | 方式：四路并行代码审查 + 本人逐条核实 | 本次未修改任何文件

## 结论

项目处于「能跑但远未完善」状态。共发现 5 项安全红线、7 项功能性 bug、11 项稳定性/数据问题、若干清理项。
其中 **2 项需要立刻处理**：真实上游密钥已进入公开 Git 仓库；管理首页无鉴权且明文下发管理员密钥。

---

## P0 — 安全红线

### 1. 管理首页无鉴权 + 密钥明文下发 + CORS 全开 + 绑定 0.0.0.0
- 位置：`app.py:102-108`（`allow_origins=["*"]` + `allow_credentials=True`）、`app.py:129-147`（`root()` / `/templates/index.html` 无鉴权）、`templates/index.html:1210-1211,1500`
- 事实：`render_dashboard()` 把真实 `local_api_key` 替换进 HTML，页面出现 3 次明文；`start-switch.bat` 与 `app.py` 的 `__main__` 均绑定 `0.0.0.0`
- 影响：任何能访问 8777 端口的人打开首页即拿到管理员 Bearer token，可增删改 provider、路由、Webhook；第三方网页也可跨域读取
- 修复：首页加登录鉴权；不再下发长期 key（改短期会话 token）；CORS 收紧白名单；默认只绑 `127.0.0.1`

### 2. Git 历史含真实密钥，其中一个已推送公开远端
- 位置：`de9456f:test_fetch.py` / `test_fetch2.py` 含 `alv2_K4pq...`（Aion Labs 上游 key）——**已在 origin/main，公开仓库**
- `01475eb` / `9e9fcd2` / `476a4fd:_crud_test.py` 含 `sk-local-b2f8...`（当前管理员 key）——**仅在本地，ahead 7 未推送**
- 影响：公开仓库的 key 可被任何人取用；等于管理员 key 已存在本地历史，一旦 push 即泄露
- 修复：立刻轮换 alv2_ 上游 key；本地 `sk-local` 在推送前必须处理；可选 `git filter-repo` 清洗历史

### 3. 全站关闭 TLS 证书校验，且转发真实上游密钥
- 位置：`api/proxy.py:243,260`，`api/admin.py:191,209,223,288,671,698`，`api/webhooks.py:96`，`services/health_service.py:470`，`app.py:37` 均为 `verify=False`
- 影响：中间人可伪造上游证书截获 16 个 provider 的明文 API key 与用户 prompt
- 修复：默认 `verify=True`；仅对确需自签名的 provider 按项配置关闭

### 4. 管理/客户端/Webhook 共用同一把 key，权限未分离
- 位置：`api/admin.py:32-41`、`api/proxy.py:24-28`、`api/webhooks.py:20-25`
- 事实：`config.example.json` 定义了 `admin_token`，但 `config/settings.py:8-14` 的 `AppConfig` 没有该字段，代码也从不读取——设计了一半
- 影响：只应调用模型的客户端拿到完整管理员权限
- 修复：拆分 `admin_token` 与客户端 key，配置模型与样例同步

### 5. 16 个 provider 的密钥明文落盘，无加密无备份
- 位置：`database/models.py:16`（`Provider.api_key`）、`:114`（`Webhook.secret`）；`flowgate.db` 中 15 条真实 key（nvapi-/sk-/hf_/cfut_/JWT 等）
- 影响：一次文件泄露即损失全部上游凭证；误删不可恢复
- 修复：环境变量注入或 envelope 加密；建立定期备份（`VACUUM INTO` / `Connection.backup()`）

---

## P1 — 功能性 bug（已核实）

| # | 问题 | 位置 | 实际后果 |
|---|------|------|----------|
| 1 | 清除单个模型错误标记 → 清空全部 | 前端 `index.html:2743-2749,3076` 用 body 传 provider；后端 `admin.py:92-104` 读 query 参数，恒为 None | 点一个红点，所有 provider 的熔断状态被清空，异常模型立即恢复流量 |
| 2 | 速率限制手动编辑是空操作 | 前端 `index.html:1741-1769` POST body；后端 `admin.py:668-673` 不接收 body | 界面显示"已保存"，实际什么都没改，还白白触发一次外网同步 |
| 3 | 模型管理弹窗错误分支 ReferenceError | `index.html:2190-2191,2272,2276` | 接口一报错，catch 里访问块级变量抛错，弹窗卡死且看不到原因 |
| 4 | 稳定性表行 HTML 结构损坏 | `index.html:1911,1936-1939` 重复 `${descHtml}` 且多一个 `</td>` | 每行 10 个单元格对 9 列表头，从"模型大小"列起整体错位 |
| 5 | 启动脚本用系统 Python，必然失败 | `start-switch.bat:3` | 系统 Python 3.13 无 uvicorn；依赖只装在 `.venv`（3.11，uvicorn 0.54.0） |
| 6 | VS Code F5 打不开后端 | `.vscode/launch.json:6-13` 只有 `editor-browser` 打开静态模板 | `{{ local_api_key }}` 不替换，`/api/*` 在 file:// 下无效 |
| 7 | `fix_proxy.ps1` 是按行号覆盖源码的危险脚本 | `fix_proxy.ps1:9-17` 覆盖 `proxy.py` 430-444 行 | 那段当前正是成功路径（record_success/用量统计）；注意文件是 .ps1 后缀但内容是 Python，PowerShell 直接跑会语法错误，用 python 跑则会破坏源码 |
| 8 | 401 后静默失败 | 前端 `showGate()/submitGate()` 是空函数，20+ 处 catch 吞 `Unauthorized` | key 失效后页面表现为"按钮没反应"，无法排障 |

---

## P1.5 — 测评数据质量（承接上一件事）

1. `speed_merged.json` 有 **5 行模型归属错误**：请求与 `returned_model` 不一致（agnes-3.0-flash→`-angel`、`openrouter/free`→nemotron 等），榜单被污染且同模型重复占位
2. "有效文本速度 0.x"来自 `speed_results_retry.json` 的 `1+1` 任务（仅 3~4 token），与主测 512 token 任务混排；`build_summary.py` 优先用补测值排序，口径失真
3. `summary_table.md` 被手工改过（`<u>stepfun</u>-ai/...`），`build_summary.py` 无法复现；与 `MODEL_REPORT.md` 已分叉
4. 46 行中 18 行参数量为 `?`、5 行上下文为 `?`，仍参与排名
5. 失败信息截成 `✗ HTTP 503: {"detail":"所` 这类半截 JSON，看不出失败原因
6. 测试脚本 docstring 称"记录首字延迟"但 `stream=false` 从不测 TTFT；无输出内容校验（riva-translate 返回翻译而非数字序列仍记有效）

---

## P2 — 稳定性与可维护性

1. `flowgate.db` 30.32MB：`health_records` 207,250 行，其中 **115,788 条超 30 天**；`usage_records` 14,202 行，13,600 条超 30 天。`cleanup_old_records()` 定义了却**全项目无调用点**，`health_records.checked_at` 无索引
2. 22 条孤立用量记录：`usage_records.provider='林若曦'`（214 条中的一部分），providers 表里实际叫 `林若曦-loacl`
3. `PRAGMA foreign_keys=OFF`，`ON DELETE CASCADE` 不生效
4. 无迁移机制：`user_version=0`，靠手写 ALTER + `except: pass`；README 里的 `migrations/` 不存在
5. `get_model_size` 每次请求同步读 143KB `models_meta.json`（`model_details` 字典从未写入，永远走 fallback），阻塞事件循环
6. `check_rate_limit` 是死逻辑：`record_rate_limit()` 从未被调用
7. `create_task` 不保存引用，后台自动验证可能被 GC；`health_state._lock` 定义但从未使用，并发状态更新无同步
8. `/metrics` 无鉴权，暴露内部模型名与健康状态
9. 配置项解析无 try/except，损坏即全站 500；`auto_validate_interval` 负数会形成忙循环
10. `config.example.json`（含不存在的 `admin_token`）与 `README.md`（写了不存在的 `frontend/`、`migrations/`）双双与实际不符
11. 无数据库备份机制

---

## P3 — 清理

- `proxy_b64.txt` 0 字节残留；`node_modules/` 只剩一个 `node-gyp`，无 `package.json`
- `.gitignore` 未忽略 `node_modules/`、`proxy_b64.txt`、13 个测评产物；却忽略了 `.vscode/`
- 前端 `index.html` 单文件 3300 行，含大量死代码（空门禁函数、`TOKEN_KEY`、`detailTimer`、Hermes 映射 long==short 的空操作）
- 无 HTML 转义，模型名/provider 名/错误信息直接进 `innerHTML`（RCE 风险组合 P0-1）
- Webhook secret 明文回显、无 HMAC 签名、URL 无校验（SSRF）

---

## 需要你拍板的问题

见对话中的提问。本报告只做记录，未改动任何文件。
