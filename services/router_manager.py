"""服务层：路由组管理。

路由组的唯一数据源是数据库 router_groups 表。
早期版本从 routers.json 加载过一份内存副本，但该副本只写不读，
已移除，避免与数据库不一致造成误导。
"""
from typing import Optional
from sqlalchemy import select


class RouterService:
    """路由组 CRUD 服务"""
    
    async def list_all(self) -> dict:
        """返回所有路由组"""
        from database.engine import db
        from database.models import RouterGroup
        
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
            return {"name": name, "models": models, "alias": alias}
    
    async def delete(self, name: str) -> bool:
        """删除路由组"""
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
            return True
    
    async def save_all(self, routers: dict) -> None:
        """批量保存路由组（全量替换）
        
        routers 格式支持两种：
          1. {"name": ["model1", "model2"]}   （旧格式，别名留空）
          2. {"name": {"models": [...], "alias": "xxx"}} （新格式）
        """
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
    
    async def get_aliases(self) -> dict:
        """返回 {alias: name} 映射（仅含非空别名）"""
        from database.engine import db
        from database.models import RouterGroup
        
        async with db.SessionLocal() as session:
            result = await session.execute(select(RouterGroup))
            rows = result.scalars().all()
        return {r.alias: r.name for r in rows if r.alias}
