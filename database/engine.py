from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text, select
from pathlib import Path
from typing import AsyncGenerator
import logging

from config.settings import settings

logger = logging.getLogger("flowgate.database")


class Base(DeclarativeBase):
    """SQLAlchemy 声明基类"""
    pass


class Database:
    """异步数据库管理"""
    
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "flowgate.db"
        self.engine = None
        self.SessionLocal = None
    
    def get_engine(self):
        """获取异步引擎（延迟初始化）"""
        if self.engine is None:
            self.engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.db_path}",
                echo=False,
                connect_args={"check_same_thread": False},
            )
            self.SessionLocal = async_sessionmaker(
                self.engine, class_=AsyncSession, expire_on_commit=False
            )
        return self.engine
    
    async def init_db(self):
        """初始化数据库，创建所有表"""
        engine = self.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 迁移历史 JSONL 数据（如果存在）
        await self._migrate_legacy_data()
    
    async def _migrate_legacy_data(self):
        """从历史 JSON 文件迁移数据到数据库"""
        from services.provider_service import ProviderService
        from database.models import Provider
        import json

        def _normalize_models(models: list) -> list:
            """去除模型名前缀（如 models/ 前缀），使名称与上游 API 一致"""
            return [m.removeprefix("models/") for m in (models or [])]

        # 迁移 providers
        providers_file = Path(__file__).parent.parent / "providers.json"
        if providers_file.exists():
            providers = json.loads(providers_file.read_text(encoding="utf-8"))
            svc = ProviderService()
            await svc._get_db()
            for p in providers:
                async with self.SessionLocal() as session:
                    result = await session.execute(
                        select(Provider).where(Provider.name == p["name"])
                    )
                    existing = result.scalar_one_or_none()
                    if existing:
                        for field in (
                            "base_url",
                            "api_key",
                            "models",
                            "disabled_models",
                            "free_only",
                        ):
                            if field in p:
                                if field == "models":
                                    setattr(existing, field, _normalize_models(p[field]))
                                else:
                                    setattr(existing, field, p[field])
                        await session.commit()
                    else:
                        p_copy = dict(p)
                        p_copy["models"] = _normalize_models(p.get("models", []))
                        p_copy["disabled_models"] = _normalize_models(p.get("disabled_models", []))
                        await svc.create(p_copy)
        
        # 迁移 routers
        routers_file = Path(__file__).parent.parent / "routers.json"
        if routers_file.exists():
            routers = json.loads(routers_file.read_text(encoding="utf-8"))
            from services.router_manager import RouterService
            svc = RouterService()
            await svc.save_all(routers)
    
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话"""
        async with self.SessionLocal() as session:
            yield session


db = Database(Path(__file__).parent.parent)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入的数据库会话"""
    async with db.get_session() as session:
        yield session


async def init_db_lifespan():
    """在应用启动时初始化数据库"""
    await db.init_db()


def get_engine():
    """获取 SQLAlchemy 引擎（用于 SQLAlchemy 工具）"""
    return db.get_engine()
