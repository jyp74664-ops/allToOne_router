"""Prometheus 指标路由"""
from fastapi import APIRouter, Response

router = APIRouter()


@router.get("/metrics")
async def prometheus_metrics():
    """暴露 Prometheus 监控指标"""
    from services.health_service import health_state
    from database.engine import db
    from database.models import Provider, UsageRecord
    from datetime import timedelta
    
    metrics_lines = []
    
    # 提供商数量
    metrics_lines.append("# HELP flowgate_providers_total Total number of providers")
    metrics_lines.append("# TYPE flowgate_providers_total gauge")
    metrics_lines.append(f"flowgate_providers_total {{}} 0")  # TODO: 从数据库读取
    
    # 健康模型数量
    metrics_lines.append("# HELP flowgate_models_total Total number of models")
    metrics_lines.append("# TYPE flowgate_models_total gauge")
    ok_count = sum(1 for v in health_state.health_status.values() if v.get("status") == "ok")
    fail_count = sum(1 for v in health_state.health_status.values() if v.get("status") in ("fail", "error"))
    metrics_lines.append(f"flowgate_models_total {{status=\"ok\"}} {ok_count}")
    metrics_lines.append(f"flowgate_models_total {{status=\"fail\"}} {fail_count}")
    
    # 用量统计
    metrics_lines.append("# HELP flowgate_usage_tokens_total Total tokens used")
    metrics_lines.append("# TYPE flowgate_usage_tokens_total counter")
    metrics_lines.append(f"flowgate_usage_tokens_total {{type=\"prompt\"}} 0")
    metrics_lines.append(f"flowgate_usage_tokens_total {{type=\"completion\"}} 0")
    
    # 熔断器状态
    metrics_lines.append("# HELP flowgate_circuit_breaker_open Circuit breaker open status")
    metrics_lines.append("# TYPE flowgate_circuit_breaker_open gauge")
    for key, cb in health_state.circuit_breaker.items():
        is_open = 1 if cb.get("open_until", 0) > __import__("time").time() else 0
        provider, model = key.split("||")
        metrics_lines.append(f'flowgate_circuit_breaker_open{{provider="{provider}",model="{model}"}} {is_open}')
    
    metrics_lines.append("")  # 末尾换行
    
    return Response(content="\n".join(metrics_lines), media_type="text/plain")
