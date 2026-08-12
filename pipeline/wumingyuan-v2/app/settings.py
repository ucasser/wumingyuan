"""运行时设置层：在 config.yaml 之上叠加可持久化的覆盖项。

- 覆盖项存 data/settings.json（含 API key，已 gitignore），优先级高于 config.yaml。
- API key 解析顺序：settings.json 里的 api_key > 环境变量 api_key_env。
- 提供云端/本地连接测试，供 Web 设置面板「测试连接」按钮调用。
"""
import copy
import json
import os

import requests

from . import config


def _settings_path(cfg) -> str:
    return os.path.join(cfg.get("data_root", os.path.join(cfg["_base"], "data")), "settings.json")


def load_overrides(cfg) -> dict:
    p = _settings_path(cfg)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_overrides(cfg, patch: dict) -> dict:
    p = _settings_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    merged = deep_merge(load_overrides(cfg), patch)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def get_config(path: str = None):
    """加载 config.yaml 并叠加持久化覆盖项，返回可用配置。"""
    cfg = config.load(path)
    ov = load_overrides(cfg)
    for key in ("embedding", "llm", "retrieval", "chunk", "ocr", "academic",
                "privacy", "translation"):
        if key in ov:
            cfg[key] = deep_merge(cfg.get(key, {}), ov[key])
    # sources 是整体替换（网页管理后以覆盖项为准）
    if "sources" in ov:
        cfg["sources"] = ov["sources"]
    for src in cfg.get("sources", []):
        src["path"] = os.path.expanduser(src["path"])
    return cfg


_ALL_EXTS = "md,markdown,txt,text,rst,org,tex,log,html,htm,rtf,docx,pptx,pdf,epub"
DEFAULT_GLOB = {
    "auto": "**/*.{%s}" % _ALL_EXTS,       # 全部支持的格式，导入时按扩展名自动分类
    "note": "**/*.{md,markdown,txt,text,rst,org,tex,log,html,htm,rtf,docx,pptx}",
    "book": "**/*.{pdf,epub,docx}",
}


def save_sources(cfg, sources: list) -> list:
    """网页保存资料目录列表。自动补默认 glob。type 缺省为 auto（自动分类）。"""
    clean = []
    for s in sources or []:
        path = os.path.expanduser(str(s.get("path", "")).strip())
        if not path:
            continue
        stype = s.get("type") or "auto"
        clean.append({"path": path, "type": stype,
                      "glob": s.get("glob") or DEFAULT_GLOB.get(stype, DEFAULT_GLOB["auto"])})
    save_overrides(cfg, {"sources": clean})
    return clean


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_key(cloud: dict) -> str:
    """云端配置里取 key：显式 api_key 优先，否则读环境变量。"""
    if cloud.get("api_key"):
        return cloud["api_key"]
    return os.environ.get(cloud.get("api_key_env", ""), "")


def mask(key: str) -> str:
    if not key:
        return ""
    return "••••" + key[-4:] if len(key) > 4 else "••••"


