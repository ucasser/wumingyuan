"""配置加载：读取 config.yaml，展开 ~ 路径，提供点式访问。"""
import os
import yaml

_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")


class Config(dict):
    """支持 cfg['a']['b'] 也支持 cfg.get('a.b') 的轻量配置对象。"""

    def get(self, dotted, default=None):
        node = self
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node


def load(path: str = None) -> Config:
    path = path or os.environ.get("LOCALLM_CONFIG", _DEFAULT)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = Config(raw)
    # db_path 相对于配置文件所在目录展开
    base = os.path.dirname(os.path.abspath(path))
    cfg["_base"] = base
    cfg["data_root"] = _abspath(cfg.get("data_root", "./data"), base)
    if cfg.get("db_path"):
        cfg["db_path"] = _abspath(cfg["db_path"], base)
    for src in cfg.get("sources", []):
        src["path"] = os.path.expanduser(src["path"])
    ollama = os.environ.get("LOCALLM_OLLAMA_URL")
    if ollama and cfg.get("embedding") and cfg.get("llm"):
        cfg["embedding"]["local"]["base_url"] = ollama
        cfg["llm"]["local"]["base_url"] = ollama
    return cfg


def _abspath(p: str, base: str) -> str:
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
