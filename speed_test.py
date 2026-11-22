"""FlowGate 模型测速：并发请求 /v1/chat/completions，记录吞吐与首字延迟。
只读测试，不改任何项目文件。结果写入 speed_results.json。
"""
import json
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:8777/v1/chat/completions"
KEY = json.load(open("config.json", encoding="utf-8"))["local_api_key"]
# 长输出任务：让吞吐测量脱离首字延迟主导。数字序列确定性高，便于核对完整性。
PROMPT = ("请写一个很长的数字序列：从1开始逐个递增，每个数字用逗号加空格分隔，"
          "一直写到200为止。只输出数字和逗号，不要任何解释、不要换行、不要其他文字。")
MAX_TOKENS = 512
TIMEOUT = 100
RETRIES = 2  # 上游 429/503 限流时重试

def _request_once(model):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "stream": False,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE, data=data,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    dt = time.time() - t0
    d = json.loads(raw)
    u = d.get("usage") or {}
    comp = u.get("completion_tokens") or 0
    detail = u.get("completion_tokens_details") or {}
    reasoning = detail.get("reasoning_tokens") or 0
    text_tok = detail.get("text_tokens")
    if not text_tok:
        text_tok = max(0, comp - reasoning)
    content = ""
    try:
        content = d["choices"][0]["message"].get("content") or ""
    except Exception:
        pass
    finish = ""
    try:
        finish = d["choices"][0].get("finish_reason") or ""
    except Exception:
        pass
    return {
        "model": model,
        "ok": True,
        "wall_s": round(dt, 3),
        "completion_tokens": comp,
        "reasoning_tokens": reasoning,
        "text_tokens": text_tok,
        "tok_per_s": round(comp / dt, 2) if dt > 0 else 0,
        "text_tok_per_s": round(text_tok / dt, 2) if dt > 0 else 0,
        "prompt_tokens": u.get("prompt_tokens") or 0,
        "finish_reason": finish,
        "content_len": len(content),
        "content_preview": content[:60],
        "returned_model": d.get("model"),
    }

def test_one(model, stream=False):
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = _request_once(model)
            r["attempts"] = attempt + 1
            return r
        except urllib.error.HTTPError as e:
            try:
                err = e.read().decode(errors="ignore")[:200]
            except Exception:
                err = ""
            last_err = f"HTTP {e.code}: {err}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:180]}"
        if attempt < RETRIES:
            time.sleep(1.5 * (attempt + 1))
    return {"model": model, "ok": False, "error": last_err,
            "wall_s": 0, "attempts": RETRIES + 1}

if __name__ == "__main__":
    models = json.load(open(sys.argv[1], encoding="utf-8"))
    out = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(test_one, m): m for m in models}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            flag = "OK " if r["ok"] else "FAIL"
            tok = r.get("text_tok_per_s", 0) if r["ok"] else 0
            detail = (f"text {r.get('text_tokens')}/{r.get('completion_tokens')} tok, "
                      f"reason {r.get('reasoning_tokens')}, {r.get('finish_reason')}") if r["ok"] else r.get("error", "")[:60]
            print(f"[{i}/{len(models)}] {flag} {r['model'][:48]:48} "
                  f"{tok:7} t/s  {r.get('wall_s')}s  {detail}", flush=True)
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ok = [r for r in results if r["ok"]]
    print(f"\n完成: {len(ok)}/{len(results)} 成功 -> {out}")
