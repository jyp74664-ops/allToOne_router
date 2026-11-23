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

            if alias:
                alias_used = await session.execute(
                    select(RouterGroup).where(RouterGroup.alias == alias)
                )
                if alias_used.scalars().first():
                    raise ValueError(f"别名 '{alias}' 已被其它路由组使用")
            
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

        # 别名必须全局唯一，否则 /v1/chat/completions 的 model 解析会出现歧义
        # （历史实现用 OR 一次查组名或别名，重复别名直接 MultipleResultsFound → 500）。
        # 这里保存前就拒绝，让用户在界面上改掉，而不是留下一条会炸的数据。
        seen_alias: dict[str, str] = {}
        for name, info in parsed.items():
            alias = (info.get("alias") or "").strip()
            if not alias:
                info["alias"] = None
                continue
            if alias != name and alias in parsed:
                raise ValueError(f"别名「{alias}」（来自「{name}」）与另一个路由组的名称重复")
            if alias in seen_alias:
                raise ValueError(
                    f"别名「{alias}」被「{seen_alias[alias]}」和「{name}」重复使用，请改成不同别名"
                )
            seen_alias[alias] = name
            info["alias"] = alias
        
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
