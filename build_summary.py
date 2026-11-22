"""汇总 FlowGate 网关模型：速度实测 + 上下文 + 参数量 + 其他维度，按参数量排序。"""
import json, re, sqlite3

meta = json.load(open("models_meta.json", encoding="utf-8"))
dsc = meta["model_descriptions"]
ctxmap = meta["context_limits"]
orctx = json.load(open("or_models_ctx.json", encoding="utf-8"))
raw = json.load(open("provider_models_raw.json", encoding="utf-8"))
speed = json.load(open("speed_merged.json", encoding="utf-8"))
models = json.load(open("speed_models.json", encoding="utf-8"))

conn = sqlite3.connect("flowgate.db")
providers = [r[0] for r in conn.execute("SELECT name FROM providers")]
# 近期健康统计（3天）
health = {}
for r in conn.execute('''SELECT model, SUM(status="ok") ok, SUM(status="rate_limited") rl,
                         SUM(status="fail") fail FROM health_records
                         WHERE checked_at>=datetime("now","-3 days") GROUP BY model'''):
    health[r[0]] = (r[1], r[2], r[3])

def split(mid):
    for p in sorted(providers, key=len, reverse=True):
        if mid.startswith(p + "-"):
            return p, mid[len(p)+1:]
    return "", mid

# --- 上下文索引 ---
or_by_base = {k.replace(':free', '').lower(): v["ctx"] for k, v in orctx.items() if v.get("ctx")}
prov_ctx = {}
for pn, arr in raw.items():
    for m in arr:
        c = m.get("context_length") or m.get("inputTokenLimit")
        if c:
            prov_ctx[m["id"].replace(':free', '').lower()] = c
# Kilo 的额外条目已在其 arr 中

# --- 参数量人工/联网补全（总B, 激活B）---
SIZE_OVERRIDE = {
    "z-ai/glm-5.3-flash": (320, 18),
    "glm-5.3": (320, 18),
    "glm-5.3-flash": (320, 18),
    "stepfun-ai/step-5-preview": (600, 27),
    "step-5-preview": (600, 27),
    "nvidia/stepfun-ai/step-5-preview": (600, 27),
    "stepfun/step-5-preview": (600, 27),
    "nvidia/nemotron-3.5-lightning-30b-a3b": (30, 3),
    "nemotron-3.5-lightning": (30, 3),
    "nvidia/nemotron-3.5-super-vl-120b-a12b": (120, 12),
    "nemotron-3.5-super-vl": (120, 12),
    "nvidia/nemotron-3.5-bolt-e4b": (4.5, 0.5),
    "nvidia/nemotron-3-ultra-550b-a55b": (550, 55),
    "nemotron-3-ultra-550b-a55b": (550, 55),
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": (30, 3),
    "nemotron-3-nano-omni-30b-a3b-reasoning": (30, 3),
    "nvidia/llama-3.1-nemotron-safety-guard-8b-v3": (8, 8),
    "nvidia/riva-translate-4b-instruct-v2": (4, 4),
    "reflection/beam-1.0-501b-a23b": (501, 23),
    "reflection/beam-free": (501, 23),
    "google/gemma-4-26b-a4b-it": (26, 4),
    "google/gemma-4-31b-it": (31, 31),
    "gemma4:31b": (31, 31),
    "gpt-oss:20b": (20, 3.6),
    "codestral-latest": (22, 22),
    "mistral-Nemo-Instruct-2407": (12, 12),
    "recursive/flint-v1.0": (None, None),  # 未知
    "stealth/glyph-cluster": (None, None),
    "sensenova-6.8-flash-lite": (6.8, None),
    "sensenova-6.8-flash": (6.8, None),
    "agnes-2.5-flash": (None, None),
    "agnes-3.0-flash": (None, None),
}

def parse_size(v):
    """把 '120B' / '30B-A3B' 等解析成 (total, active)"""
    if not v:
        return (None, None)
    s = str(v).replace(" ", "")
    m = re.search(r'(\d+(?:\.\d+)?)B(?:-A(\d+(?:\.\d+)?)B)?', s, re.I)
    if m:
        return (float(m.group(1)), float(m.group(2)) if m.group(2) else None)
    return (None, None)

def get_size(orig):
    keys = [orig, orig.split(':')[0], orig.lower()]
    for k in keys:
        if k in SIZE_OVERRIDE:
            return SIZE_OVERRIDE[k]
    # 从模型名提取
    for k in keys[:2]:
        t, a = parse_size(k)
        if t:
            return (t, a)
    # 从 dsc
    for k in keys:
        if k in dsc and dsc[k].get("size"):
            r = parse_size(dsc[k]["size"])
            if r[0]:
                return r
    return (None, None)

CTX_OVERRIDE = {
    "stepfun-ai/step-5-preview": 1000000,   # build.nvidia.com 模型页
    "step-5-preview": 1000000,
    "nvidia/nemotron-3.5-bolt-e4b": 262144, # NVIDIA NIM 同系列
    "nvidia/nemotron-3.5-lightning-30b-a3b": 131072,  # 30B-A3B 默认 128K
    "nvidia/nemotron-3.5-super-vl-120b-a12b": 262144, # 同 lightning 系列
    "nvidia/llama-3.1-nemotron-safety-guard-8b-v3": 131072,
    "nvidia/riva-translate-4b-instruct-v2": 8192,      # 翻译模型
    "agnes-3.0-flash": 262144,
    "agnes-2.5-flash": 262144,
}

