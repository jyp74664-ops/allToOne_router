"""填充缺失模型大小到 models_meta.json"""
import json
import re
import sqlite3
from pathlib import Path

META_FILE = Path("models_meta.json")
DB_FILE = Path("flowgate.db")

def extract_size_from_name(model_name: str) -> str | None:
    """从模型名中提取大小，如 nemotron-3.5-super-vl-120b-a12b -> 120B"""
    # 匹配数字+b/B模式
    match = re.search(r'(\d+(?:\.\d+)?)\s*b\b', model_name, re.IGNORECASE)
    if match:
        return f"{match.group(1)}B"
    return None

def main():
    # 加载 models_meta.json
    with open(META_FILE, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    
    descs = meta.setdefault("model_descriptions", {})
    aliases = meta.setdefault("aliases", {})
    
    # 收集数据库中所有模型
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT name, models, disabled_models FROM providers')
    rows = cursor.fetchall()
    conn.close()
    
    all_page_models = set()
    for name, models_json, disabled_json in rows:
        models = json.loads(models_json or '[]')
        disabled = json.loads(disabled_json or '[]')
        for m in models + disabled:
            all_page_models.add(m)
    
    print(f"页面上共有 {len(all_page_models)} 个不同模型")
    
    # 找出不在 models_meta.json 中的模型
    missing = []
    for m in sorted(all_page_models):
        if m not in descs and m not in aliases:
            missing.append(m)
    
    print(f"不在 models_meta.json 中的模型: {len(missing)}")
    print()
    
    # 尝试从模型名提取大小
    added = 0
    cannot_extract = []
    
    for m in missing:
        size = extract_size_from_name(m)
        if size:
            descs[m] = {
                "ctx": None,
                "desc": f"从模型名自动提取大小：{size}",
                "size": size
            }
            added += 1
            print(f"  ✓ {m} -> {size}")
        else:
            cannot_extract.append(m)
            print(f"  ? {m} -> 无法从名提取")
    
    print()
    print(f"从模型名提取成功: {added} 个")
    print(f"无法从名提取: {len(cannot_extract)} 个")
    
    if cannot_extract:
        print()
        print("=== 无法从名提取大小的模型 ===")
        for m in cannot_extract:
            print(f"  {m}")
    
    # 保存
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    print()
    print(f"已保存到 {META_FILE}")

if __name__ == "__main__":
    main()
