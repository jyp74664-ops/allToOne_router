"""补测 reasoning 模型：延长 token 预算，让思考结束后产生可见文本。"""
import json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:8777/v1/chat/completions"
KEY = json.load(open("config.json", encoding="utf-8"))["local_api_key"]
PROMPT = "1+1等于几？直接回答数字。"
MAX_TOKENS = 1024
TIMEOUT = 120

def test_one(model):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS, "temperature": 0}
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        dt = time.time() - t0
        u = d.get("usage") or {}
        comp = u.get("completion_tokens") or 0
        det = u.get("completion_tokens_details") or {}
        reason = det.get("reasoning_tokens") or 0
        text_tok = det.get("text_tokens") or max(0, comp - reason)
        content = ""
        try: content = d["choices"][0]["message"].get("content") or ""
        except Exception: pass
        return {"model": model, "ok": True, "wall_s": round(dt, 3),
                "completion_tokens": comp, "reasoning_tokens": reason,
                "text_tokens": text_tok, "tok_per_s": round(comp/dt, 2),
                "text_tok_per_s": round(text_tok/dt, 2) if dt else 0,
                "content_preview": content[:60]}
    except urllib.error.HTTPError as e:
        return {"model": model, "ok": False,
                "error": f"HTTP {e.code} {e.read().decode(errors='ignore')[:120]}",
                "wall_s": round(time.time()-t0, 3)}
    except Exception as e:
        return {"model": model, "ok": False, "error": f"{type(e).__name__} {str(e)[:120]}",
                "wall_s": round(time.time()-t0, 3)}

if __name__ == "__main__":
    models = json.load(open("speed_models_retry.json", encoding="utf-8"))
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(test_one, m): m for m in models}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result(); results.append(r)
            print(f"[{i}/{len(models)}] {'OK ' if r['ok'] else 'FAIL'} "
                  f"{r['model'][:48]:48} {r.get('text_tok_per_s',0):7} t/s  "
                  f"text {r.get('text_tokens')}/{r.get('completion_tokens')}  "
                  f"{r.get('wall_s')}s  {r.get('content_preview','')[:25]}", flush=True)
    json.dump(results, open("speed_results_retry.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("完成:", sum(r["ok"] for r in results), "/", len(results))
