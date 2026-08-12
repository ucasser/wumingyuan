# -*- coding: utf-8 -*-
"""
Small-to-Big 本地检索脚本 v2

改进点：
1. 使用简化 BM25/IDF 思路，避免常见词反复命中后所有结果同分。
2. 降低标题路径权重，避免标题里的“马克思主义/中国/传播”把分数刷满。
3. 增加关键词邻近度加分：多个词在正文中离得越近，排序越靠前。
4. 支持 --match-mode all/any、--exclude、--min-score 等参数。
5. 仍然保持 Small-to-Big：先检索 child，再回填 parent。

用法示例：
python search_small_to_big_v2.py --index "C:\\Users\\stream\\Desktop\\原始md文档\\small_to_big_output_v4" --query "唯物史观 阶级斗争" --top-k 8
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Dict, List, Any, Tuple, Iterable


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"JSONL 解析失败：{path} 第 {line_no} 行：{e}")
    return items


def normalize_text(s: str) -> str:
    s = s or ""
    s = s.replace("\u3000", " ")
    # 去掉 Markdown 标记中最常见的一部分，但不破坏中文文本。
    s = re.sub(r"[`*_>#\-]+", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def split_terms(s: str) -> List[str]:
    return [t.strip() for t in re.split(r"[\s,，；;、|]+", s.strip()) if t.strip()]


def first_positions(hay: str, terms: Iterable[str]) -> List[int]:
    pos: List[int] = []
    for term in terms:
        p = hay.find(term)
        if p >= 0:
            pos.append(p)
    return pos


def proximity_bonus(hay: str, terms: List[str]) -> Tuple[float, str]:
    """多个关键词在正文中越靠近，分数越高。"""
    if len(terms) < 2:
        return 0.0, ""
    pos = first_positions(hay, terms)
    if len(pos) < 2 or len(pos) < len(terms):
        return 0.0, ""
    span = max(pos) - min(pos)
    if span <= 80:
        return 14.0, f"关键词高度邻近，跨度约 {span} 字"
    if span <= 200:
        return 9.0, f"关键词较为邻近，跨度约 {span} 字"
    if span <= 500:
        return 5.0, f"关键词处于同一语义段，跨度约 {span} 字"
    if span <= 1000:
        return 2.0, f"关键词同块出现，跨度约 {span} 字"
    return 0.0, ""


def compute_idf(children: List[Dict[str, Any]], terms: List[str]) -> Dict[str, float]:
    """按 child 级别计算文档频率，常见词权重降低。"""
    n = max(len(children), 1)
    idf: Dict[str, float] = {}
    for term in terms:
        df = 0
        nt = normalize_text(term)
        for child in children:
            hay = normalize_text((child.get("text", "") or "") + " " + (child.get("heading_path", "") or ""))
            if nt and nt in hay:
                df += 1
        # 平滑 IDF。df 越大，idf 越低。
        idf[nt] = math.log((n + 1) / (df + 0.5) + 1.0)
    return idf


def score_child(
    child: Dict[str, Any],
    query: str,
    terms: List[str],
    idf: Dict[str, float],
    match_mode: str,
    exclude_terms: List[str],
) -> Tuple[float, List[str]]:
    text = child.get("text", "") or ""
    heading = child.get("heading_path", "") or ""
    hay_text = normalize_text(text)
    hay_heading = normalize_text(heading)
    hay_all = hay_text + hay_heading
    norm_query = normalize_text(query)
    norm_terms = [normalize_text(t) for t in terms if normalize_text(t)]
    norm_excludes = [normalize_text(t) for t in exclude_terms if normalize_text(t)]

    reasons: List[str] = []

    # 排除词：只要正文或标题中出现，就直接丢弃。
    for ex in norm_excludes:
        if ex and ex in hay_all:
            return 0.0, [f"排除词命中：{ex}"]

    matched_anywhere = [t for t in norm_terms if t in hay_all]
    matched_text = [t for t in norm_terms if t in hay_text]

    if match_mode == "all" and len(matched_anywhere) < len(norm_terms):
        return 0.0, [f"未满足 all 匹配：{len(matched_anywhere)}/{len(norm_terms)}"]
    if match_mode == "any" and not matched_anywhere:
        return 0.0, ["未命中任何关键词"]

    score = 0.0

    # 完整查询短语命中。空格去掉后仍然可命中连续表达。
    if norm_query and norm_query in hay_text:
        score += 24.0
        reasons.append("完整查询短语命中正文")
    elif norm_query and norm_query in hay_heading:
        score += 8.0
        reasons.append("完整查询短语命中标题路径")

    for raw_term, term in zip(terms, norm_terms):
        if not term:
            continue
        tf_text = hay_text.count(term)
        tf_heading = hay_heading.count(term)
        term_idf = idf.get(term, 1.0)
        if tf_text:
            add = (1.0 + math.log(tf_text + 1.0)) * term_idf * 8.0
            score += add
            reasons.append(f"关键词『{raw_term}』命中正文 {tf_text} 次，IDF={term_idf:.2f}")
        if tf_heading:
            # 标题只是辅助，不再给高权重。
            add = (1.0 + math.log(tf_heading + 1.0)) * term_idf * 1.2
            score += add
            reasons.append(f"关键词『{raw_term}』命中标题路径 {tf_heading} 次")

    # 多关键词共同出现在正文，才有组合分。
    if len(norm_terms) >= 2 and len(matched_text) >= 2:
        score += 3.0 * len(matched_text)
        reasons.append(f"多个关键词共同命中正文：{len(matched_text)}/{len(norm_terms)}")

    bonus, reason = proximity_bonus(hay_text, norm_terms)
    if bonus:
        score += bonus
        reasons.append(reason)

    # 稍微奖励中等长度 child，太短或过长都不优先。
    char_count = int(child.get("char_count", 0) or len(hay_text))
    if 300 <= char_count <= 800:
        score += 2.0
        reasons.append("child 长度适中")
    elif char_count < 180:
        score -= 4.0
        reasons.append("child 过短，降权")

    return max(score, 0.0), reasons


def truncate(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "……"


def make_result_md(
    query: str,
    terms: List[str],
    hits: List[Tuple[float, Dict[str, Any], Dict[str, Any], List[str]]],
    max_parent_chars: int,
    match_mode: str,
    exclude_terms: List[str],
) -> str:
    lines: List[str] = []
    lines.append("# Small-to-Big 检索结果 v2")
    lines.append("")
    lines.append(f"- 查询：`{query}`")
    lines.append(f"- 拆分关键词：{', '.join(terms)}")
    lines.append(f"- 匹配模式：{match_mode}")
    if exclude_terms:
        lines.append(f"- 排除词：{', '.join(exclude_terms)}")
    lines.append(f"- 返回结果数：{len(hits)}")
    lines.append("")

    for i, (score, child, parent, reasons) in enumerate(hits, start=1):
        book = child.get("book_title") or parent.get("book_title") or ""
        author = child.get("author") or parent.get("author") or ""
        heading = child.get("heading_path") or parent.get("heading_path") or ""
        page_start = child.get("page_start") or parent.get("page_start") or ""
        page_end = child.get("page_end") or parent.get("page_end") or ""
        source_path = child.get("source_path") or parent.get("source_path") or ""

        lines.append(f"## 结果 {i}")
        lines.append("")
        lines.append(f"- 分数：{score:.2f}")
        lines.append(f"- 书名：{book}")
        lines.append(f"- 作者：{author}")
        lines.append(f"- 标题路径：{heading}")
        lines.append(f"- 页码：{page_start} - {page_end}")
        lines.append(f"- child_id：{child.get('child_id', '')}")
        lines.append(f"- parent_id：{child.get('parent_id', '')}")
        lines.append(f"- 原文件：{source_path}")
        if reasons:
            lines.append(f"- 命中原因：{'；'.join(reasons)}")
        lines.append("")
        lines.append("### 命中小块")
        lines.append("")
        lines.append((child.get("text", "") or "").strip())
        lines.append("")
        lines.append("### 回填上下文 parent")
        lines.append("")
        lines.append(truncate(parent.get("text", ""), max_parent_chars))
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Small-to-Big 本地关键词检索 v2：先搜 children，再回填 parents。")
    parser.add_argument("--index", required=True, help="包含 parents.jsonl 和 children.jsonl 的目录")
    parser.add_argument("--query", required=True, help="检索词。多个关键词请用空格隔开，例如：唯物史观 阶级斗争")
    parser.add_argument("--top-k", type=int, default=10, help="返回结果数，默认 10")
    parser.add_argument("--output", default="", help="输出 markdown 路径；默认写入 index/search_results_v2.md")
    parser.add_argument("--max-parent-chars", type=int, default=2600, help="每条结果最多显示多少字 parent 上下文")
    parser.add_argument("--min-score", type=float, default=1.0, help="最低分数，默认 1")
    parser.add_argument("--match-mode", choices=["all", "any"], default="all", help="all=所有关键词都要命中；any=命中任一关键词即可。默认 all")
    parser.add_argument("--exclude", default="", help="排除词，多个词用空格隔开，例如：英国 德国 法国")
    parser.add_argument("--no-dedup-parent", action="store_true", help="默认同一 parent 只返回最高分 child；加此参数则不去重")
    args = parser.parse_args()

    index_dir = Path(args.index)
    parents_path = index_dir / "parents.jsonl"
    children_path = index_dir / "children.jsonl"

    parents = load_jsonl(parents_path)
    children = load_jsonl(children_path)
    parent_map = {p.get("parent_id"): p for p in parents if p.get("parent_id")}

    terms = split_terms(args.query)
    if not terms:
        raise ValueError("query 不能为空")
    exclude_terms = split_terms(args.exclude)
    idf = compute_idf(children, terms)

    scored: List[Tuple[float, Dict[str, Any], Dict[str, Any], List[str]]] = []
    for child in children:
        score, reasons = score_child(child, args.query, terms, idf, args.match_mode, exclude_terms)
        if score < args.min_score:
            continue
        parent = parent_map.get(child.get("parent_id"), {})
        scored.append((score, child, parent, reasons))

    scored.sort(key=lambda x: (x[0], x[1].get("char_count", 0)), reverse=True)

    results: List[Tuple[float, Dict[str, Any], Dict[str, Any], List[str]]] = []
    seen_parents = set()
    for item in scored:
        parent_id = item[1].get("parent_id")
        if not args.no_dedup_parent:
            if parent_id in seen_parents:
                continue
            seen_parents.add(parent_id)
        results.append(item)
        if len(results) >= args.top_k:
            break

    output_path = Path(args.output) if args.output else index_dir / "search_results_v2.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    md = make_result_md(args.query, terms, results, args.max_parent_chars, args.match_mode, exclude_terms)
    output_path.write_text(md, encoding="utf-8")

    print(f"[OK] query={args.query}")
    print(f"children={len(children)}, parents={len(parents)}, hits={len(scored)}, returned={len(results)}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()
