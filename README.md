# FlowGate - AI 模型网关

> 高性能 AI 模型聚合网关，支持多上游提供商智能轮询、自动熔断、负载均衡。

## 🚀 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python app.py
```

访问 `http://localhost:8777` 打开管理面板。

## ✨ 功能特性

- **多源聚合**：支持任意 OpenAI 兼容格式的 LLM 提供商
- **智能轮询**：按延迟/质量分自动择优，故障无感切换
- **熔断器**：失败模型自动冷却，避免雪崩
- **实时监控**：SLA 可用率、延迟分布、Token 消耗
- **路由组**：自定义模型分组，一键调度
- **Webhook 通知**：模型故障/恢复时推送通知
- **Prometheus 指标**：`/metrics` 端点暴露监控数据
- **SQLite 存储**：高效持久化，支持 SQL 查询

## 📦 项目结构

```
flowgate/
├── app.py                 # 应用入口
├── config/                # 配置管理
├── database/              # 数据库层
├── services/              # 业务逻辑
├── api/                   # API 路由
├── frontend/              # 前端（Vue 3）
├── migrations/            # 数据库迁移
└── requirements.txt
```

## 🔧 配置

首次启动自动生成 `config.json`，包含默认的 `local_api_key`。

```json
{
  "local_api_key": "sk-local-xxxx",
  "auto_validate": false,
  "auto_validate_interval": 1800
}
```

## 📄 许可证

MIT License - 见 [LICENSE](LICENSE) 文件

## 🙏 致谢

本项目基于 [Router AI Model Gateway](https://github.com/thy-chan/model-gateway) 开发。
