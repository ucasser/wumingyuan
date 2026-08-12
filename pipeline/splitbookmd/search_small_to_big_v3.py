# -*- coding: utf-8 -*-
"""
search_small_to_big_v3.py

Small-to-Big 检索脚本 v3

相对 v2 的主要新增功能：
1. --book  限定书名。支持模糊包含匹配，可重复使用。
2. --title 限定标题路径。支持模糊包含匹配，可重复使用。
3. --exclude 排除词。结果中含有排除词的 child 会被跳过。
4. --list-books 查看当前 index 中有哪些书。
5. 输出 search_results_v3.md，不覆盖 v2 结果。

基本用法：
python search_small_to_big_v3.py --index "索引文件夹" --query "胡适 实用主义 布尔乔亚" --top-k 8

限定书名：
python search_small_to_big_v3.py --index "索引文件夹" --book "艾思奇全书" --query "抽象作用 辩证法 白马非马 形式论理学" --top-k 8

限定标题路径：
python search_small_to_big_v3.py --index "索引文件夹" --title "胡适论" --query "实用主义 布尔乔亚" --top-k 8

查看书名：
python search_small_to_big_v3.py --index "索引文件夹" --list-books
"""

import argparse
import collections
import json
import math
import re
from pathlib import Path
from typing import Dict, List, Any, Tuple


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    data = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"JSONL 解析失败：{path} 第 {line_no} 行：{e}") from e
    return data


def norm_text(x: Any) -> str:
    if x is None:
        return ""
    return str(x)


def split_query(query: str) -> List[str]:
    # 按空格拆分。中文主题词建议用空格手动分开，例如：抽象作用 辩证法 白马非马 形式论理学
    terms = [t.strip() for t in re.split(r"\s+", query.strip()) if t.strip()]
    # 去重但保留顺序
    seen = set()
    out = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def contains_any(text: str, patterns: List[str]) -> bool:
    if not patterns:
        return True
    return any(p in text for p in patterns)


def contains_all_terms(text: str, terms: List[str]) -> bool:
    return all(t in text for t in terms)


def count_occurrences(text: str, term: str) -> int:
    if not term:
        return 0
    return text.count(term)


def find_first_pos(text: str, term: str) -> int:
    try:
        return text.index(term)
    except ValueError:
        return -1


def page_str(obj: Dict[str, Any]) -> str:
    s = obj.get("page_start", "")
    e = obj.get("page_end", "")
    if s is None:
        s = ""
    if e is None:
        e = ""
    return f"{s} - {e}"


