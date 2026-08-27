"""服务层：路由组管理"""
import json
from pathlib import Path
from typing import Optional, List, Dict
from sqlalchemy import select


# 路由组配置（默认值，可从 routers.json 加载）
DEFAULT_ROUTERS: Dict[str, List[str]] = {
    "default": [],
}


# 从文件加载路由配置
def load_routers() -> Dict[str, List[str]]:
    """从 routers.json 加载路由组配置"""
    routers_path = Path(__file__).parent.parent / "routers.json"
    if routers_path.exists():
        try:
            with open(routers_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_ROUTERS


# 运行时路由配置（可被动态更新）
ROUTERS = load_routers()


def pick_available_models(
    providers: List[dict],
    health_state,
    current_model: str = None,
) -> str:
    """从可用提供商中选择一个模型（支持轮询/故障转移）"""
    if not providers:
        return ""
    
    # 收集可用提供商
    available = []
    for p in providers:
        provider_name = p.get("name", "")
        api_key = p.get("api_key", "")
        base_url = p.get("base_url", "")
        
        # 检查提供商健康状态
        key = f"{provider_name}:{base_url}"
        if not health_state.is_circuit_open(key) and health_state.get_provider_status(provider_name) == "ok":
            available.append(p)
    
    if not available:
        return ""
    
    # 简单轮询：选择第一个可用提供商的第一个模型
    # 实际应用中可以加入加权、延迟优化等策略
    provider = available[0]
    models = provider.get("models", [])
    if models:
        # 如果指定了当前模型，尝试选择同提供商的模型
        if current_model:
            for m in models:
                if m.get("id") == current_model or m.get("model") == current_model:
                    return m.get("id", "")
        # 否则返回第一个模型
        return models[0].get("id", models[0].get("model", ""))
    
    return ""


class RouterService:
    """路由组 CRUD 服务"""
    
    async def list_all(self) -> dict:
        """返回所有路由组"""
        from database.engine import db
        from database.models import RouterGroup
        import json
        
        async with db.SessionLocal() as session:
            result = await session.execute(select(RouterGroup))
            routers = result.scalars().all()
        return {r.name: {"models": r.models, "alias": r.alias} for r in routers}
    
    async def get(self, name: str) -> Optional[dict]:
        """按名称获取路由组"""
        from database.engine import db
        from database.models import RouterGroup
        
        async with db.SessionLocal() as session:
            result = await session.execute(
                select(RouterGroup).where(RouterGroup.name == name)
            )
            r = result.scalar_one_or_none()
            if not r:
                return None
            return {"name": r.name, "models": r.models}
    
    async def create(self, name: str, models: list, alias: str = None) -> dict:
        """创建路由组"""
        global ROUTERS
        from database.engine import db
        from database.models import RouterGroup
        
        async with db.SessionLocal() as session:
            # 检查名称是否已存在
            existing = await session.execute(
                select(RouterGroup).where(RouterGroup.name == name)
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"路由组 '{name}' 已存在")
            
            r = RouterGroup(name=name, models=models, alias=alias)
            session.add(r)
            await session.commit()
            ROUTERS[name] = {"models": models, "alias": alias}
            return {"name": name, "models": models, "alias": alias}
    
    async def delete(self, name: str) -> bool:
        """删除路由组"""
        global ROUTERS
        from database.engine import db
        from database.models import RouterGroup
        
        async with db.SessionLocal() as session:
            result = await session.execute(
                select(RouterGroup).where(RouterGroup.name == name)
            )
            r = result.scalar_one_or_none()
            if not r:
                return False
            await session.delete(r)
            await session.commit()
            ROUTERS.pop(name, None)
            return True
    
    async def save_all(self, routers: dict) -> None:
        """批量保存路由组（全量替换）
        
        routers 格式支持两种：
          1. {"name": ["model1", "model2"]}   （旧格式，别名留空）
          2. {"name": {"models": [...], "alias": "xxx"}} （新格式）
        """
        global ROUTERS
        from database.engine import db
        from database.models import RouterGroup
        
        parsed = {}
        for name, value in routers.items():
            if isinstance(value, list):
                parsed[name] = {"models": value, "alias": None}
            elif isinstance(value, dict):
                parsed[name] = {
                    "models": value.get("models", []),
                    "alias": value.get("alias"),
                }
        
        async with db.SessionLocal() as session:
            await session.execute(RouterGroup.__table__.delete())
            for name, info in parsed.items():
                session.add(RouterGroup(
                    name=name, models=info["models"], alias=info["alias"]
                ))
            await session.commit()
        ROUTERS = parsed
    
    async def get_aliases(self) -> dict:
        """返回 {alias: name} 映射（仅含非空别名）"""
        from database.engine import db
        from database.models import RouterGroup
        
        async with db.SessionLocal() as session:
            result = await session.execute(select(RouterGroup))
            rows = result.scalars().all()
        return {r.alias: r.name for r in rows if r.alias}
