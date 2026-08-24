"""服务层：模型元数据"""
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger("flowgate.meta")


class ModelMetaService:
    """模型元数据服务"""
    
    def __init__(self):
        self._meta = {
            "aliases": {},
            "context_limits": {},
            "non_chat_keywords": [],
            "model_descriptions": {},
            "rate_limits": {},
        }
        self._load_meta()
    
    def _load_meta(self) -> None:
        """从文件加载元数据"""
        from pathlib import Path
        import json
        
        meta_file = Path(__file__).parent.parent / "models_meta.json"
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                self._meta.update(data)
            except Exception:
                logger.warning("failed to load models_meta.json")
    
    def _save_meta(self) -> None:
        """持久化元数据"""
        from pathlib import Path
        import json
        
        meta_file = Path(__file__).parent.parent / "models_meta.json"
        meta_file.write_text(json.dumps(self._meta, indent=2, ensure_ascii=False), encoding="utf-8")
    
    @property
    def aliases(self) -> dict:
        return self._meta.get("aliases", {})
    
    @property
    def context_limits(self) -> dict:
        return self._meta.get("context_limits", {})
    
    @property
    def non_chat_keywords(self) -> list:
        return self._meta.get("non_chat_keywords", [])
    
    @property
    def model_descriptions(self) -> dict:
        return self._meta.get("model_descriptions", {})
    
    @property
    def rate_limits(self) -> dict:
        return self._meta.get("rate_limits", {})
    
    def get_context_limit(self, model: str) -> Optional[int]:
        """获取模型上下文长度"""
        return self.context_limits.get(model)
    
    def set_context_limit(self, model: str, limit: int) -> None:
        """设置模型上下文长度"""
        self.context_limits[model] = limit
        self._save_meta()
    
    def delete_context_limit(self, model: str) -> None:
        """删除模型上下文长度配置"""
        self.context_limits.pop(model, None)
        self._save_meta()
    
    def get_description(self, model: str) -> Optional[str]:
        """获取模型描述"""
        return self.model_descriptions.get(model, {}).get("desc")
    
    def update_description(self, model: str, desc: str) -> None:
        """更新模型描述"""
        if model not in self.model_descriptions:
            self.model_descriptions[model] = {}
        self.model_descriptions[model]["desc"] = desc
        self._save_meta()
    
    def merge_from_upstream(self, details: dict) -> None:
        """从上探测结果合并元数据"""
        for model_id, info in details.items():
            if model_id not in self.model_descriptions:
                self.model_descriptions[model_id] = {}
            if info.get("context_length"):
                self.model_descriptions[model_id]["ctx"] = info["context_length"]
        self._save_meta()