# ---------------- 面向前端的视图 / 保存 ----------------
def public_view(cfg) -> dict:
    """返回给设置面板的当前配置（key 打码）。"""
    e, l = cfg["embedding"], cfg["llm"]
    oc = cfg.get("ocr", {}) or {}
    pc = oc.get("paddle", {}) or {}
    paddle_token = resolve_key({"api_key": pc.get("api_token", ""),
                                "api_key_env": pc.get("api_token_env", "PADDLEOCR_ACCESS_TOKEN")})
    presets = [
        {"name": "SiliconFlow (硅基流动)", "base_url": "https://api.siliconflow.cn/v1", "models": ["deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1", "Qwen/Qwen2.5-72B-Instruct"]},
        {"name": "DeepSeek 官方 API", "base_url": "https://api.deepseek.com", "models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
        {"name": "智谱 AI (GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-4-flash", "glm-4-plus", "embedding-3"]},
        {"name": "Moonshot (Kimi)", "base_url": "https://api.moonshot.cn/v1", "models": ["moonshot-v1-8k", "moonshot-v1-32k"]},
        {"name": "OpenAI 官方", "base_url": "https://api.openai.com/v1", "models": ["gpt-4o", "gpt-4o-mini", "text-embedding-3-small"]},
        {"name": "阿里云 DashScope (通义)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen-max", "qwen-plus"]}
    ]
    return {
        "presets": presets,
        "embedding": {
            "provider": e.get("provider"),
            "local": {"base_url": e["local"]["base_url"], "model": e["local"]["model"],
                      "dim": e["local"]["dim"]},
            "cloud": {"base_url": e["cloud"]["base_url"], "model": e["cloud"]["model"],
                      "dim": e["cloud"]["dim"], "api_key_set": bool(resolve_key(e["cloud"])),
                      "api_key_masked": mask(resolve_key(e["cloud"]))},
            "fallback_to_cloud": e.get("fallback_to_cloud", False),
        },
        "llm": {
            "provider": l.get("provider"),
            "local": {"base_url": l["local"]["base_url"], "model": l["local"]["model"]},
            "cloud": {"base_url": l["cloud"]["base_url"], "model": l["cloud"]["model"],
                      "api_key_set": bool(resolve_key(l["cloud"])),
                      "api_key_masked": mask(resolve_key(l["cloud"]))},
            "fallback_to_cloud": l.get("fallback_to_cloud", False),
        },
        "ocr": {
            "enabled": bool(oc.get("enabled")),
            "provider": oc.get("provider", "tesseract"),
            "lang": oc.get("lang", "chi_sim+eng"),
            "fallback_local": bool(oc.get("fallback_local", True)),
            "paddle": {
                "base_url": pc.get("base_url", ""),
                "model": pc.get("model", "PaddleOCR-VL-1.6"),
                "api_token_set": bool(paddle_token),
                "api_token_masked": mask(paddle_token),
                "orientation": bool(pc.get("orientation", True)),
                "unwarping": bool(pc.get("unwarping", False)),
                "max_pages_per_job": int(pc.get("max_pages_per_job", 1000)),
                "max_upload_mb": int(pc.get("max_upload_mb", 50)),
            },
        },
        "academic": {
            "auto_ingest_on_start": bool(cfg.get("academic", {}).get("auto_ingest_on_start", True)),
            "watch_inbox": bool(cfg.get("academic", {}).get("watch_inbox", True)),
            "auto_refresh_topics": bool(
                cfg.get("academic", {}).get("auto_refresh_topics", True)
            ),
            "outline_path": cfg.get("academic", {}).get("outline_path", ""),
            "top3_size": int(cfg.get("academic", {}).get("top3_size", 3)),
            "top10_size": int(cfg.get("academic", {}).get("top10_size", 10)),
            "data_root": cfg.get("data_root", ""),
        },
        "privacy": {
            "mode": cfg.get("privacy", {}).get("mode", "local_only"),
            "audit_enabled": bool(cfg.get("privacy", {}).get("audit_enabled", True)),
            "show_cloud_notice": bool(
                cfg.get("privacy", {}).get("show_cloud_notice", True)
            ),
        },
        "translation": {
            "auto_to_simplified_chinese": bool(
                cfg.get("translation", {}).get(
                    "auto_to_simplified_chinese", True
                )
            ),
            "target": cfg.get("translation", {}).get("target", "zh-CN"),
        },
    }


def sanitize_patch(patch: dict) -> dict:
    """去掉空 api_key（表示不改动），避免用打码值覆盖真实 key。"""
    patch = copy.deepcopy(patch or {})
    for section in ("embedding", "llm"):
        cloud = patch.get(section, {}).get("cloud", {})
        if "api_key" in cloud and not str(cloud["api_key"]).strip():
            cloud.pop("api_key")
    paddle = patch.get("ocr", {}).get("paddle", {})
    if "api_token" in paddle and not str(paddle["api_token"]).strip():
        paddle.pop("api_token")
    return patch


# ---------------- 连接测试 ----------------
def test_connection(cfg, target: str) -> dict:
    try:
        if target == "embed_local":
            return _ok(_ollama_embed(cfg["embedding"]["local"]))
        if target == "embed_cloud":
            return _ok(_cloud_embed(cfg["embedding"]["cloud"]))
        if target == "llm_local":
            return _ok(_ollama_chat(cfg["llm"]["local"]))
        if target == "llm_cloud":
            return _ok(_cloud_chat(cfg["llm"]["cloud"]))
        return {"ok": False, "msg": f"未知测试目标 {target}"}
    except Exception as ex:
        return {"ok": False, "msg": str(ex)}


def _ok(detail):
    return {"ok": True, "msg": detail}


def _ollama_embed(c):
    url = c["base_url"].rstrip("/") + "/api/embed"
    r = requests.post(url, json={"model": c["model"], "input": ["ping"]}, timeout=30)
    r.raise_for_status()
    dim = len(r.json()["embeddings"][0])
    return f"本地嵌入可用，维度 {dim}"


def _cloud_embed(c):
    key = resolve_key(c)
    if not key:
        raise RuntimeError("未设置 API key")
    url = c["base_url"].rstrip("/") + "/embeddings"
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                      json={"model": c["model"], "input": ["ping"]}, timeout=30)
    r.raise_for_status()
    dim = len(r.json()["data"][0]["embedding"])
    return f"云端嵌入可用，维度 {dim}"


def _ollama_chat(c):
    url = c["base_url"].rstrip("/") + "/api/chat"
    r = requests.post(url, json={"model": c["model"], "stream": False,
                                 "messages": [{"role": "user", "content": "ping"}]}, timeout=60)
    r.raise_for_status()
    return "本地 LLM 可用"


def _cloud_chat(c):
    key = resolve_key(c)
    if not key:
        raise RuntimeError("未设置 API key")
    url = c["base_url"].rstrip("/") + "/chat/completions"
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                      json={"model": c["model"],
                            "messages": [{"role": "user", "content": "ping"}]}, timeout=60)
    r.raise_for_status()
    return "云端 LLM 可用"
