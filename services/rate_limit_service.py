"""服务层：速率限制同步"""
import re
import logging
import httpx
from typing import Optional

logger = logging.getLogger("flowgate.rate_limit")

RATE_LIMIT_GLOSSARY = {
    "RPM": ("请求/分钟", "请求"),
    "RPD": ("请求/日", "请求"),
    "TPM": ("tokens/分钟", "tokens"),
    "TPD": ("tokens/日", "tokens"),
    "RPS": ("请求/秒", "请求"),
}

RATE_LIMIT_DATA_URL = "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/data.json"


def translate_rate_limit(rate_limit_str: str) -> str:
    """将英文速率限制转为中文描述"""
    parts = [p.strip() for p in rate_limit_str.split(",")]
    translated = []
    for part in parts:
        m = re.match(r'^([~><]?\s*\d[\d,]*)\s*(RPM|RPD|TPM|TPD|RPS)\b', part, re.IGNORECASE)
        if m:
            num = m.group(1).strip()
            unit = m.group(2).upper()
            cn_unit, cn_item = RATE_LIMIT_GLOSSARY.get(unit, (unit, unit))
            translated.append(f"{num} {unit}({num}{cn_item}/{cn_unit.split('/')[1]})")
        else:
            translated.append(part)
    return ", ".join(translated)


def rate_limit_tooltip(rate_limit_str: str) -> str:
    """生成中文 tooltip"""
    if rate_limit_str.strip().lower() == "credit-metered":
        return "按积分/信用额度计费"
    pattern = re.compile(r'([~><]?\s*\d[\d,]*)\s*(RPM|RPD|TPM|TPD|RPS)\b', re.IGNORECASE)
    tips = []
    for m in pattern.finditer(rate_limit_str):
        num = m.group(1).strip()
        unit = m.group(2).upper()
        cn_unit, cn_item = RATE_LIMIT_GLOSSARY.get(unit, (unit, unit))
        time_word = cn_unit.split("/")[1] if "/" in cn_unit else cn_unit
        tips.append(f"每{time_word}{num}次{cn_item}")
    return "，".join(tips) if tips else rate_limit_str


async def sync_rate_limits(client: httpx.AsyncClient, meta_service) -> dict:
    """从 GitHub 下载速率限制数据并匹配"""
    try:
        resp = await client.get(RATE_LIMIT_DATA_URL, timeout=30)
        if resp.status_code != 200:
            logger.warning("rate-limit fetch failed: HTTP %d", resp.status_code)
            return {"ok": False, "detail": f"HTTP {resp.status_code}"}
        data = resp.json()
    except Exception as e:
        logger.warning("rate-limit fetch error: %s", e)
        return {"ok": False, "detail": str(e)}
    
    # 构建 baseUrl → provider 映射
    url_map = {}
    for prov in data.get("providers", []):
        base = prov.get("baseUrl", "").rstrip("/").lower()
        if base:
            url_map[base] = prov
    
    new_limits = {}
    matched = 0
    
    # 获取所有提供商
    from database.engine import db
    from database.models import Provider
    
    async with db.SessionLocal() as session:
        from sqlalchemy import select
        providers_result = await session.execute(select(Provider))
        providers = providers_result.scalars().all()
        
        for p in providers:
            base = p.base_url.rstrip("/").lower()
            candidates = [base]
            if base.endswith("/openai"):
                candidates.append(base[:-7])
            if base.endswith("/v1"):
                candidates.append(base[:-3])
            
            prov_data = None
            for c in candidates:
                if c in url_map:
                    prov_data = url_map[c]
                    break
            if not prov_data:
                for b, pd in url_map.items():
                    if b in base or base in b:
                        prov_data = pd
                        break
            if not prov_data:
                continue
            
            # 提取 provider 级别默认速率
            prov_rate_limits = [m.get("rateLimit", "") for m in prov_data.get("models", []) if m.get("rateLimit")]
            default_rl = max(set(prov_rate_limits), key=prov_rate_limits.count) if prov_rate_limits else ""
            
            # 构建模型名 → {raw, tooltip} 映射
            model_limits = {}
            for m in prov_data.get("models", []):
                mid = m.get("id", "")
                mname = m.get("name", "")
                rl = m.get("rateLimit", "")
                if not rl:
                    continue
                entry = {
                    "raw": "按积分计费" if rl.strip().lower() == "credit-metered" else rl,
                    "tooltip": rate_limit_tooltip(rl),
                }
                if mid:
                    model_limits[mid.lower()] = entry
                if mname:
                    model_limits[mname.lower()] = entry
            
            default_entry = {"raw": default_rl, "tooltip": rate_limit_tooltip(default_rl)} if default_rl else None
            
            # 匹配当前 provider 的模型
            for model in p.models or []:
                key = model.lower()
                found = None
                if key in model_limits:
                    found = model_limits[key]
                elif "/" in key:
                    short = key.split("/", 1)[1]
                    if short in model_limits:
                        found = model_limits[short]
                if not found:
                    for mk, mv in model_limits.items():
                        if mk in key or key in mk:
                            found = mv
                            break
                if not found and default_entry:
                    found = default_entry
                
                if found:
                    new_limits[model] = found
                    matched += 1
    
    # 写入 meta
    meta = meta_service._meta
    meta["rate_limits"] = new_limits
    meta_service._save_meta()
    
    logger.info("rate-limit sync: %d matched out of %d total models", matched, sum(len(p.models or []) for p in providers))
    return {"ok": True, "matched": matched, "total_providers": len(data.get("providers", []))}
