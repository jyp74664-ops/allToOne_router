from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text, select
from sqlalchemy.pool import NullPool
from pathlib import Path
from typing import AsyncGenerator
import json
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

    @staticmethod
    def _suppress_aisqlite_noise(record: logging.LogRecord) -> bool:
        """过滤 aiosqlite/NullPool 在请求取消时产生的无害日志噪音。
        
        场景：请求被客户端取消 → asyncio.CancelledError 传播到连接关闭/回滚阶段
              → SQLAlchemy pool 记录 ERROR，实际不影响任何数据。
        """
        msg = str(record.getMessage())
        # 消息文本直接匹配
        if "CancelledError" in msg or "Connection closed" in msg:
            return False
        # exc_info=True 时异常对象在 record.exc_info 里，文本里未必有 "Cancel"
        if record.exc_info and record.exc_info[1] is not None:
            exc_type = type(record.exc_info[1]).__name__
            if "cancel" in exc_type.lower():
                return False
        return True

    def get_engine(self):
        """获取异步引擎（延迟初始化）"""
        if self.engine is None:
            self.engine = create_async_engine(
                f"sqlite+aiosqlite:///{self.db_path}",
                echo=False,
                connect_args={"check_same_thread": False},
                # NullPool 禁用连接池，避免 shutdown 时 AsyncAdaptedQueuePool
                # 因连接被 asyncio.CancelledError 终止而打印 ERROR 日志
                poolclass=NullPool,
            )
            # 静默 aiosqlite 在请求取消时的无害 ERROR 日志
            _pool_logger = logging.getLogger("sqlalchemy.pool")
            _pool_logger.addFilter(self._suppress_aisqlite_noise)
            _db_logger = logging.getLogger("aiosqlite")
            _db_logger.addFilter(self._suppress_aisqlite_noise)
            self.SessionLocal = async_sessionmaker(
                self.engine, class_=AsyncSession, expire_on_commit=False
            )
        return self.engine
    
    async def init_db(self):
        """初始化数据库，创建所有表"""
        engine = self.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 迁移：为 router_groups 表添加 alias 列（如不存在）
            try:
                await conn.execute(text(
                    "ALTER TABLE router_groups ADD COLUMN alias VARCHAR(100)"
                ))
            except Exception:
                pass  # 列已存在则忽略
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

        # 迁移 providers（仅在 providers 表为空时执行首次导入，避免每次启动覆盖用户运行时增删改）
        providers_file = Path(__file__).parent.parent / "providers.json"
        if providers_file.exists():
            async with self.SessionLocal() as session:
                existing_providers = (await session.execute(select(Provider))).scalars().all()
            if not existing_providers:
                providers = json.loads(providers_file.read_text(encoding="utf-8"))
                svc = ProviderService()
                await svc._get_db()
                for p in providers:
                    p_copy = dict(p)
                    p_copy["models"] = _normalize_models(p.get("models", []))
                    p_copy["disabled_models"] = _normalize_models(p.get("disabled_models", []))
                    await svc.create(p_copy)
        
        # 迁移 routers（仅在路由组表为空时执行首次导入，避免每次启动覆盖用户运行时增删改）
        routers_file = Path(__file__).parent.parent / "routers.json"
        if routers_file.exists():
            from services.router_manager import RouterService
            from database.models import RouterGroup
            async with self.SessionLocal() as session:
                existing_routers = (await session.execute(select(RouterGroup))).scalars().all()
            if not existing_routers:
                routers = json.loads(routers_file.read_text(encoding="utf-8"))
                svc = RouterService()
                await svc.save_all(routers)

    async def _backfill_router_aliases(self):
        """启动时不需要回填别名，别名由用户在管理界面设置或 API 创建时传入"""
        pass
    
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
