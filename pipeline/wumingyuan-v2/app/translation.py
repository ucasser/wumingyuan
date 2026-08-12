"""非中文文献按需翻译为简体中文；原文与翻译层分开保存并可恢复。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace

import requests

from .academic import AcademicIndex
from .parsers import Document
from .settings import resolve_key


class DocumentTranslator:
    def __init__(self, cfg: dict, academic: AcademicIndex):
        self.cfg = cfg
        self.academic = academic
        self.options = cfg.get("translation", {}) or {}
        self.root = os.path.join(cfg["data_root"], "翻译缓存")
        os.makedirs(self.root, exist_ok=True)

    def translate(self, document: Document, meta: dict) -> tuple[Document, dict]:
        language = str(meta.get("detected_language", "unknown"))
        if language in {"zh", "unknown", "mixed"} or not self.options.get(
            "auto_to_simplified_chinese", True
        ):
            return document, meta
        if self.cfg.get("privacy", {}).get("mode") != "full":
            raise RuntimeError(
                "检测到非中文文献；自动翻译需要把文本发送给当前模型。"
                "请先在“隐私审计”中选择“完整问答”，或关闭自动翻译。"
            )
        translated = []
        for section in document.sections:
            translated.append(replace(
                section, text=self._translate_text(section.text, meta["document_id"])
            ))
        document = replace(document, sections=translated)
        for index, page in enumerate(meta.get("page_records", []) or []):
            if index < len(translated):
                page["normalized_text"] = translated[index].text
                page["display_text"] = translated[index].text
        meta["translated_from"] = language
        meta["translated_to"] = "zh-CN"
        meta["translation_model"] = self._model_name()
        return document, meta

    def _model_name(self) -> str:
        llm = self.cfg["llm"]
        provider = llm.get("provider", "local")
        return str(llm[provider].get("model", ""))

    def _translate_text(self, text: str, document_id: str) -> str:
        if not str(text or "").strip():
            return ""
        maximum = max(1000, int(self.options.get("max_chars_per_request", 5000)))
        parts = [text[i:i + maximum] for i in range(0, len(text), maximum)]
        return "\n".join(
            self._translate_part(part, document_id) for part in parts
        )

    def _translate_part(self, text: str, document_id: str) -> str:
        key = hashlib.sha256(
            (self._model_name() + "\0" + text).encode()
        ).hexdigest()
        path = os.path.join(self.root, key + ".json")
        try:
            with open(path, encoding="utf-8") as handle:
                cached = json.load(handle)
            if cached.get("source_hash") == hashlib.sha256(text.encode()).hexdigest():
                return str(cached["translation"])
        except (OSError, ValueError, KeyError):
            pass
        prompt = (
            "把下面文献正文忠实翻译为简体中文。保留人名、书名、术语、段落、"
            "引号和脚注编号；不概括、不解释、不遗漏。只输出译文。\n\n" + text
        )
        llm = self.cfg["llm"]
        provider = llm.get("provider", "local")
        model = self._model_name()
        try:
            if provider == "local":
                local = llm["local"]
                if not model:
                    raise RuntimeError("没有配置可用于翻译的本地模型")
                response = requests.post(
                    local["base_url"].rstrip("/") + "/api/chat",
                    json={"model": model, "stream": False, "messages": [
                        {"role": "user", "content": prompt}
                    ], "options": {"temperature": 0.0}},
                    timeout=(10, 240), proxies={"http": None, "https": None},
                )
                response.raise_for_status()
                value = response.json()["message"]["content"]
            else:
                cloud = llm["cloud"]
                token = resolve_key(cloud)
                if not token:
                    raise RuntimeError("没有配置云端模型 API Key")
                response = requests.post(
                    cloud["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": model, "messages": [
                        {"role": "user", "content": prompt}
                    ], "temperature": 0.0, "max_tokens": 6000},
                    timeout=(10, 240),
                )
                response.raise_for_status()
                value = response.json()["choices"][0]["message"]["content"]
                self.academic.record_cloud_audit(
                    privacy_mode="full", provider="cloud", model=model,
                    purpose="document_translation", query="translate-document",
                    chars_sent=len(prompt), redacted=False,
                    document_ids=[document_id], status="complete",
                )
        except Exception as ex:
            if provider == "cloud":
                self.academic.record_cloud_audit(
                    privacy_mode="full", provider="cloud", model=model,
                    purpose="document_translation", query="translate-document",
                    chars_sent=len(prompt), redacted=False,
                    document_ids=[document_id], status="failed", detail=str(ex),
                )
            raise
        value = str(value).strip()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({
                "source_hash": hashlib.sha256(text.encode()).hexdigest(),
                "translation": value,
            }, handle, ensure_ascii=False)
        os.replace(tmp, path)
        return value
