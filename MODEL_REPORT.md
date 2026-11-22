# FlowGate 网关模型测评报告

生成时间：2026-11-22 | 网关地址：http://localhost:8777 | 数据来源：本地实测 + 上游实时 API

## 一、测速方法

- 对每个模型发起一次真实 chat/completions 请求：定长数字序列生成任务（512 token 上限）
- 吞吐 = completion_tokens / 端到端耗时；reasoning token 单独统计
- 首轮有 13 个模型将 token 全部用于思考，已用简单任务二次补测
- 全部 16 个 provider 均为免费额度源，测速不产生费用，但消耗各源免费配额

## 二、速度榜

### 总吞吐 TOP15（含推理 token）

| t/s | 模型 | 参数量 | 供应商 |
|-----|------|--------|--------|
| 348.7 | agnes-3.0-flash | ? | agnes |
| 102.2 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | 30B/3B | nvidia |
| 97.2 | agnes-2.5-flash | ? | agnes |
| 85.8 | deepseek-v4-flash | 200B | SenseNova |
| 83.6 | openrouter/free | ? | Kilo Code 🇺🇸 |
| 73.2 | nvidia/nemotron-3.5-lightning:free | ? | Kilo Code 🇺🇸 |
| 72.4 | nvidia/nemotron-3.5-lightning:free | ? | openrouter |
| 64.4 | nvidia/nemotron-3.5-lightning-30b-a3b | 30B/3B | nvidia |
| 57.2 | gpt-oss:20b | 20B/3.6B | llm7.io |
| 56.8 | gemini-3.5-flash-lite | ? | Gemini |
| 55.1 | nvidia/nemotron-3.5-bolt-e4b | 4.5B/0.5B | nvidia |
| 53.3 | nvidia/nemotron-3-ultra-550b-a55b:free | 550B/55B | Kilo Code 🇺🇸 |
| 52.8 | gemini-flash-lite-latest | ? | Gemini |
| 52.6 | nvidia/nemotron-3-ultra-550b-a55b | 550B/55B | nvidia |
| 49.2 | poolside/laguna-xs-2.1 | 3B | nvidia |

### 有效文本速度 TOP15（排除思考占满的模型）

| t/s | 模型 | 参数量 | 供应商 |
|-----|------|--------|--------|
| 348.7 | agnes-3.0-flash | ? | agnes |
| 85.6 | agnes-2.5-flash | ? | agnes |
| 69.2 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | 30B/3B | nvidia |
| 57.2 | gpt-oss:20b | 20B/3.6B | llm7.io |
| 56.8 | gemini-3.5-flash-lite | ? | Gemini |
| 55.1 | nvidia/nemotron-3.5-bolt-e4b | 4.5B/0.5B | nvidia |
| 52.8 | gemini-flash-lite-latest | ? | Gemini |
| 49.2 | poolside/laguna-xs-2.1 | 3B | nvidia |
| 44.9 | nvidia/nemotron-3-ultra-550b-a55b:free | 550B/55B | Kilo Code 🇺🇸 |
| 44.4 | nvidia/nemotron-3-ultra-550b-a55b | 550B/55B | nvidia |
| 42.3 | codestral-latest | 22B/22B | llm7.io |
| 39.0 | stealth/glyph-cluster | ? | Kilo Code 🇺🇸 |
| 35.8 | reflection/beam-free | 501B/23B | Kilo Code 🇺🇸 |
| 33.8 | nvidia/nemotron-3-ultra-550b-a55b:free | 550B/55B | openrouter |
| 33.1 | google/gemma-4-26b-a4b-it:free | 26B/4B | openrouter |

## 三、完整对比表（按参数量降序）

