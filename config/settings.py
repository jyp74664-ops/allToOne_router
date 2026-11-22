import json
import logging
import secrets
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("flowgate.config")


class AppConfig(BaseModel):
    """应用基础配置"""
    local_api_key: str = ""
    admin_token: str = ""
    auto_validate: bool = False
    auto_validate_interval: int = Field(default=1800, ge=30)
    merge_reasoning: bool = True
    verify_ssl: bool = True
    announcement_url: Optional[str] = None


class Settings:
    """全局配置管理器，封装配置读写操作"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.config_file = data_dir / "config.json"
        self._config: Optional[AppConfig] = None
        self._last_check_time: int = 0
    
    def load(self) -> AppConfig:
        """加载配置，不存在则生成默认配置"""
        if self._config is not None:
            return self._config
        
        data = {}
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("config root must be an object")
            except Exception as e:
                logger.error("配置文件解析失败，回退到安全默认配置: %s", e)
                data = {}
        else:
            data = {}

        changed = False
        if not data.get("local_api_key"):
            data["local_api_key"] = "sk-local-" + secrets.token_hex(16)
            changed = True
        if not data.get("admin_token"):
            data["admin_token"] = "adm-" + secrets.token_hex(16)
            changed = True
        for key, default in (
            ("auto_validate", False),
            ("auto_validate_interval", 1800),
            ("merge_reasoning", True),
        ):
            if key not in data:
                data[key] = default
                changed = True
        # 纠正非法区间（负数会形成忙循环）
        try:
            if int(data.get("auto_validate_interval", 1800)) < 30:
                data["auto_validate_interval"] = 1800
                changed = True
        except (TypeError, ValueError):
            data["auto_validate_interval"] = 1800
            changed = True

        try:
            self._config = AppConfig(**data)
        except Exception as e:
            logger.error("配置字段校验失败，回退到安全默认配置: %s", e)
            self._config = AppConfig(
                local_api_key="sk-local-" + secrets.token_hex(16),
                admin_token="adm-" + secrets.token_hex(16),
            )
            changed = True

        if changed or not self.config_file.exists():
            self._atomic_write(
                self.config_file,
                json.dumps(self._config.model_dump(), indent=2, ensure_ascii=False),
            )
        return self._config

    def reload(self) -> AppConfig:
        """重新从磁盘加载配置，用于运行时热更新"""
        self._config = None
        return self.load()
    
    def save(self, config: AppConfig) -> None:
        """持久化配置"""
        self._config = config
        self._atomic_write(self.config_file, config.model_dump_json(indent=2))
    
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """原子写入文件"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)


settings = Settings(Path(__file__).parent.parent)
