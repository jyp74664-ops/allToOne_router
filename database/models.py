from datetime import datetime
from typing import Optional
from sqlalchemy import String, Float, Integer, DateTime, Text, Boolean, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.engine import Base


class Provider(Base):
    """上游提供商配置"""
    __tablename__ = "providers"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str] = mapped_column(Text)
    models: Mapped[list] = mapped_column(JSON, default=list)
    disabled_models: Mapped[list] = mapped_column(JSON, default=list)
    retired_models: Mapped[list] = mapped_column(JSON, default=list)  # 检测确认 EOL/404 后永久移除的模型
    free_models: Mapped[list] = mapped_column(JSON, default=list)  # 同步时上游 pricing 判定为免费的模型，检测时据此跳过收费模型
    check_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # 关闭后不参与健康检测
    free_only: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_status: Mapped[str] = mapped_column(String(20), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关系
    models_config: Mapped[list["ModelConfig"]] = relationship(back_populates="provider", cascade="all, delete-orphan")


class ModelConfig(Base):
    """模型配置（关联到 Provider）"""
    __tablename__ = "model_configs"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    model_id: Mapped[str] = mapped_column(String(200), index=True)

    __table_args__ = (
        Index("ix_model_config_provider", "provider_id"),
    )

    provider: Mapped["Provider"] = relationship(back_populates="models_config")


class RouterGroup(Base):
    """路由组配置"""
    __tablename__ = "router_groups"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    alias: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    models: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class HealthRecord(Base):
    """健康探测记录"""
    __tablename__ = "health_records"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_name: Mapped[str] = mapped_column(String(100), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20))  # ok, fail, error, deleted
    code: Mapped[int] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_health_checked_at", "checked_at"),
        Index("ix_health_provider_model_time", "provider_name", "model", "checked_at"),
    )
    
    @property
    def key(self) -> str:
        return f"{self.provider_name}||{self.model}"


class UsageRecord(Base):
    """用量统计记录"""
    __tablename__ = "usage_records"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    request_id: Mapped[str] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        Index("ix_usage_provider_time", "provider", "timestamp"),
        Index("ix_usage_model_time", "model", "timestamp"),
    )


class ModelMeta(Base):
    """模型元数据"""
    __tablename__ = "model_meta"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    context_length: Mapped[int] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    rate_limit: Mapped[str] = mapped_column(String(200), nullable=True)
    size: Mapped[str] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Announcement(Base):
    """系统公告"""
    __tablename__ = "announcements"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    source_url: Mapped[str] = mapped_column(String(500), nullable=True)


class Webhook(Base):
    """Webhook 配置"""
    __tablename__ = "webhooks"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list] = mapped_column(JSON, default=list)
    secret: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CheckLog(Base):
    """检查日志：两个固定槽位（provider=最近单次检查, all=最近全量检查），各自只保留一份。"""
    __tablename__ = "check_logs"

    slot: Mapped[str] = mapped_column(String(20), primary_key=True)  # provider / all
    scope: Mapped[str] = mapped_column(String(100), default="")  # 单检查的服务商名
    items: Mapped[list] = mapped_column(JSON, default=list)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