| # | 模型 | 参数量(总/激活) | 最大上下文 | 实测吞吐 | 有效文本速度 | 供应商 | 3天可用/限流/失败 | 状态 |
|---|------|----------------|-----------|---------|-------------|--------|------------------|------|
| 1 | stepfun-ai/step-5-preview | 600B/27B | 1M | 7.1 | 0.6 (思考占满) | nvidia | 12/0/0 | ✓ |
| 2 | nvidia/nemotron-3-ultra-550b-a55b:free | 550B/55B | 256K | 53.3 | 44.9 | Kilo Code 🇺🇸 | 20/0/0 | ✓ |
| 3 | nvidia/nemotron-3-ultra-550b-a55b | 550B/55B | 256K | 52.6 | 44.4 | nvidia | 20/0/0 | ✓ |
| 4 | nvidia/nemotron-3-ultra-550b-a55b:free | 550B/55B | 256K | 41.9 | 33.8 | openrouter | 20/0/0 | ✓ |
| 5 | reflection/beam-free | 501B/23B | 1M | 45.4 | 35.8 | Kilo Code 🇺🇸 | 21/0/0 | ✓ |
| 6 | z-ai/glm-5.3-flash | 320B/18B | 1M | 7.4 | 6.6 | nvidia | 13/0/0 | ✓ |
| 7 | glm-5.3 | 320B/18B | 1M | 失败 | — | SenseNova | 1/20/21 | ✗ HTTP 503: {"detail":"所 |
| 8 | glm-5.3 | 320B/18B | 1M | 失败 | — | llm7.io | 1/20/21 | ✗ HTTP 503: {"detail":"无 |
| 9 | glm-5.2 | 300B | 1M | 失败 | — | SenseNova | 0/0/42 | ✗ HTTP 503: {"detail":"无 |
| 10 | glm-5.2 | 300B | 1M | 失败 | — | llm7.io | 0/0/42 | ✗ HTTP 503: {"detail":"无 |
| 11 | deepseek-v4-flash:0731 | 284B | 1M | 失败 | — | llm7.io | 4/17/0 | ✗ HTTP 503: {"detail":"无 |
| 12 | deepseek-v4-flash | 200B | 1M | 85.8 | — (思考占满) | SenseNova | 4/17/0 | ✓ |
| 13 | nvidia/nemotron-3.5-super-vl-120b-a12b | 120B/12B | 256K | 11.6 | 0.5 (思考占满) | nvidia | 21/0/0 | ✓ |
| 14 | gemma4:31b | 31B/31B | 128K | 失败 | — | llm7.io | 0/0/0 | ✗ HTTP 503: {"detail":"无 |
| 15 | google/gemma-4-31b-it:free | 31B/31B | 128K | 失败 | — | openrouter | 2/0/0 | ✗ HTTP 503: {"detail":"所 |
| 16 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | 30B/3B | 128K | 102.2 | 69.2 | nvidia | 20/0/0 | ✓ |
| 17 | nvidia/nemotron-3.5-lightning-30b-a3b | 30B/3B | 128K | 64.4 | 0.3 (思考占满) | nvidia | 21/0/0 | ✓ |
| 18 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning: | 30B/3B | 256K | 14.1 | 0.8 (思考占满) | Kilo Code 🇺🇸 | 20/0/0 | ✓ |
| 19 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning: | 30B/3B | 256K | 11.3 | 0.8 (思考占满) | openrouter | 20/0/0 | ✓ |
| 20 | google/gemma-4-26b-a4b-it:free | 26B/4B | 128K | 33.1 | 33.1 | openrouter | 0/0/0 | ✓ |
| 21 | codestral-latest | 22B/22B | 32K | 42.3 | 42.3 | llm7.io | 21/0/0 | ✓ |
| 22 | gpt-oss:20b | 20B/3.6B | 128K | 57.2 | 57.2 | llm7.io | 0/0/0 | ✓ |
| 23 | mistral-Nemo-Instruct-2407 | 12B/12B | 128K | 22.9 | 22.9 | llm7.io | 21/0/0 | ✓ |
| 24 | nvidia/llama-3.1-nemotron-safety-guard-8b-v3 | 8B/8B | 128K | 7.0 | 7.0 | nvidia | 20/0/0 | ✓ |
| 25 | sensenova-6.8-flash-lite | 6.8B | 256K | 失败 | — | SenseNova | 3/18/0 | ✗ HTTP 503: {"detail":"无 |
| 26 | nvidia/nemotron-3.5-bolt-e4b | 4.5B/0.5B | 256K | 55.1 | 55.1 | nvidia | 21/0/0 | ✓ |
| 27 | nvidia/riva-translate-4b-instruct-v2 | 4B/4B | 8K | 9.9 | 9.9 | nvidia | 19/0/0 | ✓ |
| 28 | poolside/laguna-xs-2.1 | 3B | 32K | 49.2 | 49.2 | nvidia | 21/0/0 | ✓ |
| 29 | agnes-3.0-flash | ? | 256K | 348.7 | 348.7 | agnes | 21/0/0 | ✓ |
| 30 | agnes-2.5-flash | ? | 256K | 97.2 | 85.6 | agnes | 21/0/0 | ✓ |
| 31 | openrouter/free | ? | 200K | 83.6 | 0.4 (思考占满) | Kilo Code 🇺🇸 | 41/0/0 | ✓ |
| 32 | nvidia/nemotron-3.5-lightning:free | ? | 1M | 73.2 | 0.4 (思考占满) | Kilo Code 🇺🇸 | 0/0/0 | ✓ |
| 33 | nvidia/nemotron-3.5-lightning:free | ? | 1M | 72.4 | 0.5 (思考占满) | openrouter | 0/0/0 | ✓ |
| 34 | gemini-3.5-flash-lite | ? | ? | 56.8 | 56.8 | Gemini | 15/0/0 | ✓ |
| 35 | gemini-flash-lite-latest | ? | ? | 52.8 | 52.8 | Gemini | 15/0/0 | ✓ |
| 36 | nvidia/nemotron-3.5-super-vl:free | ? | 1M | 43.6 | 1.9 (思考占满) | Kilo Code 🇺🇸 | 0/0/0 | ✓ |
| 37 | stealth/glyph-cluster | ? | 256K | 40.3 | 39.0 | Kilo Code 🇺🇸 | 19/0/2 | ✓ |
| 38 | recursive/flint-v1.0:free | ? | 256K | 38.6 | 11.4 | openrouter | 0/0/0 | ✓ |
| 39 | nvidia/nemotron-3.5-super-vl:free | ? | 1M | 10.9 | 0.5 (思考占满) | openrouter | 0/0/0 | ✓ |
| 40 | kilo-auto/free | ? | 256K | 10.3 | 0.4 (思考占满) | Kilo Code 🇺🇸 | 16/5/0 | ✓ |
| 41 | openrouter/free | ? | 200K | 6.0 | 0.7 (思考占满) | openrouter | 41/0/0 | ✓ |
| 42 | gemini-3-flash-preview | ? | ? | 4.5 | 4.5 | Gemini | 15/0/0 | ✓ |
| 43 | gemini-robotics-er-2-preview | ? | ? | 2.3 | 2.3 | Gemini | 15/0/0 | ✓ |
| 44 | kimi-k3 | ? | 128K | 失败 | — | SenseNova | 2/19/21 | ✗ HTTP 503: {"detail":"所 |
| 45 | gemini-3.5-transcribe | ? | ? | 失败 | — | Gemini | 15/0/0 | ✗ HTTP 503: {"detail":"所 |
| 46 | kimi-k3 | ? | 128K | 失败 | — | llm7.io | 2/19/21 | ✗ HTTP 503: {"detail":"无 |

## 四、数据来源与口径

- 上下文：OpenRouter 实时 API（441 模型）+ 各 provider 上游 /models + SenseNova 官方 + 百度百科/智谱财报（GLM/Step）
- 参数量：项目 models_meta.json + 模型名提取 + 联网核实（GLM-5.3-Flash 320B/18B、Step-5 600B/27B 等）
- 速度：本机实测，受网络/上游负载影响，单次采样有波动
- 状态列：3 天内探活 ok/限流/失败次数，来自网关 health_records
