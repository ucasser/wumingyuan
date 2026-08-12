# -*- coding: utf-8 -*-
r"""
给批量引文卡片添加 Obsidian YAML 标签和 [[主题链接]]

用法示例：
python add_obsidian_quote_tags_fixed.py --input "D:\your\vault\02_候选引文库_Top3" --library-tag "引文库/Top3" --backup
"""

import argparse
import re
import shutil
from pathlib import Path

PEOPLE = {
    "马克思", "恩格斯", "列宁", "斯大林", "黑格尔", "费尔巴哈", "拉布里奥拉",
    "毛泽东", "李大钊", "陈独秀", "李达", "瞿秋白", "艾思奇", "胡绳",
    "陈唯实", "沈志远", "周恩来", "蔡和森", "李汉俊", "杨匏安",
    "鲁迅", "冯友兰", "张君劢", "梁启超", "康有为", "谭嗣同", "严复",
    "朱执信", "马君武", "郭沫若", "吕振羽", "范文澜", "侯外庐",
    "吴承仕", "翦伯赞", "张申府", "杨松", "王稼祥", "张如心",
}

STOPWORDS = {
    "中国", "马克思主义", "哲学", "思想", "历史", "理论", "研究", "问题",
    "传播", "发展", "意义", "批判", "方法", "特点", "关系", "讨论",
    "学术史", "世界观", "方法论", "实践", "文化", "社会", "政治",
    "传统", "现代", "科学", "人生观", "启蒙", "运动",
}

def has_frontmatter(text: str) -> bool:
    return text.startswith("---\n") or text.startswith("---\r\n")

def extract_query(text: str, path: Path) -> str:
    for line in text.splitlines()[:30]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    stem = path.stem
    stem = re.sub(r"^\d+[_\-]?", "", stem)
    return stem.replace("_", " ").strip()

def split_keywords(query: str) -> list[str]:
    q = query.replace("_", " ")
    parts = re.split(r"[\s、，,；;]+", q)
    parts = [p.strip(" #[]（）()《》“”\"'：:") for p in parts]
    parts = [p for p in parts if p and len(p) >= 2]
    out = []
    seen = set()
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out

def tag_safe(s: str) -> str:
    s = s.strip().replace(" ", "_")
    s = re.sub(r"[#\[\]\|\^\?！!，,。；;：:（）()《》“”\"'`~]", "", s)
    s = s.replace("/", "_")
    return s

def pick_tags_and_links(keywords: list[str]) -> tuple[list[str], list[str]]:
    tags = ["quote_card"]
    links = []

    for kw in keywords:
        if kw in PEOPLE:
            tags.append(f"人物/{tag_safe(kw)}")
            links.append(kw)
        elif kw not in STOPWORDS:
            tags.append(f"主题/{tag_safe(kw)}")
            links.append(kw)

    if len(links) == 0:
        for kw in keywords[:3]:
            tags.append(f"主题/{tag_safe(kw)}")
            links.append(kw)

    tags = list(dict.fromkeys(tags))
    links = list(dict.fromkeys(links))
    return tags, links

def make_frontmatter(query: str, tags: list[str], library_tag: str) -> str:
    all_tags = list(dict.fromkeys([library_tag] + tags))
    tag_lines = "\n".join([f"  - {t}" for t in all_tags])
    q = query.replace('"', '\\"')
    return f"""---
type: quote_card
source: batch_search_topics
query: "{q}"
tags:
{tag_lines}
---

"""

def make_links_section(links: list[str]) -> str:
    if not links:
        return ""
    link_text = " ".join(f"[[{x}]]" for x in links)
    return f"""

## Obsidian 关系链接

相关主题：{link_text}
"""

def process_file(path: Path, library_tag: str, backup: bool) -> tuple[str, str]:
    if path.name.lower() == "_index.md":
        return "skip", "index"
    if path.suffix.lower() != ".md":
        return "skip", "not md"
    if path.name.endswith(".md.bak"):
        return "skip", "backup"

    text = path.read_text(encoding="utf-8", errors="ignore")
    query = extract_query(text, path)
    keywords = split_keywords(query)
    tags, links = pick_tags_and_links(keywords)

    new_text = text
    fm_added = False
    links_added = False

    if not has_frontmatter(text):
        new_text = make_frontmatter(query, tags, library_tag) + new_text
        fm_added = True

    if "## Obsidian 关系链接" not in new_text:
        new_text = new_text.rstrip() + make_links_section(links) + "\n"
        links_added = True

    if new_text != text:
        if backup:
            bak = path.with_suffix(path.suffix + ".bak")
            if not bak.exists():
                shutil.copy2(path, bak)
        path.write_text(new_text, encoding="utf-8")
        return "updated", f"frontmatter={fm_added}, links={links_added}, query={query}"

    return "unchanged", query

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="引文库文件夹，例如 Top3 文件夹")
    parser.add_argument("--library-tag", default="引文库/Top3", help="整批卡片共同标签")
    parser.add_argument("--backup", action="store_true", help="修改前生成 .bak 备份")
    args = parser.parse_args()

    root = Path(args.input)
    if not root.exists():
        raise SystemExit(f"输入文件夹不存在：{root}")

    files = sorted(root.rglob("*.md"))
    updated = unchanged = skipped = 0

    for f in files:
        status, info = process_file(f, args.library_tag, args.backup)
        if status == "updated":
            updated += 1
        elif status == "unchanged":
            unchanged += 1
        else:
            skipped += 1

    print("完成。")
    print(f"输入文件夹：{root}")
    print(f"扫描 md 文件：{len(files)}")
    print(f"已更新：{updated}")
    print(f"未变化：{unchanged}")
    print(f"跳过：{skipped}")
    if args.backup:
        print("已为修改过的文件生成 .bak 备份。")

if __name__ == "__main__":
    main()
