"""Web 搜索工具：调 ripgrep/grep 在卡片库和原始 MD 中全文检索。"""
import os
import re
import shutil
import subprocess
from pathlib import Path


class SearchTools:
    """纯文本搜索引擎，不依赖向量库。搜索路径遵循 AGENTS.md 规则。"""

    def __init__(self, sources: list):
        self.paths = []
        for item in sources or []:
            p = os.path.expanduser(str(item.get("path", "")))
            if os.path.isdir(p):
                self.paths.append(p)
        self.rg = shutil.which("rg")

    def search(self, query: str, limit: int = 30) -> list[dict]:
        """在全部索引目录中搜索关键词，返回命中列表。"""
        if not self.paths or not query:
            return []

        terms = self._split_query(query)
        pattern = "|".join(re.escape(t) for t in terms[:8])
        results = []

        for root in self.paths:
            try:
                hits = self._grep(pattern, root, limit)
                results.extend(hits)
            except Exception:
                continue

        results.sort(key=lambda r: r["lines_matched"], reverse=True)
        return results[:limit]

    def read_file(self, path: str, start: int = 0, length: int = 200) -> dict:
        """读取文件的指定行范围。"""
        if not os.path.isfile(path):
            return {"ok": False, "error": "文件不存在"}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            total = len(lines)
            chunk = lines[start:start + length]
            return {"ok": True, "content": "".join(chunk), "total_lines": total,
                    "start": start, "end": min(start + length, total)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _split_query(query: str) -> list[str]:
        parts = re.split(r"[\s、，,；;]+", query.strip())
        return [p for p in parts if len(p) >= 2][:12]

    def _grep(self, pattern: str, root: str, limit: int) -> list[dict]:
        results = []
        if self.rg:
            cmd = ["rg", "-n", "-i", "--max-count", "3", "-l", pattern, root]
        else:
            cmd = ["grep", "-rn", "-i", "-l", "--max-count=3", pattern, root]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            files = [f for f in proc.stdout.strip().split("\n") if f][:limit * 2]
        except Exception:
            return []

        for filepath in files:
            if not os.path.isfile(filepath):
                continue
            try:
                rel = os.path.relpath(filepath, root)
                fname = os.path.basename(filepath)
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                match_lines = []
                for line_no, line in enumerate(text.splitlines(), 1):
                    if re.search(pattern, line, re.I):
                        match_lines.append({"line": line_no, "text": line.strip()[:200]})
                        if len(match_lines) >= 3:
                            break
                if match_lines:
                    results.append({"file": filepath, "name": fname, "rel_path": rel,
                                    "lines_matched": len(match_lines),
                                    "snippets": match_lines})
            except Exception:
                continue

        return results
