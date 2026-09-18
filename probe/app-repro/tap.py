#!/usr/bin/env python3
"""透明抓包代理：claude → tap(8780) → 网关(8779)，把请求/响应原始字节落盘（不改 app 代码）。

用途：定位「claude 报 API returned an empty or malformed response (HTTP 200)」时，
网关到底返回了什么。

留痕：
  raw/tap.jsonl    每行一条：{"t","dir":"req"/"resp"/"chunk_end","path","status","headers","body"/"chunk"}

用法：.venv/bin/python probe/app-repro/tap.py    （监听 127.0.0.1:8780）
然后把被测 agent 的 base_url 指向 http://127.0.0.1:8780
"""
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
RAW.mkdir(parents=True, exist_ok=True)
LOG = RAW / "tap.jsonl"
UPSTREAM = os.environ.get("TAP_UPSTREAM", "http://127.0.0.1:8779")
PORT = int(os.environ.get("TAP_PORT", "8780"))

app = FastAPI()


def log(obj: dict):
    obj["t"] = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def tap(request: Request, path: str):
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "accept-encoding")}
    headers["accept-encoding"] = "identity"
    url = f"{UPSTREAM}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    log({"dir": "req", "method": request.method, "path": "/" + path, "query": request.url.query,
         "headers": headers, "body": body.decode("utf-8", "replace")})

    if request.method != "POST":
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(request.method, url, headers=headers)
            log({"dir": "resp", "path": "/" + path, "status": r.status_code, "body": r.text[:4000]})
            return StreamingResponse(iter([r.content]), status_code=r.status_code,
                                     media_type=r.headers.get("content-type"))

    async def gen():
        async with httpx.AsyncClient(timeout=900) as c:
            async with c.stream("POST", url, content=body, headers=headers) as r:
                log({"dir": "resp_head", "path": "/" + path, "query": request.url.query,
                     "status": r.status_code, "headers": dict(r.headers)})
                n = 0
                try:
                    async for chunk in r.aiter_raw():
                        n += len(chunk)
                        log({"dir": "chunk", "path": "/" + path, "size": len(chunk),
                             "chunk": chunk.decode("utf-8", "replace")})
                        yield chunk
                finally:
                    log({"dir": "chunk_end", "path": "/" + path, "total_bytes": n})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn
    print(f"tap: 127.0.0.1:{PORT} -> {UPSTREAM}\nlog: {LOG}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
