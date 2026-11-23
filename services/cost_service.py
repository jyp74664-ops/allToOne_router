"""服务层：成本账本。

把 usage_records 里的 token 用量按上游标价折算成美元，回答两个问题：
「这个月这些请求如果照价付费要花多少」「用免费渠道省了多少」。

价格全部统一成 USD / 1M token：上游 /models 有的按每 token 报价
（1.5e-07），有的按每 1M token 报价（0.15），落库前用 normalize_price 归一。
没有报价的模型不写 0——未知和免费是两件事，账本里分开统计。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func

from config.settings import settings
from database.engine import db
from database.models import ModelPrice, UsageRecord

logger = logging.getLogger("flowgate.cost")


def normalize_price(value) -> Optional[float]:
    """把上游单价统一成 USD / 1M token。

    - None / 非数字 / 负数（-1 表示动态定价或未知）→ None
    - 0 → 0（明确免费）
    - < 0.001 → 按「每 token 美元」处理，乘 1e6
    - 其余 → 已经是「每 1M token 美元」
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    if v == 0:
        return 0.0
    return v * 1_000_000 if v < 0.001 else v


async def save_prices(provider_name: str, prices: dict) -> int:
    """把一次 /models 拿到的价格写入或更新，返回处理条数。

    prices 结构：{model: {"prompt": 原始单价, "completion": 原始单价}}
    """
    if not prices:
        return 0
    saved = 0
    async with db.SessionLocal() as session:
        existing = await session.execute(
            select(ModelPrice).where(ModelPrice.provider_name == provider_name)
        )
        by_model = {row.model: row for row in existing.scalars().all()}
        for model, raw in prices.items():
            raw = raw or {}
            pin = normalize_price(raw.get("prompt"))
            pout = normalize_price(raw.get("completion"))
            if pin is None and pout is None:
                continue
            row = by_model.get(model)
            if row is None:
                session.add(ModelPrice(
                    provider_name=provider_name,
                    model=model,
                    prompt_per_mtok=pin,
                    completion_per_mtok=pout,
                ))
            else:
                row.prompt_per_mtok = pin
                row.completion_per_mtok = pout
            saved += 1
        await session.commit()
    return saved


async def _price_maps() -> tuple[dict, dict]:
    """返回 (精确价格表, 同名模型最低价表)。

    精确表键为 (provider, model)；最低价表键为 model，用于 usage 记录的
    upstream_model 与当前 provider 对不上时兜底（例如换了渠道）。
    """
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(ModelPrice))).scalars().all()
    exact: dict[tuple[str, str], tuple] = {}
    lowest: dict[str, tuple] = {}
    for r in rows:
        pair = (r.prompt_per_mtok, r.completion_per_mtok)
        exact[(r.provider_name, r.model)] = pair
        cur = lowest.get(r.model)
        score = (pair[0] or 0.0) + (pair[1] or 0.0)
        if cur is None or score < ((cur[0] or 0.0) + (cur[1] or 0.0)):
            lowest[r.model] = pair
    return exact, lowest


async def cost_summary(days: int = 30) -> dict:
    """按天窗口汇总花费与节省。

    spent：按上游标价计算的实际等价花费（免费渠道为 0）
    reference：同样 token 量按「参照价」折算的金额
    saved：reference - spent，即免费额度的等价价值
    unpriced_tokens：没有报价、无法折价的 token 数（单列，不混进 spent）
    """
    cfg = settings.load()
    ref_in = float(getattr(cfg, "cost_ref_prompt", 0.15) or 0)
    ref_out = float(getattr(cfg, "cost_ref_completion", 0.60) or 0)
    cutoff = datetime.now() - timedelta(days=max(1, min(days, 365)))
    exact, lowest = await _price_maps()

    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(
                UsageRecord.provider,
                UsageRecord.model,
                UsageRecord.upstream_model,
                func.count(UsageRecord.id),
                func.sum(UsageRecord.prompt_tokens),
                func.sum(UsageRecord.completion_tokens),
            )
            .where(UsageRecord.timestamp >= cutoff)
            .group_by(UsageRecord.provider, UsageRecord.model, UsageRecord.upstream_model)
        )).all()

    total = {
        "requests": 0, "pt": 0, "ct": 0, "tt": 0,
        "spent": 0.0, "reference": 0.0, "saved": 0.0,
        "unpriced_tokens": 0,
    }
    by_model: dict[tuple, dict] = {}

    for provider, model, upstream_model, count, pt, ct in rows:
        pt = int(pt or 0)
        ct = int(ct or 0)
        tt = pt + ct
        price = None
        if upstream_model:
            price = exact.get((provider, upstream_model))
        if price is None:
            price = exact.get((provider, model))
        if price is None:
            price = lowest.get(upstream_model or model)

        ref_cost = pt / 1e6 * ref_in + ct / 1e6 * ref_out
        if price is None:
            spent = 0.0
            unpriced = tt
        else:
            pin, pout = price
            spent = pt / 1e6 * (pin or 0.0) + ct / 1e6 * (pout or 0.0)
            unpriced = 0

        key = (provider, model, upstream_model or "")
        item = by_model.setdefault(key, {
            "provider": provider, "model": model, "upstream_model": upstream_model or "",
            "requests": 0, "pt": 0, "ct": 0, "tt": 0,
            "spent": 0.0, "reference": 0.0, "saved": 0.0, "unpriced_tokens": 0,
            "priced": price is not None,
        })
        item["requests"] += int(count or 0)
        item["pt"] += pt
        item["ct"] += ct
        item["tt"] += tt
        item["spent"] += spent
        item["reference"] += ref_cost
        item["saved"] += ref_cost - spent
        item["unpriced_tokens"] += unpriced

        total["requests"] += int(count or 0)
        total["pt"] += pt
        total["ct"] += ct
        total["tt"] += tt
        total["spent"] += spent
        total["reference"] += ref_cost
        total["unpriced_tokens"] += unpriced

    total["saved"] = total["reference"] - total["spent"]
    total["savings_pct"] = (
        round(total["saved"] / total["reference"] * 100, 1) if total["reference"] > 0 else 0.0
    )
    for v in total.values():
        if isinstance(v, float):
            pass
    total["spent"] = round(total["spent"], 4)
    total["reference"] = round(total["reference"], 4)
    total["saved"] = round(total["saved"], 4)

    detail = sorted(by_model.values(), key=lambda x: (-x["spent"], -x["tt"]))
    for d in detail:
        d["spent"] = round(d["spent"], 4)
        d["reference"] = round(d["reference"], 4)
        d["saved"] = round(d["reference"] - d["spent"], 4)

    return {
        "days": days,
        "reference_prices": {"prompt": ref_in, "completion": ref_out},
        "total": total,
        "by_model": detail,
    }
