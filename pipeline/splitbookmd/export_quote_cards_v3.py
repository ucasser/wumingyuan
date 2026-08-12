# -*- coding: utf-8 -*-

import argparse
import re
from pathlib import Path


def safe_filename(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[\\/:*?\"<>|]", "_", text)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:max_len] or "untitled"


def yaml_sq(text: str) -> str:
    if text is None:
        text = ""
    text = str(text)
    text = text.replace("'", "''")
    return f"'{text}'"


def parse_results(md: str):
    query_match = re.search(r"- 查询：`([^`]+)`", md)
    query = query_match.group(1).strip() if query_match else "未命名查询"

    parts = re.split(r"\n## 结果\s+(\d+)\n", md)
    results = []

    for i in range(1, len(parts), 2):
        num = parts[i].strip()
        body = parts[i + 1].strip()

        def get_field(label):
            m = re.search(rf"^- {re.escape(label)}：(.+)$", body, flags=re.MULTILINE)
            return m.group(1).strip() if m else ""

        score_raw = get_field("分数")
        try:
            score = float(score_raw)
        except Exception:
            score = 0.0

        title_path = get_field("标题路径")
        book_title = get_field("书名")
        author = get_field("作者")
        pages = get_field("页码")
        child_id = get_field("child_id")
        parent_id = get_field("parent_id")
        source_file = get_field("原文件")
        reason = get_field("命中原因")

        child_match = re.search(
            r"### 命中小块\n\n(.*?)(?=\n### 回填上下文 parent\n|\Z)",
            body,
            flags=re.S,
        )
        parent_match = re.search(
            r"### 回填上下文 parent\n\n(.*)$",
            body,
            flags=re.S,
        )

        child_text = child_match.group(1).strip() if child_match else ""
        parent_text = parent_match.group(1).strip() if parent_match else ""

        results.append({
            "num": num,
            "query": query,
            "score": score,
            "score_raw": score_raw,
            "book_title": book_title,
            "author": author,
            "title_path": title_path,
            "pages": pages,
            "child_id": child_id,
            "parent_id": parent_id,
            "source_file": source_file,
            "reason": reason,
            "child_text": child_text,
            "parent_text": parent_text,
            "raw": body,
        })

    return query, results


def write_card(out_dir: Path, result: dict):
    query = result["query"]
    score = result["score"]
    num = result["num"]
    title_path = result["title_path"] or "未识别标题路径"

    filename = f"{safe_filename(query)}_结果{num}_score{score:.2f}.md"
    path = out_dir / filename

    content = f"""---
type: {yaml_sq("候选引文")}
query: {yaml_sq(query)}
score: {score:.2f}
book_title: {yaml_sq(result['book_title'])}
author: {yaml_sq(result['author'])}
title_path: {yaml_sq(title_path)}
pages: {yaml_sq(result['pages'])}
child_id: {yaml_sq(result['child_id'])}
parent_id: {yaml_sq(result['parent_id'])}
source_file: {yaml_sq(result['source_file'])}
status: {yaml_sq("待核验")}
relevance: {yaml_sq("待判断")}
tags:
  - 候选引文
  - small-to-big
  - 待核验
---

# 候选引文｜{query}｜结果 {num}

## 基本信息

- 查询词：{query}
- 分数：{score:.2f}
- 书名：{result['book_title']}
- 作者：{result['author']}
- 标题路径：{title_path}
- 页码：{result['pages']}
- 原文件：`{result['source_file']}`

## 命中原因

{result['reason']}

## 命中小块

{result['child_text']}

## 回填上下文

{result['parent_text']}

## 人工核验记录

- 是否可引用：
- 可用于论证：
- 需要回原书核验的位置：
- 备注：
"""
    path.write_text(content, encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="search_results_v2.md 路径")
    parser.add_argument("--output", required=True, help="候选引文卡片输出文件夹")
    parser.add_argument("--min-score", type=float, default=0.0, help="最低分数，低于该分数不导出")
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = input_path.read_text(encoding="utf-8")
    query, results = parse_results(md)

    kept = [r for r in results if r["score"] >= args.min_score]
    written = []
    for r in kept:
        written.append(write_card(out_dir, r))

    index_lines = [
        "# 候选引文卡片索引",
        "",
        f"- 来源文件：`{input_path}`",
        f"- 查询词：`{query}`",
        f"- 最低分数：{args.min_score}",
        f"- 导出数量：{len(written)}",
        "",
    ]
    for p in written:
        index_lines.append(f"- [[{p.stem}]]")

    (out_dir / "_index.md").write_text("\n".join(index_lines), encoding="utf-8")

    print(f"[OK] 查询：{query}")
    print(f"[OK] 读取结果：{len(results)} 条")
    print(f"[OK] 导出卡片：{len(written)} 张")
    print(f"[OK] 输出目录：{out_dir}")
    print(f"[OK] 索引文件：{out_dir / '_index.md'}")


if __name__ == "__main__":
    main()
