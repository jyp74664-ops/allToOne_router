# Skill: 补全模型元数据 (Complete Model Metadata)

## 目的
将 `providers.json` 中列出的所有模型补全到 `models_meta.json` 的 `model_descriptions` 字段，确保每个模型都有完整的元数据（上下文长度、描述、参数量）。

## 适用场景
- 模型网关项目维护
- 新增模型提供商后同步元数据
- 定期检查并补全缺失的模型描述

## 操作步骤

### 1. 分析缺失模型
```python
import json

with open('providers.json', 'r', encoding='utf-8') as f:
    providers = json.load(f)
with open('models_meta.json', 'r', encoding='utf-8') as f:
    models_meta = json.load(f)

all_models = set()
for p in providers:
    for m in p.get('models', []):
        all_models.add(m)

existing = set(models_meta.get('model_descriptions', {}).keys())
missing = all_models - existing
print(f"缺失: {len(missing)} 个")
```

### 2. 批量补全模型描述
每个模型需包含三字段：
- `ctx`: 上下文长度（整数，token 数）
- `desc`: 中文简述（功能定位、特色）
- `size`: 参数量（字符串，估算值加 `~` 前缀，如 `"~100B"`）

### 3. 常见模型家族参数参考表

| 模型家族 | 典型参数量 | 典型上下文 | 备注 |
|---------|-----------|-----------|------|
| GPT-4o/4.1 | 200B~500B | 128K~1M | OpenAI 旗舰 |
| GPT-5 系列 | 500B~1T | 1M | 最新旗舰 |
| o1/o3/o4 | 300B~500B | 200K | 推理专用 |
| Llama 3.x | 8B~400B | 131K | Meta 开源 |
| Qwen 2.5/3 | 4B~480B | 131K | 阿里通义 |
| DeepSeek V3/R1 | 671B | 131K~1M | MoE 架构 |
| DeepSeek V4 | 200B~670B | 1M | 新一代 MoE |
| Nemotron 3/4 | 12B~550B | 128K~256K | NVIDIA 优化 |
| Gemma 3/4 | 4B~31B | 131K | Google 轻量 |
| Gemini 2.5/3.x | ~50B~300B | 1M | Google 多模态 |
| Mistral Large | 123B | 32K~128K | 旗舰 |
| Mixtral 8x22B | 176B | 64K | MoE |
| GLM 4/5 | 30B~300B | 131K~1M | 智谱 |
| Kimi K2 | 1T | 131K~256K | 月之暗面 MoE |
| Cohere Command | 7B~111B | 131K~256K | 企业级 |

### 4. 验证完整性
```python
# 重新统计
all_models = set()
for p in providers:
    for m in p.get('models', []):
        all_models.add(m)
existing = set(models_meta.get('model_descriptions', {}).keys())
print(f"总计: {len(all_models)}, 已有: {len(existing)}, 缺失: {len(all_models - existing)}")
```

### 5. 清理临时文件
删除分析/补全过程中生成的 `_*.py` 临时脚本。

## 注意事项
- ⚠️ 别名模型（如 `MiniMax/MiniMax-M3` → `minimaxai/minimax-m3`）只需补全主键
- ⚠️ 同一模型不同提供商的不同命名（如 `deepseek-v4-flash` vs `deepseek-ai/deepseek-v4-flash`）都要补全
- ⚠️ 估算参数量务必加 `~` 前缀，避免误导
- ⚠️ 上下文长度以官方文档为准，无官方数据时按同架构同规模模型推断

## 输出示例
```json
"model_name": {
  "ctx": 131072,
  "desc": "模型功能简述，中文",
  "size": "70B"
}
```

## 相关文件
- `providers.json` - 模型提供商列表（源）
- `models_meta.json` - 模型元数据（目标，含 `model_descriptions`、`context_limits`、`aliases`、`rate_limits`、`model_scores`）