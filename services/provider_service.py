"""服务层：提供商管理"""
import logging
from typing import Optional

import httpx
from sqlalchemy import select

from database.models import Provider, ModelConfig
from database.engine import get_db
from services.health_service import check_model, fetch_models as _fetch_models, mask_key

logger = logging.getLogger("flowgate.providers")


class ProviderService:
    """提供商 CRUD 服务"""
    
    def __init__(self):
        from database.engine import db
        self.db = db
    
    async def _get_db(self):
        from database.engine import db as _db
        self.db = _db
    
    async def list_all(self) -> list[dict]:
        """列出所有提供商（含实时健康状态）"""
        from services.health_service import health_state
        async with self.db.SessionLocal() as session:
            providers = await session.execute(
                select(Provider).order_by(Provider.created_at.desc())
            )
            result = []
            for p in providers.scalars().all():
                # 附加每个模型的实时健康状态，供前端监控页展示
                health = {}
                for m in (p.models or []) + (p.disabled_models or []):
                    key = f"{p.name}||{m}"
                    st = health_state.health_status.get(key, {})
                    if st:
                        health[m] = {
                            "status": st.get("status", "unknown"),
                            "code": st.get("code"),
                            "latency_ms": st.get("latency_ms"),
                            "prompt_tokens": st.get("prompt_tokens", 0),
                            "completion_tokens": st.get("completion_tokens", 0),
                        }
                result.append({
                    "id": p.id,
                    "name": p.name,
                    "base_url": p.base_url,
                    "api_key_masked": mask_key(p.api_key),
                    "models": p.models or [],
                    "disabled_models": p.disabled_models or [],
                    "free_only": p.free_only,
                    "provider_status": p.provider_status,
                    "health": health,
                    "created_at": p.created_at.isoformat(),
                })
            return result
    
    async def get(self, name: str) -> Optional[dict]:
        """按名称获取提供商"""
        async with self.db.SessionLocal() as session:
            result = await session.execute(
                select(Provider).where(Provider.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return None
            return {
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "api_key_masked": mask_key(p.api_key),
                "models": p.models or [],
                "disabled_models": p.disabled_models or [],
                "free_only": p.free_only,
                "provider_status": p.provider_status,
            }
    
    async def create(self, data: dict) -> dict:
        """创建提供商"""
        async with self.db.SessionLocal() as session:
            # 检查名称是否已存在
            existing = await session.execute(
                select(Provider).where(Provider.name == data["name"])
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"提供商 '{data['name']}' 已存在")
            
            # 创建提供商记录
            p = Provider(
                name=data["name"],
                base_url=data["base_url"],
                api_key=data["api_key"],
                models=data.get("models", []),
                disabled_models=data.get("disabled_models", []),
                free_only=data.get("free_only", True),
            )
            session.add(p)
            await session.commit()
            await session.refresh(p)
            
            # 创建模型配置
            for model in p.models:
                session.add(ModelConfig(provider_id=p.id, model_id=model))
            await session.commit()
            
            return {
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "models": p.models,
                "free_only": p.free_only,
            }
    
    async def update(self, name: str, data: dict) -> Optional[dict]:
        """更新提供商"""
        async with self.db.SessionLocal() as session:
            result = await session.execute(
                select(Provider).where(Provider.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return None
            
            for key, value in data.items():
                if value is not None and hasattr(p, key):
                    setattr(p, key, value)
            
            await session.commit()
            return {"ok": True}
    
    async def delete(self, name: str) -> bool:
        """删除提供商"""
        async with self.db.SessionLocal() as session:
            result = await session.execute(
                select(Provider).where(Provider.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return False
            await session.delete(p)
            await session.commit()
            return True
    
    async def toggle_model(self, name: str, model: str, enabled: bool) -> dict:
        """启用/禁用模型"""
        async with self.db.SessionLocal() as session:
            result = await session.execute(
                Provider.__table__.select().where(Provider.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                raise ValueError(f"提供商 '{name}' 不存在")
            
            disabled = p.disabled_models or []
            if enabled:
                if model in disabled:
                    disabled.remove(model)
            else:
                if model not in disabled:
                    disabled.append(model)
            p.disabled_models = disabled
            await session.commit()
            return {"disabled_models": disabled}
    
    async def fetch_available_models(
        self,
        name: str,
        client: httpx.AsyncClient,
        aliases: dict,
        context_limits: dict,
    ) -> list[str]:
        """从上游拉取可用模型列表"""
        async with self.db.SessionLocal() as session:
            result = await session.execute(
                Provider.__table__.select().where(Provider.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                raise ValueError(f"提供商 '{name}' 不存在")
            return await _fetch_models(client, p.base_url, p.api_key, p.free_only, aliases, context_limits)
    
    async def sync_free_models(
        self,
        client: httpx.AsyncClient,
        aliases: dict,
        context_limits: dict,
        health_status: dict,
    ) -> list[dict]:
        """批量同步所有提供商的免费模型"""
        results = []
        async with self.db.SessionLocal() as session:
            providers = await session.execute(Provider.__table__.select())
            for p in providers.scalars().all():
                models = await _fetch_models(
                    client, p.base_url, p.api_key, True, aliases, context_limits
                )
                # 过滤已删除的模型
                deleted_keys = {k for k, v in health_status.items() if v and v.get("status") == "deleted"}
                deleted_models = {k.split("||", 1)[1] for k in deleted_keys if k.startswith(p.name + "||")}
                models = [m for m in models if m not in deleted_models]
                
                p.models = models
                p.disabled_models = []
                await session.commit()
                results.append({
                    "name": p.name,
                    "ok": True,
                    "count": len(models),
                    "removed_deleted": len(deleted_models & set(models)),
                })
        return results
