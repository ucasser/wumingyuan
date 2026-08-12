# -*- coding: utf-8 -*-
"""
build_small_to_big_v4.py

用途：
把已经清理为“正文版”的 Markdown 文档，转换为 Small-to-Big Retrieval 可用的
parents.jsonl 与 children.jsonl。

第四版修正：
1. 新标题出现时强制切断 parent，避免 parent 跨章节/小节。
2. 去掉 child 的“尾巴字符硬拼接”机制，避免 child 内部出现重复片段。
3. 保留标题与正文之间的段落边界，避免“标题正文粘连”。
3. child 改为“段落窗口 + 可选段落级重叠”，优先保持自然段完整。
4. 自动合并过短 child，避免小块从半个词或半句话开始。
5. 更严格地跳过页码、脚注、元数据表、图片、纯分隔线等噪声。
5. 预览文件增加“质量警告”，便于先检查再向量化。

用法示例：
python build_small_to_big_v4.py `
  --source "C:\\Users\\stream\\Desktop\\原始md文档\\文档" `
  --output "C:\\Users\\stream\\Desktop\\原始md文档\\small_to_big_output_v2" `
  --parent-size 2200 `
  --parent-overlap 0 `
  --child-size 500 `
  --child-overlap 0
"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CJK_FOOTNOTE_NUMS = "①②③④⑤⑥⑦⑧⑨⑩"


def stable_id(text: str, prefix: str = "") -> str:
    h = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}{h}" if prefix else h


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_metadata(raw: str, path: Path) -> Dict[str, str]:
    meta = {
        "book_title": path.stem,
        "author": "",
        "source_type": "book",
    }

    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*", raw, flags=re.S)
    if m:
        yaml_text = m.group(1)
        for line in yaml_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k in ("book_title", "title", "书名"):
                    meta["book_title"] = v
                elif k in ("author", "作者"):
                    meta["author"] = v
                elif k in ("publisher", "出版社"):
                    meta["publisher"] = v
                elif k in ("publication_year", "出版年份", "year"):
                    meta["publication_year"] = v
                elif k in ("source_type", "文献类型"):
                    meta["source_type"] = v

    table_patterns = {
        "书名": "book_title",
        "作者": "author",
        "出版社": "publisher",
        "出版年份": "publication_year",
        "出版地": "publication_place",
        "文献类型": "source_type",
    }
    for line in raw.splitlines()[:160]:
        line = line.strip()
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$", line)
        if m:
            k = m.group(1).strip()
            v = m.group(2).strip()
            if k in table_patterns and v and v != "内容":
                meta[table_patterns[k]] = v

    return meta


def is_page_number_line(line: str) -> Optional[int]:
    s = line.strip()
    if re.fullmatch(r"\d{1,4}", s):
        n = int(s)
        if 1 <= n <= 3000:
            return n
    return None


def is_footnote_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(rf"^[{CJK_FOOTNOTE_NUMS}]\s*", s):
        return True
    if re.match(r"^\$\s*\^\{?[①②③④⑤⑥⑦⑧⑨⑩0-9]+\}?\s*\$", s):
        return True
    # 典型脚注文献行：含作者/书名/出版社/年份/页码等
    if re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩].*(出版社|人民出版社|全集|选集|文集|卷|版|转引)", s):
        return True
    return False


def is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("<!--") and s.endswith("-->"):
        return True
    if s.startswith("![") or re.match(r"^!\[\[.*\]\]$", s):
        return True
    if s in ("<!-- AI整理信息开始 -->", "<!-- AI整理信息结束 -->"):
        return True
    if re.match(r"^[-–—_]{3,}$", s):
        return True
    return False


def normalize_paragraph_text(text: str) -> str:
    text = text.strip()
    # 中文行内断行合并
    text = re.sub(r"(?<=[\u4e00-\u9fff])\n(?=[\u4e00-\u9fff])", "", text)
    # 保留段落间的空行，避免把标题和正文、相邻自然段粘在一起。
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_and_collect_paragraphs(raw: str) -> Tuple[List[Dict], Dict[str, str]]:
    lines = raw.splitlines()
    paragraphs: List[Dict] = []
    buf: List[str] = []
    buf_page_start = None
    buf_page_end = None
    current_page = None
    heading_stack: List[Tuple[int, str]] = []

    in_metadata_table = False
    in_yaml = False
    yaml_boundary_count = 0

    def current_heading_path() -> str:
        return " > ".join(title for _, title in heading_stack)

    def flush():
        nonlocal buf, buf_page_start, buf_page_end
        if not buf:
            return
        text = normalize_paragraph_text("\n".join(buf))
        if len(text) >= 20:
            paragraphs.append({
                "text": text,
                "heading_path": current_heading_path(),
                "page_start": buf_page_start,
                "page_end": buf_page_end,
                "is_heading": False,
            })
        buf = []
        buf_page_start = None
        buf_page_end = None

    for i, line in enumerate(lines):
        s = line.rstrip("\n").strip()

        # YAML front matter
        if i < 8 and s == "---":
            in_yaml = True
            yaml_boundary_count += 1
            continue
        if in_yaml:
            if s == "---":
                yaml_boundary_count += 1
                if yaml_boundary_count >= 2:
                    in_yaml = False
            continue

        # 元数据表：从 “## 元数据” 到下一个标题
        if re.match(r"^#{1,6}\s*元数据\s*$", s):
            flush()
            in_metadata_table = True
            continue
        if in_metadata_table:
            if re.match(r"^#{1,6}\s+", s) and "元数据" not in s:
                in_metadata_table = False
            else:
                continue

        page = is_page_number_line(s)
        if page is not None:
            current_page = page
            continue

        if is_noise_line(s) or is_footnote_line(s):
            continue

        # 跳过纯表格行，通常来自元数据或整理痕迹
        if s.startswith("|") and s.endswith("|"):
            continue

        hm = re.match(r"^(#{1,6})\s*(.+?)\s*$", s)
        if hm:
            flush()
            level = len(hm.group(1))
            title = hm.group(2).strip()
            title = re.sub(r"\.{3,}.*$", "", title).strip()
            if title:
                # 更新标题栈
                heading_stack = [(lv, t) for lv, t in heading_stack if lv < level]
                heading_stack.append((level, title))
                paragraphs.append({
                    "text": f"{'#' * level} {title}",
                    "heading_path": current_heading_path(),
                    "page_start": current_page,
                    "page_end": current_page,
                    "is_heading": True,
                    "heading_level": level,
                })
            continue

        if not s:
            flush()
            continue

        if buf_page_start is None:
            buf_page_start = current_page
        buf_page_end = current_page
        buf.append(s)

    flush()
    return paragraphs, {}


def make_parents(paragraphs: List[Dict], meta: Dict[str, str], source_path: Path, parent_size: int, parent_overlap: int) -> List[Dict]:
    parents: List[Dict] = []
    current: List[Dict] = []
    current_len = 0
    current_heading = ""
    page_start = None
    page_end = None

    def emit(use_overlap: bool = True):
        nonlocal current, current_len, current_heading, page_start, page_end
        if not current:
            return
        text = "\n\n".join(p["text"] for p in current).strip()
        if len(text) >= 80:
            raw_id_basis = f"{source_path}|{len(parents)}|{text[:200]}"
            parent_id = stable_id(raw_id_basis, "p_")
            parents.append({
                "parent_id": parent_id,
                "book_title": meta.get("book_title", source_path.stem),
                "author": meta.get("author", ""),
                "publisher": meta.get("publisher", ""),
                "publication_year": meta.get("publication_year", ""),
                "source_type": meta.get("source_type", "book"),
                "source_path": str(source_path),
                "parent_index": len(parents),
                "heading_path": current_heading,
                "page_start": page_start,
                "page_end": page_end,
                "char_count": len(text),
                "text": text,
            })

        if use_overlap and parent_overlap > 0 and current:
            overlap: List[Dict] = []
            total = 0
            # 不把标题带入 overlap；避免新 parent 开头携带上个标题
            for old in reversed(current):
                if old.get("is_heading"):
                    break
                overlap.insert(0, old)
                total += len(old["text"])
                if total >= parent_overlap:
                    break
            current = overlap
            current_len = sum(len(x["text"]) for x in current)
            page_start = current[0].get("page_start") if current else None
            page_end = current[-1].get("page_end") if current else None
        else:
            current = []
            current_len = 0
            page_start = None
            page_end = None

    for p in paragraphs:
        text = p["text"]
        p_len = len(text)

        # 关键修正：新标题出现时，先结束旧 parent，且不跨标题 overlap。
        # 这样不会出现“上一节内容 + 下一节标题路径”的错位。
        if p.get("is_heading"):
            if current:
                emit(use_overlap=False)
            current_heading = p.get("heading_path", "")
            current = [p]
            current_len = p_len
            page_start = p.get("page_start")
            page_end = p.get("page_end")
            continue

        if p.get("heading_path"):
            current_heading = p.get("heading_path")

        if page_start is None and p.get("page_start") is not None:
            page_start = p.get("page_start")
        if p.get("page_end") is not None:
            page_end = p.get("page_end")

        if current and current_len + p_len > parent_size:
            emit(use_overlap=True)

        current.append(p)
        current_len += p_len

    emit(use_overlap=False)
    return parents


def split_parent_into_children(parent_text: str, child_size: int, child_overlap: int) -> List[str]:
    """
    段落优先切分：
    - 不再做“上一块尾巴字符 + 下一块正文”的硬拼接，避免重复片段。
    - child_overlap 表示段落级重叠的目标字数；只从上一块末尾取完整段落。
    """
    paras = [normalize_paragraph_text(x) for x in re.split(r"\n{2,}", parent_text) if normalize_paragraph_text(x)]
    chunks: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0

    def emit_cur():
        nonlocal cur, cur_len
        if cur:
            chunks.append(cur)
            cur = []
            cur_len = 0

    for para in paras:
        # 超长自然段：按句号等标点切；切不开再按字符切
        if len(para) > child_size * 1.4:
            emit_cur()
            sentences = re.split(r"(?<=[。！？；])", para)
            tmp = ""
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if len(tmp) + len(sent) <= child_size:
                    tmp += sent
                else:
                    if tmp:
                        chunks.append([tmp])
                    if len(sent) <= child_size:
                        tmp = sent
                    else:
                        start = 0
                        while start < len(sent):
                            end = min(start + child_size, len(sent))
                            chunks.append([sent[start:end]])
                            if end >= len(sent):
                                break
                            start = end
                        tmp = ""
            if tmp:
                chunks.append([tmp])
            continue

        add_len = len(para)
        if cur and cur_len + add_len > child_size:
            emit_cur()
            if child_overlap > 0 and chunks:
                # 从上一块末尾取完整段落作为重叠；不取字符尾巴
                overlap_paras: List[str] = []
                total = 0
                for old_para in reversed(chunks[-1]):
                    if old_para.startswith("#"):
                        continue
                    overlap_paras.insert(0, old_para)
                    total += len(old_para)
                    if total >= child_overlap:
                        break
                cur = overlap_paras[:]
                cur_len = sum(len(x) for x in cur)
        cur.append(para)
        cur_len += add_len

    emit_cur()

    texts = ["\n\n".join(chunk).strip() for chunk in chunks]

    # 合并过短 child。OCR 文本常把“文化”切成“文 / 化”，
    # 如果短块单独保留，会影响向量识别；这里尽量并入前一块。
    min_child_size = max(160, int(child_size * 0.35))
    merged: List[str] = []
    for t in texts:
        t = normalize_paragraph_text(t)
        if not t:
            continue
        if merged and len(t) < min_child_size:
            # 不超过 child_size 的 1.4 倍时并入前一块；略长也可以接受，因为这是检索候选块。
            if len(merged[-1]) + len(t) <= int(child_size * 1.45):
                merged[-1] = (merged[-1].rstrip() + "\n\n" + t).strip()
            else:
                merged.append(t)
        else:
            merged.append(t)

    # 如果第一个块仍然过短，并且后面还有块，则并入后一块。
    if len(merged) >= 2 and len(merged[0]) < min_child_size:
        merged[1] = (merged[0].rstrip() + "\n\n" + merged[1]).strip()
        merged = merged[1:]

    # 去掉完全重复 child，同时保留顺序。
    seen = set()
    result = []
    for t in merged:
        if len(t.strip()) < 80:
            continue
        key = re.sub(r"\s+", "", t)
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
    return result


def make_children(parents: List[Dict], child_size: int, child_overlap: int) -> List[Dict]:
    children: List[Dict] = []
    for parent in parents:
        parts = split_parent_into_children(parent["text"], child_size, child_overlap)
        for idx, part in enumerate(parts):
            child_id = stable_id(f'{parent["parent_id"]}|{idx}|{part[:120]}', "c_")
            children.append({
                "child_id": child_id,
                "parent_id": parent["parent_id"],
                "book_title": parent.get("book_title", ""),
                "author": parent.get("author", ""),
                "publisher": parent.get("publisher", ""),
                "publication_year": parent.get("publication_year", ""),
                "source_path": parent.get("source_path", ""),
                "parent_index": parent.get("parent_index"),
                "child_index": idx,
                "heading_path": parent.get("heading_path", ""),
                "page_start": parent.get("page_start"),
                "page_end": parent.get("page_end"),
                "char_count": len(part),
                "text": part,
            })
    return children


def quality_warnings(parents: List[Dict], children: List[Dict]) -> List[str]:
    warnings: List[str] = []
    if not parents:
        warnings.append("没有生成 parent。")
    if not children:
        warnings.append("没有生成 child。")

    repeated_children = 0
    for c in children[:200]:
        text = c.get("text", "")
        half = len(text) // 2
        if half > 80 and text[:half] == text[half:half * 2]:
            repeated_children += 1
    if repeated_children:
        warnings.append(f"前 200 个 child 中发现 {repeated_children} 个疑似整段重复。")

    crossing = 0
    for p in parents[:200]:
        text = p.get("text", "")
        # 一个 parent 里如果出现多个二级标题，大概率跨标题
        if len(re.findall(r"(?m)^##\s+", text)) > 1:
            crossing += 1
    if crossing:
        warnings.append(f"前 200 个 parent 中有 {crossing} 个疑似跨二级标题。")

    return warnings


def write_jsonl(path: Path, rows: List[Dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_preview(path: Path, parents: List[Dict], children: List[Dict], n: int = 8):
    warnings = quality_warnings(parents, children)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Small-to-Big 切块预览\n\n")
        f.write(f"- parents: {len(parents)}\n")
        f.write(f"- children: {len(children)}\n\n")
        if warnings:
            f.write("## 质量警告\n\n")
            for w in warnings:
                f.write(f"- {w}\n")
            f.write("\n")
        else:
            f.write("## 质量警告\n\n- 未发现明显重复 child 或 parent 跨二级标题。\n\n")

        f.write("## Parents 预览\n\n")
        for p in parents[:n]:
            f.write(f"### {p['parent_id']}\n\n")
            f.write(f"- 书名：{p.get('book_title','')}\n")
            f.write(f"- 作者：{p.get('author','')}\n")
            f.write(f"- 标题路径：{p.get('heading_path','')}\n")
            f.write(f"- 页码：{p.get('page_start','')} - {p.get('page_end','')}\n")
            f.write(f"- 字数：{p.get('char_count','')}\n\n")
            f.write(p["text"][:900] + "\n\n---\n\n")
        f.write("## Children 预览\n\n")
        for c in children[:n]:
            f.write(f"### {c['child_id']}\n\n")
            f.write(f"- parent_id：{c.get('parent_id','')}\n")
            f.write(f"- 标题路径：{c.get('heading_path','')}\n")
            f.write(f"- 页码：{c.get('page_start','')} - {c.get('page_end','')}\n")
            f.write(f"- 字数：{c.get('char_count','')}\n\n")
            f.write(c["text"][:700] + "\n\n---\n\n")


def collect_sources(source: Path) -> List[Path]:
    if source.is_file() and source.suffix.lower() == ".md":
        return [source]
    if source.is_dir():
        return sorted(source.rglob("*.md"))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="单个 MD 文件或 MD 文件夹")
    ap.add_argument("--output", required=True, help="输出目录")
    ap.add_argument("--parent-size", type=int, default=2200)
    ap.add_argument("--parent-overlap", type=int, default=0, help="建议先设为 0，避免 parent 跨标题重叠")
    ap.add_argument("--child-size", type=int, default=500)
    ap.add_argument("--child-overlap", type=int, default=0, help="建议先设为 0；需要召回率时可设 80-120")
    args = ap.parse_args()

    source = Path(args.source)
    output = Path(args.output)
    files = collect_sources(source)
    if not files:
        raise SystemExit(f"没有找到 MD 文件：{source}")

    all_parents: List[Dict] = []
    all_children: List[Dict] = []

    for file in files:
        raw = read_text(file)
        meta = parse_metadata(raw, file)
        paragraphs, _ = clean_and_collect_paragraphs(raw)
        parents = make_parents(paragraphs, meta, file, args.parent_size, args.parent_overlap)
        children = make_children(parents, args.child_size, args.child_overlap)

        all_parents.extend(parents)
        all_children.extend(children)

        print(f"[OK] {file.name}: paragraphs={len(paragraphs)}, parents={len(parents)}, children={len(children)}")

    write_jsonl(output / "parents.jsonl", all_parents)
    write_jsonl(output / "children.jsonl", all_children)
    write_preview(output / "preview.md", all_parents, all_children)

    print("\n完成。")
    print(f"parents:  {output / 'parents.jsonl'}")
    print(f"children: {output / 'children.jsonl'}")
    print(f"preview:  {output / 'preview.md'}")


if __name__ == "__main__":
    main()
