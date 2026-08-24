import json
import secrets
from pathlib import Path
from typing import Optional
from pydantic import BaseModel


class AppConfig(BaseModel):
    """应用基础配置"""
    local_api_key: str = ""
    auto_validate: bool = False
    auto_validate_interval: int = 1800
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
        
        if self.config_file.exists():
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        else:
            data = {
                "local_api_key": "sk-local-" + secrets.token_hex(16),
                "auto_validate": False,
                "auto_validate_interval": 1800,
            }
            self._atomic_write(self.config_file, json.dumps(data, indent=2))
        
        self._config = AppConfig(**data)
        return self._config
    
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
