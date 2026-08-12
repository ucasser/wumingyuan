#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
batch_search_topics_v2.py

用途：
  读取 topic_queries.txt，对 Small-to-Big 索引中的 children.jsonl 批量检索，
  命中 child 后自动回填 parent 上下文，并按 parent_id 去重后导出 Markdown 引文卡片。

适用文件结构：
  --index 指向一个文件夹，里面至少包含：
    parents.jsonl
    children.jsonl

推荐用法：
  python batch_search_topics.py ^
    --index "E:\博士学习\胡绳研究库\00_原文库\书籍库_index" ^
    --topics "D:\project\splitbookmd\topic_queries_all_core_first_run.txt" ^
    --output "E:\博士学习\胡绳研究库\02_引文库\首轮主题检索" ^
    --top-k 5 ^
    --max-queries 50

说明：
  第一次建议加 --max-queries 50 或 100 测试质量。
  确认质量后，再去掉 --max-queries 跑完整主题词。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception as e:
                print(f"[警告] JSON 解析失败：{path.name}:{i} {e}", file=sys.stderr)
    return rows


def get_any(obj: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for k in keys:
        v = obj.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            return " > ".join(str(x) for x in v if x is not None)
        return str(v)
    return default


def normalize_space(s: str) -> str:
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def read_topics(path: Path, start: int = 0, max_queries: int | None = None) -> List[str]:
    topics: List[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            raw = re.sub(r"\s+", " ", raw)
            topics.append(raw)
    if start > 0:
        topics = topics[start:]
    if max_queries is not None:
        topics = topics[:max_queries]
    return topics


def split_terms(query: str) -> List[str]:
    # 主题词文件本身已经用空格分词；这里保留 2 字及以上词，也保留人名/专名。
    terms = [t.strip() for t in re.split(r"\s+", query.strip()) if t.strip()]
    terms = [t for t in terms if len(t) >= 2 or re.search(r"[A-Za-z0-9]", t)]
    # 去重但保序
    seen = set()
    out = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def page_str(obj: Dict[str, Any]) -> str:
    # 兼容不同脚本可能写入的页码字段。
    for k in ["page_range", "pages", "page"]:
        v = obj.get(k)
        if v not in (None, "", []):
            return str(v)

    ps = obj.get("page_start", obj.get("start_page", obj.get("page_from", "")))
    pe = obj.get("page_end", obj.get("end_page", obj.get("page_to", "")))

    if ps not in (None, "") and pe not in (None, ""):
        if str(ps) == str(pe):
            return str(ps)
        return f"{ps}-{pe}"
    if ps not in (None, ""):
        return str(ps)
    return ""


def score_child(
    child: Dict[str, Any],
    query: str,
    exclude_terms: List[str] | None = None,
    book_filters: List[str] | None = None,
    title_filters: List[str] | None = None,
) -> Tuple[float, List[str]]:
    text = get_any(child, ["text", "content", "chunk", "body"])
    book = get_any(child, ["book_title", "book", "book_name", "source_title", "title"])
    heading = get_any(child, ["heading_path", "heading", "section_path", "path", "section"])
    hay_body = text
    hay_meta = f"{book} {heading}"
    hay_all = f"{book} {heading}\n{text}"

    if exclude_terms:
        for t in exclude_terms:
            if t and t in hay_all:
                return 0.0, []

    if book_filters:
        if not any(b in book for b in book_filters):
            return 0.0, []

    if title_filters:
        if not any(t in heading for t in title_filters):
            return 0.0, []

    terms = split_terms(query)
    if not terms:
        return 0.0, []

    matched: List[str] = []
    score = 0.0

    for term in terms:
        body_count = hay_body.count(term)
        meta_count = hay_meta.count(term)
        if body_count or meta_count:
            matched.append(term)
            score += 5.0
            score += min(body_count, 5) * 1.2
            score += min(meta_count, 3) * 2.0

    # 至少命中一定数量词，避免单词泛命中。
    required = 1 if len(terms) <= 2 else 2
    if len(matched) < required:
        return 0.0, []

    # 命中率加权。
    hit_ratio = len(matched) / max(len(terms), 1)
    score += hit_ratio * 8.0

    # 查询词整体或相邻组合命中加分。
    compact_query = "".join(terms)
    compact_text = hay_all.replace(" ", "").replace("\n", "")
    if len(compact_query) >= 4 and compact_query in compact_text:
        score += 10.0

    for a, b in zip(terms, terms[1:]):
        pair = a + b
        if len(pair) >= 4 and pair in compact_text:
            score += 3.0

    # 标题命中更有价值。
    if any(t in heading for t in terms):
        score += 4.0

    # 过短文本降权。
    if len(text) < 80:
        score *= 0.6

    return score, matched


def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", "_", s.strip())
    if len(s) > max_len:
        h = hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
        s = s[:max_len] + "_" + h
    return s or "query"


def trim_text(s: str, limit: int) -> str:
    s = normalize_space(s)
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "\n\n……【已截断】"


def md_escape_pipe(s: str) -> str:
    return str(s).replace("|", "｜")


def build_parent_map(parents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mp: Dict[str, Dict[str, Any]] = {}
    for p in parents:
        pid = get_any(p, ["id", "parent_id", "uid", "chunk_id"])
        if pid:
            mp[pid] = p
    return mp


def find_parent(child: Dict[str, Any], parent_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any] | None:
    pid = get_any(child, ["parent_id", "parent", "pid"])
    if pid and pid in parent_map:
        return parent_map[pid]
    # 有些脚本 child 自身可能就是大块。
    return None


def parent_key(child: Dict[str, Any]) -> str:
    """用于同一查询下按 parent 去重，避免 child overlap 造成同一大段重复出现。"""
    pid = get_any(child, ["parent_id", "parent", "pid"])
    if pid:
        return pid
    cid = get_any(child, ["id", "uid", "chunk_id"])
    if cid:
        return cid
    return hashlib.md5(json.dumps(child, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def dedupe_by_parent(scored: List[Tuple[float, List[str], Dict[str, Any]]]) -> List[Tuple[float, List[str], Dict[str, Any]]]:
    """同一 parent 只保留分数最高的 child，再排序。"""
    best: Dict[str, Tuple[float, List[str], Dict[str, Any]]] = {}
    for score, matched, child in scored:
        key = parent_key(child)
        if key not in best or score > best[key][0]:
            best[key] = (score, matched, child)
    out = list(best.values())
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def run(args: argparse.Namespace) -> None:
    index_dir = Path(args.index)
    children_path = index_dir / "children.jsonl"
    parents_path = index_dir / "parents.jsonl"

    if not children_path.exists():
        raise FileNotFoundError(f"找不到 children.jsonl：{children_path}")
    if not parents_path.exists():
        raise FileNotFoundError(f"找不到 parents.jsonl：{parents_path}")

    topics_path = Path(args.topics)
    if not topics_path.exists():
        raise FileNotFoundError(f"找不到 topics 文件：{topics_path}")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 读取索引：{index_dir}")
    children = read_jsonl(children_path)
    parents = read_jsonl(parents_path)
    parent_map = build_parent_map(parents)

    print(f"  children: {len(children)}")
    print(f"  parents : {len(parents)}")

    print(f"[2/4] 读取主题词：{topics_path}")
    topics = read_topics(topics_path, start=args.start, max_queries=args.max_queries)
    print(f"  queries : {len(topics)}")

    exclude_terms = args.exclude or []
    book_filters = args.book or []
    title_filters = args.title or []

    index_lines = [
        "# 批量主题检索结果索引",
        "",
        f"- 主题词文件：`{topics_path}`",
        f"- 索引目录：`{index_dir}`",
        f"- 检索主题数：{len(topics)}",
        f"- 每题输出 Top K：{args.top_k}",
        "- 已按 parent_id 去重：是",
        "",
        "| 序号 | 查询 | 结果数 | 文件 |",
        "|---:|---|---:|---|",
    ]

    print("[3/4] 开始批量检索")
    for qi, query in enumerate(topics, 1):
        scored = []
        for child in children:
            score, matched = score_child(
                child,
                query,
                exclude_terms=exclude_terms,
                book_filters=book_filters,
                title_filters=title_filters,
            )
            if score >= args.min_score:
                scored.append((score, matched, child))

        scored.sort(key=lambda x: x[0], reverse=True)
        scored = dedupe_by_parent(scored)
        top = scored[: args.top_k]

        filename = f"{qi:04d}_{safe_filename(query)}.md"
        out_path = out_dir / filename

        md = [
            f"# {query}",
            "",
            f"- 查询序号：{qi}",
            f"- 命中结果数：{len(top)}",
            f"- 已按 parent_id 去重：是",
            "",
        ]

        if not top:
            md.extend([
                "## 未命中",
                "",
                "这条主题词没有达到最低分数线。后续可以缩短查询词，或改用原文中更常见的表达。",
                "",
            ])
        else:
            for rank, (score, matched, child) in enumerate(top, 1):
                parent = find_parent(child, parent_map)
                child_text = get_any(child, ["text", "content", "chunk", "body"])
                parent_text = get_any(parent or {}, ["text", "content", "chunk", "body"])
                if not parent_text:
                    parent_text = child_text

                book = get_any(parent or {}, ["book_title", "book", "book_name", "source_title", "title"]) or get_any(child, ["book_title", "book", "book_name", "source_title", "title"])
                heading = get_any(parent or {}, ["heading_path", "heading", "section_path", "path", "section"]) or get_any(child, ["heading_path", "heading", "section_path", "path", "section"])
                pages = page_str(parent or child)

                md.extend([
                    f"## 结果 {rank}",
                    "",
                    "| 字段 | 内容 |",
                    "|---|---|",
                    f"| 分数 | {score:.2f} |",
                    f"| 命中词 | {md_escape_pipe('、'.join(matched))} |",
                    f"| 书名 | {md_escape_pipe(book)} |",
                    f"| 标题路径 | {md_escape_pipe(heading)} |",
                    f"| 页码 | {md_escape_pipe(pages)} |",
                    "",
                    "### 检索命中的短段",
                    "",
                    trim_text(child_text, args.child_chars),
                    "",
                    "### 回填原文上下文",
                    "",
                    trim_text(parent_text, args.parent_chars),
                    "",
                    "---",
                    "",
                ])

        out_path.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
        index_lines.append(f"| {qi} | {md_escape_pipe(query)} | {len(top)} | [{filename}](./{filename}) |")

        if qi % 50 == 0:
            print(f"  已完成 {qi}/{len(topics)}")

    index_path = out_dir / "_index.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    print("[4/4] 完成")
    print(f"输出目录：{out_dir}")
    print(f"索引文件：{index_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="批量主题词检索 Small-to-Big 索引并导出 Markdown 卡片")
    parser.add_argument("--index", required=True, help="包含 parents.jsonl 和 children.jsonl 的索引目录")
    parser.add_argument("--topics", required=True, help="主题词 txt 文件")
    parser.add_argument("--output", required=True, help="输出 Markdown 卡片目录")
    parser.add_argument("--top-k", type=int, default=5, help="每个主题词输出几个结果，默认 5")
    parser.add_argument("--min-score", type=float, default=3.0, help="最低命中分数，默认 3.0")
    parser.add_argument("--max-queries", type=int, default=None, help="最多检索多少条主题词，测试时建议 50 或 100")
    parser.add_argument("--start", type=int, default=0, help="从第几条主题词开始，默认 0")
    parser.add_argument("--child-chars", type=int, default=900, help="命中短段最多保留字数")
    parser.add_argument("--parent-chars", type=int, default=2200, help="回填上下文最多保留字数")
    parser.add_argument("--book", action="append", help="限定书名，可重复使用，例如 --book 艾思奇全书")
    parser.add_argument("--title", action="append", help="限定标题路径，可重复使用")
    parser.add_argument("--exclude", action="append", help="排除词，可重复使用")
    args = parser.parse_args()

    try:
        run(args)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
