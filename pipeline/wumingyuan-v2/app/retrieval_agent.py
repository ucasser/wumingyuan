"""检索 Agent：按 AGENTS.md 规则分层搜索本地引文库。

标准路径：主题页 → 全量映射 → Top3 → Top10 → 原始MD
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class RetrievalAgent:
    """分层检索引擎，严格遵循 AGENTS.md 规定的搜索次序。"""

    def __init__(self, project_root: str):
        self.root = os.path.abspath(project_root)
        self.rg = shutil.which("rg")
        self.paths = {
            "topic": os.path.join(self.root, "原文库_obsidianvault", "主题页"),
            "mapping": os.path.join(self.root, "辅助索引", "全量映射"),
            "top3": os.path.join(self.root, "原文库_obsidianvault", "02_核心引文库T3"),
            "top10": os.path.join(self.root, "原文库_obsidianvault", "03_扩展引文库T10"),
            "ocr_md": os.path.join(self.root, "原始md文档", "无名园OCR", "文档"),
            "core_md": os.path.join(self.root, "原始md文档", "文档"),
            "misc_md": os.path.join(self.root, "原始md文档", "杂文"),
        }

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """主入口：按规则分层检索，返回合并去重后的结果。"""
        keywords = self._extract_keywords(query)
        if not keywords:
            return []
        pattern = self._build_pattern(keywords)

        stage_1 = self._grep_dir(pattern, self.paths["topic"], top_k)
        card_names = self._extract_card_names(stage_1)

        if not stage_1:
            stage_1b = self._grep_dir(pattern, self.paths["mapping"], top_k)
            card_names = self._extract_card_names(stage_1b)

        if card_names:
            stage_2 = self._grep_dir(self._build_pattern(card_names), self.paths["top3"], top_k)
        else:
            stage_2 = self._grep_dir(pattern, self.paths["top3"], top_k)

        stage_3 = []
        if len(stage_2) < 5:
            stage_3 = self._grep_dir(pattern, self.paths["top10"], 5)

        stage_4 = self._grep_keyword(pattern, keywords, self.paths["ocr_md"], 5)
        if not stage_4:
            stage_4 = self._grep_keyword(pattern, keywords, self.paths["core_md"], 5)
        if not stage_4:
            stage_4 = self._grep_keyword(pattern, keywords, self.paths["misc_md"], 3)

        all_results = self._merge_results(stage_1, stage_2, stage_3, stage_4)
        return all_results[:top_k]

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        import re
        stop = {"的", "了", "和", "与", "及", "在", "中", "是", "有", "对",
                "从", "为", "这", "那", "什么", "怎么", "如何", "一个", "这个",
                "中国", "研究", "问题", "思想", "理论", "哲学", "马克思主义"}
        words = re.findall(r"[\u4e00-\u9fff]{2,8}", query)
        return [w for w in words if w not in stop][:10]

    def _build_pattern(self, terms: list[str]) -> str:
        return "|".join(set(terms))

    def _extract_card_names(self, results: list[dict]) -> list[str]:
        names = []
        for r in results:
            for snippet in r.get("snippets", []):
                import re
                found = re.findall(r"(\d{4}_.+?\.md)", snippet.get("text", ""))
                names.extend(found)
        return list(set(names))[:8]

    def _grep_dir(self, pattern: str, directory: str, limit: int) -> list[dict]:
        if not os.path.isdir(directory) or not pattern:
            return []
        try:
            if self.rg:
                cmd = ["rg", "-n", "-i", "--max-count", "5", "-l", pattern, directory]
            else:
                cmd = ["grep", "-rn", "-i", "-l", pattern, directory]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            matched_files = [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()][:limit * 2]
        except Exception:
            return []

        results = []
        for filepath in matched_files:
            if not os.path.isfile(filepath):
                continue
            try:
                content = open(filepath, "r", encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            lines = content.splitlines()
            snippets = []
            for i, line in enumerate(lines, 1):
                if any(t.lower() in line.lower() for t in pattern.split("|") if len(t) >= 2):
                    snippets.append({"line": i, "text": line.strip()[:300]})
                    if len(snippets) >= 5:
                        break
            if snippets:
                rel = os.path.relpath(filepath, self.root)
                results.append({"file": filepath, "rel_path": rel,
                                "name": os.path.basename(filepath),
                                "snippets": snippets})
        return results

    def _grep_keyword(self, pattern: str, keywords: list[str],
                      directory: str, limit: int) -> list[dict]:
        if not os.path.isdir(directory):
            return []
        results = []
        for kw in keywords[:6]:
            try:
                if self.rg:
                    cmd = ["rg", "-n", "-i", "--max-count", "3", "-l", kw, directory]
                else:
                    cmd = ["grep", "-rn", "-i", "-l", "--max-count=3", kw, directory]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                matched = [f.strip() for f in proc.stdout.strip().split("\n") if f.strip()][:3]
            except Exception:
                continue

            for filepath in matched:
                if not os.path.isfile(filepath):
                    continue
                try:
                    content = open(filepath, "r", encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                lines = content.splitlines()
                for i, line in enumerate(lines, 1):
                    if kw in line:
                        start = max(0, i - 3)
                        end = min(len(lines), i + 3)
                        ctx = "\n".join(lines[start:end])
                        rel = os.path.relpath(filepath, self.root)
                        results.append({"file": filepath, "rel_path": rel,
                                        "name": os.path.basename(filepath),
                                        "snippets": [{"line": i, "text": ctx[:500]}],
                                        "keyword": kw})
                        break
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return results

    def _merge_results(self, *stages) -> list[dict]:
        seen = set()
        merged = []
        for stage in stages:
            for r in stage:
                key = r["file"]
                if key not in seen:
                    seen.add(key)
                    merged.append(r)
        return merged
