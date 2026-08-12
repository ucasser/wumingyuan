"""无名园 Web 服务：PDF 导入 + 检索 Agent + LLM 问答。
启动: python server.py
浏览器打开 http://127.0.0.1:8888
"""
import json
import os
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

from app import paddle_ocr, settings
from app.indexer import iter_files, run_ingest
from app.library_pipeline import file_hash
from app.notes import NotesStore
from app.retrieval_agent import RetrievalAgent
from app.llm_client import LLMClient

web = FastAPI(title="无名园")
STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")

_ingest_state = {"running": False, "cancel_flag": False,
                 "progress": [], "file_count": 0, "current": 0,
                 "files_added": 0, "skipped": 0, "failed": 0,
                 "message": "等待开始"}

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _State:
    def __init__(self):
        self.reload()

    def reload(self):
        self.cfg = settings.get_config()
        self.notes = NotesStore(self.cfg["data_root"])
        self.agent = RetrievalAgent(
            self.cfg.get("project_root", PROJECT_ROOT))
        llm_cfg = self.cfg.get("llm", {})
        self.llm = LLMClient(llm_cfg) if llm_cfg.get("api_key") else None


state = _State()


# ── 页面 ──
@web.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(STATIC)


# ── 导入 ──
@web.post("/api/ingest")
def start_ingest():
    if _ingest_state["running"]:
        return {"ok": False, "message": "已有导入任务在运行"}
    _ingest_state.update(running=True, cancel_flag=False, progress=[],
                         file_count=0, current=0, files_added=0,
                         skipped=0, failed=0, message="正在扫描文件…")
    threading.Thread(target=_run_ingest_bg, daemon=True).start()
    return {"ok": True}


@web.get("/api/ingest/status")
def ingest_status():
    return {k: _ingest_state[k] for k in
            ("running", "progress", "message", "current",
             "file_count", "files_added", "skipped", "failed")}


@web.post("/api/ingest/cancel")
def cancel_ingest():
    _ingest_state["cancel_flag"] = True
    return {"ok": True}


def _run_ingest_bg():
    try:
        state.reload()
        cfg = state.cfg
        def report(done, total, msg):
            _ingest_state.update(current=done, file_count=total, message=msg)
        def should_cancel():
            return _ingest_state["cancel_flag"]
        result = run_ingest(cfg, progress=report, should_cancel=should_cancel)
        _ingest_state.update(files_added=result["files_added"],
                             skipped=result["skipped"],
                             failed=result["failed"],
                             file_count=result["total"],
                             current=result["done"],
                             message="完成" if not result.get("cancelled") else "已取消")
    except Exception as e:
        _ingest_state["message"] = f"错误: {e}"
    finally:
        _ingest_state["running"] = False


# ── 检索 Agent ──
class AgentSearchReq(BaseModel):
    q: str
    top_k: int = 10


class AgentChatReq(BaseModel):
    q: str
    history: list[dict] = []


@web.post("/api/agent/search")
def agent_search(req: AgentSearchReq):
    results = state.agent.search(req.q, req.top_k)
    return {"results": results, "query": req.q, "count": len(results)}


@web.post("/api/agent/chat")
def agent_chat(req: AgentChatReq):
    if not state.llm:
        return {"answer": "", "error": "未配置大模型 API，请在设置页面填写"}

    results = state.agent.search(req.q)
    if not results:
        return {"answer": "未找到相关文献材料，请尝试调整关键词或扩充文库。",
                "error": None, "results": []}

    resp = state.llm.chat(req.q, results, req.history)
    resp["results"] = [{"name": r["name"], "rel_path": r["rel_path"],
                        "snippets": r["snippets"]} for r in results[:5]]
    return resp


@web.post("/api/agent/chat/stream")
async def agent_chat_stream(req: AgentChatReq):
    """流式返回，打字机效果。"""
    if not state.llm:
        yield f"data: {json.dumps({'error': '未配置大模型 API'})}\n\n"
        return

    results = state.agent.search(req.q)
    if not results:
        yield f"data: {json.dumps({'answer': '未找到相关文献材料。'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    import requests as req_lib

    context = state.llm._build_context(results)
    messages = [
        {"role": "system", "content": state.llm._system_prompt()},
    ]
    if req.history:
        messages.extend(req.history)
    messages.append({"role": "user",
                     "content": f"【文献库搜索结果】\n\n{context}\n\n---\n【用户问题】\n{req.q}"})

    url = f"{state.llm.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {state.llm.api_key}",
               "Content-Type": "application/json"}
    body = {"model": state.llm.model, "messages": messages,
            "temperature": 0.3, "max_tokens": 4096, "stream": True}

    try:
        resp = req_lib.post(url, headers=headers, json=body,
                            stream=True, timeout=state.llm.timeout)
        for line in resp.iter_lines():
            if line.startswith(b"data: "):
                data = line[6:].decode("utf-8")
                if data == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield f"data: {json.dumps({'content': content})}\n\n"
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"


# ── 模型列表 ──
@web.get("/api/models")
def list_models():
    return {"models": LLMClient.PRESETS}


# ── 设置 ──
@web.get("/api/settings")
def get_settings():
    cfg = settings.get_config()
    safe = dict(cfg)
    if "llm" in safe and "api_key" in safe.get("llm", {}):
        key = safe["llm"]["api_key"]
        safe["llm"]["api_key"] = key[:8] + "***" if len(key) > 8 else "***"
    if "ocr" in safe and "paddle" in safe.get("ocr", {}):
        tok = safe["ocr"]["paddle"].get("api_token", "")
        if tok:
            safe["ocr"]["paddle"]["api_token"] = tok[:6] + "***" if len(tok) > 6 else "***"
    return safe


@web.post("/api/settings")
def save_settings(data: dict):
    settings.save_settings(data)
    state.reload()
    return {"ok": True}


# ── OCR ──
@web.get("/api/ocr/status")
def ocr_status():
    cfg = state.cfg
    token = cfg.get("ocr", {}).get("paddle", {}).get("api_token", "")
    return {"paddle_enabled": bool(cfg.get("ocr", {}).get("enabled")),
            "paddle_token_set": bool(token)}


@web.post("/api/ocr/test")
def ocr_test():
    cfg = state.cfg
    try:
        result = paddle_ocr.test_connection(cfg.get("ocr", {}), cfg["data_root"])
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 文件管理 ──
@web.get("/api/files")
def list_files():
    cfg = state.cfg
    files = []
    for s in cfg.get("sources", []):
        for path in iter_files(s):
            fh = file_hash(path)
            ocr_dir = os.path.join(cfg["data_root"], "ocr", fh)
            md_path = os.path.join(ocr_dir, "document.md")
            files.append({"name": os.path.basename(path), "path": path,
                          "hash": fh, "imported": os.path.exists(md_path),
                          "md_path": md_path})
    return {"files": files}


@web.get("/api/file/markdown")
def get_markdown(path: str):
    if not os.path.exists(path):
        return {"ok": False, "message": "文件不存在"}
    with open(path, "r", encoding="utf-8") as f:
        return {"ok": True, "content": f.read()[:100000]}


# ── 启动 ──
if __name__ == "__main__":
    cfg = settings.get_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = cfg.get("server", {}).get("port", 8888)
    uvicorn.run("server:web", host=host, port=port, reload=False)
