"""Prometheus 指标路由"""
import time

from fastapi import APIRouter, Response, Depends
from sqlalchemy import select, func

from api.auth import verify_admin

router = APIRouter()


@router.get("/metrics", dependencies=[Depends(verify_admin)])
async def prometheus_metrics():
    """暴露 Prometheus 监控指标"""
    from services.health_service import health_state
    from database.engine import db
    from database.models import Provider, UsageRecord

    metrics_lines = []

    # 提供商数量
    async with db.SessionLocal() as session:
        providers_total = (
            await session.execute(select(func.count()).select_from(Provider))
        ).scalar() or 0

    metrics_lines.append("# HELP flowgate_providers_total Total number of providers")
    metrics_lines.append("# TYPE flowgate_providers_total gauge")
    metrics_lines.append(f"flowgate_providers_total {providers_total}")

    # 模型数量：以配置总数为基准，未检查的归入 unknown（冷启动时不再恒为 0）
    metrics_lines.append("# HELP flowgate_models_total Configured models by health status")
    metrics_lines.append("# TYPE flowgate_models_total gauge")
    from sqlalchemy import select as _select
    from database.models import Provider as _Provider
    async with db.SessionLocal() as session:
        _rows = (await session.execute(_select(_Provider.models))).scalars().all()
    configured = {m for models in _rows for m in (models or [])}
    ok_count = sum(1 for v in health_state.health_status.values() if v.get("status") == "ok")
    fail_count = sum(1 for v in health_state.health_status.values() if v.get("status") in ("fail", "error"))
    checked = ok_count + fail_count
    unknown_count = max(0, len(configured) - checked)
    metrics_lines.append(f'flowgate_models_total{{status="ok"}} {ok_count}')
    metrics_lines.append(f'flowgate_models_total{{status="fail"}} {fail_count}')
    metrics_lines.append(f'flowgate_models_total{{status="unknown"}} {unknown_count}')

    # 用量统计（从数据库汇总）
    async with db.SessionLocal() as session:
        pt, ct = (
            await session.execute(
                select(
                    func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                )
            )
        ).one()

    metrics_lines.append("# HELP flowgate_usage_tokens_total Total tokens used")
    metrics_lines.append("# TYPE flowgate_usage_tokens_total counter")
    metrics_lines.append(f'flowgate_usage_tokens_total{{type="prompt"}} {int(pt or 0)}')
    metrics_lines.append(f'flowgate_usage_tokens_total{{type="completion"}} {int(ct or 0)}')

    # 熔断器状态
    metrics_lines.append("# HELP flowgate_circuit_breaker_open Circuit breaker open status")
    metrics_lines.append("# TYPE flowgate_circuit_breaker_open gauge")
    for key, cb in health_state.circuit_breaker.items():
        is_open = 1 if cb.get("open_until", 0) > time.time() else 0
        if "||" not in key:
            continue
        provider, model = key.split("||", 1)
        # Prometheus 标签值需转义反斜杠、双引号与换行
        provider = provider.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        model = model.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        metrics_lines.append(f'flowgate_circuit_breaker_open{{provider="{provider}",model="{model}"}} {is_open}')

    metrics_lines.append("")  # 末尾换行

    return Response(content="\n".join(metrics_lines), media_type="text/plain")