def get_ctx(orig):
    keys = [orig, orig.split(':')[0], orig.lower()]
    for k in keys:
        if k in CTX_OVERRIDE:
            return CTX_OVERRIDE[k], "官方文档"
    for k in keys:
        if k in ctxmap:
            return ctxmap[k], "本地元数据"
        if k in dsc and dsc[k].get("ctx"):
            return dsc[k]["ctx"], "本地元数据"
        if k.lower() in or_by_base:
            return or_by_base[k.lower()], "OpenRouter"
        if k.lower() in prov_ctx:
            return prov_ctx[k.lower()], "上游API"
    return None, "未查到"

# 速度映射
spd = {r["model"]: r for r in speed}

rows = []
for mid in models:
    p, orig = split(mid)
    total, active = get_size(orig)
    ctx, ctx_src = get_ctx(orig)
    sr = spd.get(mid, {})
    ok = sr.get("ok", False)
    # 速度：优先用补测总吞吐（reasoning模型），否则用主测
    if ok and "retry_total_tps" in sr:
        tps = sr["retry_total_tps"]
        text_tps = sr["retry_text_tps"]
        comp = sr["retry_comp"]
        reason = sr["retry_reason"]
        wall = sr["retry_wall"]
        speed_note = "补测"
    elif ok:
        tps = sr.get("tok_per_s", 0)
        text_tps = sr.get("text_tok_per_s", 0)
        comp = sr.get("completion_tokens", 0)
        reason = sr.get("reasoning_tokens", 0)
        wall = sr.get("wall_s", 0)
        speed_note = ""
    else:
        tps = text_tps = comp = reason = wall = 0
        speed_note = sr.get("error", "未测")[:40]
    hk = orig.split(':')[0]
    h = health.get(hk, (0, 0, 0))
    rows.append({
        "id": mid, "provider": p, "orig": orig,
        "total": total, "active": active,
        "ctx": ctx, "ctx_src": ctx_src,
        "ok": ok, "tps": tps, "text_tps": text_tps,
        "comp": comp, "reason": reason, "wall": wall,
        "speed_note": speed_note,
        "h_ok": h[0], "h_rl": h[1], "h_fail": h[2],
    })

# 排序：有参数量的按总参数降序，无的排最后；同参数按速度降序
rows.sort(key=lambda r: (r["total"] is not None, r["total"] or 0, r["tps"]), reverse=True)
json.dump(rows, open("summary_rows.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# 输出 markdown
def fmt(v, suffix=""):
    if v is None: return "?"
    if isinstance(v, float) and v != int(v):
        return f"{v:g}{suffix}"
    return f"{int(v)}{suffix}"

def ctx_str(v):
    if not v: return "?"
    # 先归一到标准档位：128K=131072, 256K=262144, 1M=1048576 等
    standards = [(1048576, "1M"), (524288, "512K"), (262144, "256K"),
                 (131072, "128K"), (65536, "64K"), (32768, "32K")]
    for val, label in standards:
        if abs(v - val) <= 3000:
            return label
    if v >= 1000000: return f"{v/1e6:g}M"
    if v >= 1000: return f"{round(v/1000)}K"
    return str(v)

lines = ["| # | 模型 | 参数量(总/激活) | 最大上下文 | 实测吞吐 | 有效文本速度 | 供应商 | 3天可用/限流/失败 | 状态 |",
         "|---|------|----------------|-----------|---------|-------------|--------|------------------|------|"]
for i, r in enumerate(rows, 1):
    if r["total"] is not None:
        sz = fmt(r["total"]) + "B" + (f"/{fmt(r['active'])}B" if r["active"] else "")
    else:
        sz = "?"
    ctx = ctx_str(r["ctx"])
    tps = f"{r['tps']:.1f}" if r["ok"] and r["tps"] else ("失败" if not r["ok"] else "—")
    ttps = f"{r['text_tps']:.1f}" if r["ok"] and r["text_tps"] else "—"
    if r["ok"] and r["reason"] and r["reason"] >= r["comp"] * 0.8:
        ttps += " (思考占满)"
    status = "✓" if r["ok"] else "✗"
    if r["speed_note"] and not r["ok"]:
        status = "✗ " + r["speed_note"].replace("|", "/")[:22]
    lines.append(f"| {i} | {r['orig'][:46]} | {sz} | {ctx} | {tps} | {ttps} | {r['provider']} | "
                 f"{r['h_ok']}/{r['h_rl']}/{r['h_fail']} | {status} |")
open("summary_table.md", "w", encoding="utf-8").write("\n".join(lines))
print("已生成 summary_table.md，共", len(rows), "行")
print("有参数量:", sum(1 for r in rows if r["total"] is not None))
print("有上下文:", sum(1 for r in rows if r["ctx"]))
print("测速成功:", sum(1 for r in rows if r["ok"]))
