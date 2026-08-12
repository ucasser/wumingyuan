"""LLM 调用层：兼容 OpenAI / DeepSeek / 智谱 / Kimi / 豆包 等 API。"""
import json
import os
import time
from typing import Optional

import requests


# AGENTS.md 中的核心检索规则，作为系统提示注入
RETRIEVAL_SYSTEM_PROMPT = """你是一个基于用户本地文献库的学术研究助手。

## 你的能力
用户已经通过本地搜索引擎搜索了文献库，搜索结果附在下方。你的任务是：

1. 根据搜索结果中的原文材料回答用户的问题
2. 每条引用必须用 [文件名] 标注来源
3. 如果搜索结果不足以回答问题，直接说明"当前搜索的材料不足以回答，建议调整关键词或扩充文献库"
4. 直接引文必须使用中文双引号，不得改写原文
5. 不要编造引文、页码或出处

## 材料使用原则
- 优先使用命中词具体、上下文直接相关的材料
- 谨慎使用只命中泛词的材料
- 页码缺失时标注"页码待核"
- 引文前说明引用目的，引文后进行分析
"""


class LLMClient:
    """统一的 LLM API 客户端。"""

    # 预置的常用模型配置
    PRESETS = {
        "deepseek": {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "glm": {
            "name": "智谱 GLM",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "models": ["glm-4-flash", "glm-4-plus"],
        },
        "kimi": {
            "name": "Kimi（月之暗面）",
            "base_url": "https://api.moonshot.cn/v1",
            "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        },
        "openai": {
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "models": ["gpt-4o", "gpt-4o-mini"],
        },
    }

    def __init__(self, config: dict):
        self.base_url = config.get("base_url", "").rstrip("/")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        self.timeout = config.get("timeout", 60)

    def chat(self, question: str, search_results: list[dict],
             conversation_history: Optional[list] = None) -> dict:
        """发送问题 + 搜索结果给 LLM，返回带引用的回答。

        Args:
            question: 用户问题
            search_results: 检索 Agent 返回的命中列表
            conversation_history: 可选的历史对话

        Returns:
            {"answer": str, "error": str|None}
        """
        if not self.api_key:
            return {"answer": "", "error": "未配置 API Key，请在设置页面填写"}

        context = self._build_context(search_results)
        if not context:
            return {"answer": "未找到相关文献材料。", "error": None}

        messages = [{"role": "system", "content": RETRIEVAL_SYSTEM_PROMPT}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({
            "role": "user",
            "content": f"【文献库搜索结果】\n\n{context}\n\n---\n【用户问题】\n{question}",
        })

        return self._call_api(messages)

    def _system_prompt(self) -> str:
        return RETRIEVAL_SYSTEM_PROMPT

    def list_models(self) -> list[dict]:
        """返回所有预置模型的列表。"""
        result = []
        for pid, preset in self.PRESETS.items():
            for model in preset["models"]:
                result.append({"id": f"{pid}:{model}",
                               "provider": preset["name"],
                               "model": model,
                               "base_url": preset["base_url"]})
        return result

    def _build_context(self, results: list[dict]) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            name = r.get("name", r.get("rel_path", ""))
            snips = r.get("snippets", [])
            text = "\n".join(s.get("text", "") for s in snips[:3])
            if text.strip():
                parts.append(f"[{i}] 来源：{name}\n{text}")
        return "\n\n---\n\n".join(parts)

    def _call_api(self, messages: list[dict]) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "messages": messages,
                "temperature": 0.3, "max_tokens": 4096}

        try:
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                answer = data["choices"][0]["message"]["content"]
                return {"answer": answer, "error": None,
                        "model": self.model,
                        "tokens": data.get("usage", {}).get("total_tokens", 0)}
            else:
                detail = resp.json().get("error", {}).get("message", resp.text)
                return {"answer": "", "error": f"API 错误 ({resp.status_code}): {detail}"}
        except requests.exceptions.Timeout:
            return {"answer": "", "error": "API 请求超时，请检查网络或重试"}
        except Exception as e:
            return {"answer": "", "error": f"连接失败: {str(e)}"}