def truncate_for_reason(text: str, max_len: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "……"


def passes_filters(obj: Dict[str, Any], books: List[str], titles: List[str], excludes: List[str]) -> bool:
    book_title = norm_text(obj.get("book_title"))
    heading_path = norm_text(obj.get("heading_path"))
    text = norm_text(obj.get("text"))

    if books and not any(b in book_title for b in books):
        return False

    if titles and not any(t in heading_path for t in titles):
        return False

    haystack = f"{book_title}\n{heading_path}\n{text}"
    if excludes and any(x in haystack for x in excludes):
        return False

    return True


def build_idf(children: List[Dict[str, Any]], terms: List[str]) -> Dict[str, float]:
    n = max(1, len(children))
    df = {t: 0 for t in terms}
    for child in children:
        text = norm_text(child.get("text"))
        heading = norm_text(child.get("heading_path"))
        haystack = text + "\n" + heading
        for t in terms:
            if t in haystack:
                df[t] += 1

    idf = {}
    for t in terms:
        # 平滑 IDF，避免过度夸张
        idf[t] = math.log((n + 1) / (df[t] + 1)) + 1.0
    return idf


def match_child(child: Dict[str, Any], terms: List[str], mode: str) -> bool:
    text = norm_text(child.get("text"))
    heading = norm_text(child.get("heading_path"))
    haystack = text + "\n" + heading

    if mode == "all":
        return contains_all_terms(haystack, terms)
    if mode == "any":
        return any(t in haystack for t in terms)

    raise ValueError(f"未知匹配模式：{mode}")


def score_child(child: Dict[str, Any], terms: List[str], idf: Dict[str, float]) -> Tuple[float, List[str]]:
    text = norm_text(child.get("text"))
    heading = norm_text(child.get("heading_path"))
    char_count = int(child.get("char_count") or len(text))

    score = 0.0
    reasons = []

    text_hit_terms = []
    heading_hit_terms = []

    for t in terms:
        c_text = count_occurrences(text, t)
        c_heading = count_occurrences(heading, t)
        w = idf.get(t, 1.0)

        if c_text:
            text_hit_terms.append(t)
            score += c_text * w * 10
            reasons.append(f"关键词『{t}』命中正文 {c_text} 次，IDF={w:.2f}")

        if c_heading:
            heading_hit_terms.append(t)
            score += c_heading * w * 4
            reasons.append(f"关键词『{t}』命中标题路径 {c_heading} 次")

    # 多关键词共同命中正文更重要
    if len(text_hit_terms) >= 2:
        score += len(text_hit_terms) ** 2 * 8
        reasons.append(f"多个关键词共同命中正文：{len(text_hit_terms)}/{len(terms)}")

    if len(text_hit_terms) == len(terms):
        score += 30
        reasons.append("所有关键词均命中正文")

    # 邻近度奖励
    positions = [find_first_pos(text, t) for t in terms if find_first_pos(text, t) >= 0]
    if len(positions) >= 2:
        span = max(positions) - min(positions)
        if span <= 80:
            bonus = 32
            reasons.append(f"关键词高度邻近，跨度约 {span} 字")
        elif span <= 300:
            bonus = 20
            reasons.append(f"关键词较为邻近，跨度约 {span} 字")
        elif span <= 700:
            bonus = 10
            reasons.append(f"关键词处于同一语义段，跨度约 {span} 字")
        else:
            bonus = 0
        score += bonus

    # child 长度适中更适合做命中小块
    if 250 <= char_count <= 900:
        score += 8
        reasons.append("child 长度适中")
    elif 120 <= char_count <= 1500:
        score += 4

    return score, reasons


def render_result_md(
    query: str,
    terms: List[str],
    mode: str,
    results: List[Dict[str, Any]],
    parents_by_id: Dict[str, Dict[str, Any]],
    books: List[str],
    titles: List[str],
    excludes: List[str],
) -> str:
    lines = []
    lines.append("# Small-to-Big 检索结果 v3")
    lines.append("")
    lines.append(f"- 查询：`{query}`")
    lines.append(f"- 拆分关键词：{', '.join(terms)}")
    lines.append(f"- 匹配模式：{mode}")
    if books:
        lines.append(f"- 限定书名：{', '.join(books)}")
    if titles:
        lines.append(f"- 限定标题路径：{', '.join(titles)}")
    if excludes:
        lines.append(f"- 排除词：{', '.join(excludes)}")
    lines.append(f"- 返回结果数：{len(results)}")
    lines.append("")

    if not results:
        lines.append("## 未找到结果")
        lines.append("")
        lines.append("可能原因：")
        lines.append("")
        lines.append("- 查询词过窄，所有关键词无法同时命中。可以改用 `--match any`。")
        lines.append("- 书名或标题路径过滤过严。")
        lines.append("- 原文使用了不同说法，例如“形式论理学”而不是“形式逻辑”。")
        lines.append("")
        return "\n".join(lines)

    for i, item in enumerate(results, 1):
        child = item["child"]
        parent = parents_by_id.get(child.get("parent_id"), {})
        reasons = item["reasons"]

        lines.append(f"## 结果 {i}")
        lines.append("")
        lines.append(f"- 分数：{item['score']:.2f}")
        lines.append(f"- 书名：{norm_text(child.get('book_title'))}")
        lines.append(f"- 作者：{norm_text(child.get('author'))}")
        lines.append(f"- 标题路径：{norm_text(child.get('heading_path'))}")
        lines.append(f"- 页码：{page_str(child)}")
        lines.append(f"- child_id：{norm_text(child.get('child_id'))}")
        lines.append(f"- parent_id：{norm_text(child.get('parent_id'))}")
        lines.append(f"- 原文件：{norm_text(child.get('source_path'))}")
        lines.append(f"- 命中原因：{'；'.join(reasons)}")
        lines.append("")
        lines.append("### 命中小块")
        lines.append("")
        lines.append(norm_text(child.get("text")).strip())
        lines.append("")
        lines.append("### 回填上下文 parent")
        lines.append("")
        parent_text = norm_text(parent.get("text")).strip()
        if parent_text:
            lines.append(parent_text)
        else:
            lines.append("> 未找到 parent。请检查 parents.jsonl 与 children.jsonl 是否来自同一次切块。")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def list_books(index_dir: Path):
    parents = read_jsonl(index_dir / "parents.jsonl")
    counter = collections.Counter(norm_text(p.get("book_title")) for p in parents)
    print("当前 index 中的书名：")
    for book, count in counter.most_common():
        print(f"- {book}: {count} parents")


def parse_multi_values(values: List[str]) -> List[str]:
    # 支持重复参数，也支持逗号分隔
    out = []
    for v in values or []:
        for part in re.split(r"[,，]", v):
            part = part.strip()
            if part:
                out.append(part)
    # 去重保序
    seen = set()
    final = []
    for x in out:
        if x not in seen:
            seen.add(x)
            final.append(x)
    return final


def main():
    parser = argparse.ArgumentParser(description="Small-to-Big 检索脚本 v3")
    parser.add_argument("--index", required=True, help="索引文件夹，里面应包含 parents.jsonl 和 children.jsonl")
    parser.add_argument("--query", default="", help="查询词，用空格分隔多个关键词")
    parser.add_argument("--top-k", type=int, default=8, help="返回结果数")
    parser.add_argument("--match", choices=["all", "any"], default="all", help="匹配模式：all=所有关键词都要命中；any=任一关键词命中")
    parser.add_argument("--book", action="append", default=[], help="限定书名，可重复使用，也可用逗号分隔")
    parser.add_argument("--title", action="append", default=[], help="限定标题路径，可重复使用，也可用逗号分隔")
    parser.add_argument("--exclude", action="append", default=[], help="排除词，可重复使用，也可用逗号分隔")
    parser.add_argument("--output", default="", help="输出 md 文件路径。默认写到 index 文件夹下的 search_results_v3.md")
    parser.add_argument("--list-books", action="store_true", help="列出 index 中的书名和 parent 数量")

    args = parser.parse_args()

    index_dir = Path(args.index)
    if args.list_books:
        list_books(index_dir)
        return

    if not args.query.strip():
        raise SystemExit("错误：必须提供 --query，或者使用 --list-books 查看书名。")

    terms = split_query(args.query)
    if not terms:
        raise SystemExit("错误：查询词为空。")

    books = parse_multi_values(args.book)
    titles = parse_multi_values(args.title)
    excludes = parse_multi_values(args.exclude)

    parents = read_jsonl(index_dir / "parents.jsonl")
    children = read_jsonl(index_dir / "children.jsonl")
    parents_by_id = {norm_text(p.get("parent_id")): p for p in parents}

    # 先按书名/标题/排除词过滤，再做 IDF 与匹配
    filtered_children = [
        c for c in children
        if passes_filters(c, books=books, titles=titles, excludes=excludes)
    ]

    idf = build_idf(filtered_children, terms)

    scored = []
    for child in filtered_children:
        if not match_child(child, terms, args.match):
            continue
        score, reasons = score_child(child, terms, idf)
        if score <= 0:
            continue
        scored.append({"score": score, "child": child, "reasons": reasons})

    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[: max(1, args.top_k)]

    md = render_result_md(
        query=args.query,
        terms=terms,
        mode=args.match,
        results=results,
        parents_by_id=parents_by_id,
        books=books,
        titles=titles,
        excludes=excludes,
    )

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = index_dir / "search_results_v3.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    print(f"[OK] 检索完成：{len(results)} 条结果")
    print(f"[OK] 输出文件：{out_path}")


if __name__ == "__main__":
    main()
