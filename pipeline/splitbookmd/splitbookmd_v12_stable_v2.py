# -*- coding: utf-8 -*-
"""
批量拆分 Markdown 书籍脚本 v12：稳定版框架

核心原则：
1. 不再单纯相信 OCR 里的 Markdown # 号。
2. 先判断目录质量和文档类型，再选择分割策略。
3. 优先保证正文不丢失；结构不可靠时自动降级为粗分。
4. 输出质量预检报告，方便后续用一致性检查脚本复核。
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re
import shutil
import unicodedata
import argparse
import hashlib

# =========================
# CONFIG
# =========================
SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR
OUTPUT_ROOT = SCRIPT_DIR / "output"
OVERWRITE = True
RECURSIVE = False
RENAME_SOURCE_MD_ON_SPLIT = False
ASSUME_YES = True
SKIP_METADATA_EDIT = True

WRITE_CLEAN_FULL_MD = True
FULL_CLEAN_FILENAME = "00_整本书_整理清洗版.md"
FULL_CLEAN_OUTPUT_DIR = OUTPUT_ROOT

CONVERT_PAGE_MARKERS_ON_OUTPUT = True
ADD_PAGE_ANCHORS = False
PAGE_MARKER_OUTPUT_TEMPLATE = "%% 原书页码：{page} %%"

REMOVE_LINKS_AND_WATERMARKS = True

USE_CURATED_METADATA_BLOCK = True
CURATED_START_MARKERS = ["<!-- AI整理信息开始 -->", "<!-- 元数据整理开始 -->", "<!-- 整理信息开始 -->"]
CURATED_END_MARKERS = ["<!-- AI整理信息结束 -->", "<!-- 元数据整理结束 -->", "<!-- 整理信息结束 -->"]
METADATA_REVIEW_LIST_FILENAME = "00_元数据核对清单.md"

SPLIT_GRANULARITY = "section"  # section / chapter
CHAPTER_INTRO_MODE = "separate"  # separate / merge_to_first_section / skip_if_short
# 默认不把正文中的“一、……”“（一）……”这类中文编号分条当作拆分标题。
# 若某些无目录文档确实需要按“一、……”拆分，可临时改为 True。
ALLOW_LOOSE_CN_ENUM_HEADINGS = False
# 回退到 Markdown 标题拆分时，默认只使用较高层级标题，避免把正文内部“一、二、三”或三级小标题误拆。
FALLBACK_MARKDOWN_MAX_LEVEL = 1
# 目录匹配成功后补充标题时也保持保守，避免把文章内部小层次补成独立文件。
SUPPLEMENT_MARKDOWN_MAX_LEVEL = 1
TOC_LOCATION = "auto"  # auto / tail / head
WRITE_SPLIT_MANIFEST = True
WRITE_MATCH_REPORT = True
RAG_CHUNK_LONG_SECTIONS = True
MAX_RAG_CHUNK_CHARS = 12000
MIN_RAG_CHUNK_CHARS = 4000

MIN_TOC_ENTRIES_TO_USE = 5
MATCH_THRESHOLD = 0.72
PAGE_WINDOW = 4
BACK_MATTER_SCAN_CHARS = 120000
METADATA_SCAN_CHARS = 40000
SKIP_GENERATED_OR_HELPER_MD = True
DEFAULT_AUTHOR = ""

# v12 稳定版新增：自动判断文档类型与保守降级
# A类：目录完整且有页码；B类：目录较完整但页码多为“待核对”；
# C类：目录缺失或异常但 Markdown 标题较可信；D类：结构极差，自动粗分。
AUTO_COARSE_FALLBACK = True
COARSE_CHUNK_CHARS = 18000
MIN_TOC_MATCH_RATIO = 0.35
MIN_SAFE_SPLIT_POINTS = 2
WRITE_QUALITY_REPORT = True
# 当目录不可用时，Markdown 回退只作为保守方案；若候选标题太少或明显异常，则粗分。
MAX_REASONABLE_POINTS_PER_100K = 35
# v12.1：正文抽取后自检阈值。若抽取正文显著短于原始正文，说明误删目录/后记/正文，自动改用保守粗分。
MIN_BODY_EXTRACTION_RATIO = 0.82
MIN_OUTPUT_EMIT_RATIO = 0.82

MANUAL_METADATA_BY_FILENAME = {
    # "书名.pdf_by_PaddleOCR-VL-1.5.md": {
    #     "book_title": "书名",
    #     "author": "作者",
    #     "publisher": "出版社",
    #     "publication_date": "年份",
    #     "edition": "版次",
    #     "isbn": "ISBN",
    # },
}

CN_NUM = "一二三四五六七八九十百千万零〇两"
STRUCTURAL_LEVELS = {"part", "chapter", "section_group", "section", "prelim", "reference"}

@dataclass
class TocEntry:
    order: int
    title: str
    page: Optional[int]
    level: str
    raw: str = ""

@dataclass
class CandidateHeading:
    offset: int
    line: str
    title: str
    level: str
    page_before: Optional[int]
    page_after: Optional[int]
    source: str

@dataclass
class MatchPoint:
    order: int
    title: str
    page: Optional[int]
    level: str
    offset: int
    score: float
    matched_line: str
    source: str

def safe_filename(name: str, max_len: int = 90) -> str:
    name = unicodedata.normalize("NFKC", name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "未命名")[:max_len]

TOC_HEADING_NAMES = {"目录", "目次", "目 录", "日录", "日 录", "Contents", "CONTENTS"}

def strip_toc_page_suffix(title: str) -> str:
    s = unicodedata.normalize("NFKC", title or "").strip()
    s = re.sub(r"\s*(?:[.·。]{2,}|…{1,}|\.{2,})\s*[（(]?\s*\d{1,4}(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]?\s*$", "", s)
    s = re.sub(r"\s*[（(]\s*\d{1,4}(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" .。·…")

def is_toc_heading_title(title: str) -> bool:
    n = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title or "").strip())
    return n in {"目录", "目次", "日录", "Contents".lower()} or n.lower() == "contents"

def looks_like_toc_entry_line(line: str) -> bool:
    s = unicodedata.normalize("NFKC", line or "").strip()
    if not s:
        return False
    s = re.sub(r"^#{1,6}\s*", "", s).strip()
    s = re.sub(r"^[-*+]\s+", "", s).strip()
    s = re.sub(r"^\d+[.)、]\s+", "", s).strip()
    return bool(
        re.search(r"(?:[.·。]{2,}|…{1,}|\.{2,})\s*[（(]?\s*\d{1,4}(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]?\s*$", s)
        or re.search(r"[（(]\s*\d{1,4}(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]\s*$", s)
        or re.search(r"\s*[\/／]\s*\d{1,4}(?:\s*[-—–至]\s*\d{1,4})?\s*$", s)
    )

def looks_like_toc_only_block(block: str) -> bool:
    lines = [ln.strip() for ln in (block or "").splitlines() if ln.strip()]
    if not lines:
        return True
    if len(lines) <= 4 and all(looks_like_toc_entry_line(ln) or is_toc_heading_title(clean_heading_line(ln)) for ln in lines):
        return True
    return len(lines) == 1 and looks_like_toc_entry_line(lines[0])

def clean_output_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "").strip()
    title = strip_toc_page_suffix(title)
    title = re.sub(r"\s*[\(（]\s*\d{1,4}\s*[\)）](?=\s*(?:[\(（](?:编引言|章引言)[\)）])?\s*$)", "", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip(" .")

def infer_book_title(md_path: Path) -> str:
    stem = md_path.stem
    stem = re.sub(r"\.pdf_by_.*$", "", stem, flags=re.I)
    stem = re.sub(r"_by_.*$", "", stem, flags=re.I)
    stem = re.sub(r"\.pdf$", "", stem, flags=re.I)
    # 清理文件名中的描述性后缀（非书名内容）
    # 例如 "艾思奇全書_的《辩证唯物主义历史唯物主义》。意思是叫我多学点..."
    stem = re.sub(r"_[的是为].*$", "", stem)
    stem = re.sub(r"[。！？，,].*$", "", stem)
    return stem.strip() or md_path.stem

def yaml_quote(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')

def yaml_list(items: List[str]) -> str:
    vals = [yaml_quote(x) for x in items if x]
    return "[" + ", ".join(f'"{x}"' for x in vals) + "]"

def as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []

def yaml_meta_value(v) -> str:
    vals = as_list(v) if isinstance(v, list) else []
    if isinstance(v, list):
        if len(vals) == 0:
            return '""'
        if len(vals) == 1:
            return f'"{yaml_quote(vals[0])}"'
        return yaml_list(vals)
    return f'"{yaml_quote(str(v or ""))}"'

def display_meta_value(v) -> str:
    vals = as_list(v) if isinstance(v, list) else []
    if isinstance(v, list):
        return "、".join(vals) if vals else "待核对"
    return str(v).strip() if str(v or "").strip() else "待核对"

PAGE_MARKER_PATTERN = r"【原书页码标记：(\d{1,4})】"


def normalize_page_markers(text: str) -> str:
    """将独立行上的页码数字转换为标准页码标记。

    过滤非页码数字：
    - 印刷批次号（如 2630/13）
    - 位于代码块/表格中的数字
    - 不在合理页码范围（1-2500）的数字
    """
    lines = text.splitlines()
    out: List[str] = []
    in_code_block = False
    in_table = False

    for line in lines:
        stripped = line.strip()

        # 跟踪代码块和表格状态
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            out.append(line)
            continue
        if in_code_block:
            out.append(line)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            out.append(line)
            continue
        if in_table and not (stripped.startswith("|") and stripped.endswith("|")):
            in_table = False

        # 检查是否为独立数字行
        m = re.fullmatch(r"(\d{1,4})", stripped)
        if m:
            page_num = int(m.group(1))
            # 页码范围检查：书籍页码通常在 1-2500 之间
            if 1 <= page_num <= 2500:
                out.append(f"【原书页码标记：{page_num}】")
                continue
            # 超出合理范围，保留原样
            out.append(line)
            continue

        # 清理印刷批次号行（如 2630/13）
        if re.fullmatch(r"\d{3,6}/\d{1,3}", stripped):
            continue

        out.append(line)

    return "\n".join(out)

def detect_page_range(text: str) -> str:
    nums = [int(x) for x in re.findall(PAGE_MARKER_PATTERN, text)]
    nums += [int(x) for x in re.findall(r"%%\s*原书页码：\s*(\d{1,4})\s*%%", text)]
    return f"{min(nums)}-{max(nums)}" if nums else "未检测到"

def strip_links_and_watermarks(text: str) -> str:
    if not REMOVE_LINKS_AND_WATERMARKS:
        return text

    # 先删除 OCR 产生的 HTML 图片块，再处理普通 URL。
    # 如果先删除 URL，<img src="https://..."> 会被破坏成残缺 HTML，反而不容易被后续规则匹配。
    html_image_block_patterns = [
        # PaddleOCR 常见形式：<div style="text-align: center;"><img ... /></div>
        r'(?is)<div\b[^>]*>\s*(?:<[^>]+>\s*)*<img\b[^>]*?(?:/?>)\s*(?:</[^>]+>\s*)*</div>',
        r'(?is)<p\b[^>]*>\s*(?:<[^>]+>\s*)*<img\b[^>]*?(?:/?>)\s*(?:</[^>]+>\s*)*</p>',
        r'(?is)<figure\b[^>]*>.*?<img\b[^>]*?(?:/?>).*?</figure>',
        # 专门兜底清理百度 BCE / PaddleOCR 图片链接所在容器
        r'(?is)<div\b[^>]*>.*?(?:pplines-online\.bj\.bcebos\.com|deploy/official/paddleocr).*?</div>',
    ]
    for pat in html_image_block_patterns:
        text = re.sub(pat, "", text)

    # 删除独立 HTML 图片、Markdown 图片、base64 图片
    text = re.sub(r'(?is)<img\b[^>]*?(?:/?>)', '', text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+', '', text, flags=re.I)

    # 删除普通超链接，保留链接文本
    text = re.sub(r"\[([^\]]+)\]\((?:https?|ftp|file|attachment):[^)]*\)", r"\1", text, flags=re.I)
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)

    # 删除无内容的 HTML 容器和常见样式标签
    text = re.sub(r"</?(?:span|font)\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r'(?is)<div\b[^>]*>\s*</div>', '', text)
    text = re.sub(r'(?is)<p\b[^>]*>\s*</p>', '', text)

    # 删除裸 URL。放在 HTML 图片清理之后，避免破坏 <img> 块匹配。
    text = re.sub(r"https?://\S+", "", text, flags=re.I)
    text = re.sub(r"www\.[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", "", text, flags=re.I)

    # 兜底删除残留的 PaddleOCR 图片行
    text = re.sub(r"(?im)^.*(?:pplines-online\.bj\.bcebos\.com|deploy/official/paddleocr|img_in_image_box).*$", "", text)

    watermark_patterns = [
        r"(?im)^\s*.*(?:z-library|z-lib|1lib|singlelogin|bookzz|booksc|downloaded\s+from).*$",
        r"(?im)^\s*.*(?:本书由|电子书|资源来自|更多电子书).*(?:整理|制作|下载|分享).*$",
    ]
    for pat in watermark_patterns:
        text = re.sub(pat, "", text)
    return text

def clean_latex_ocr_artifacts(text: str) -> str:
    # 脚注号：$ ^{①} $ → 〔①〕
    text = re.sub(r"\$\s*\^\{([^{}]{1,4})\}\s*\$", r"〔\1〕", text)
    greek = {"\\Phi": "Φ", "\\Pi": "Π", "\\Gamma": "Γ"}
    for k, v in greek.items():
        text = re.sub(r"\$\s*" + re.escape(k) + r"\s*\$", v, text)

    def clean_underset_math(m: re.Match) -> str:
        inner = m.group(1)
        inner = re.sub(r"\\underset{\\cdot}\{([^{}]+)\}", r"\1", inner)
        inner = re.sub(r"\s+", "", inner)
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-zΑ-ω]+", inner):
            return inner
        return m.group(0)

    text = re.sub(r"\$\s*([^$]*\\underset[^$]*)\s*\$", clean_underset_math, text)

    # 修复连续 \underset{\cdot}{字} 堆叠
    def fix_underset_stack(match):
        chars = re.findall(r"\\underset{\\cdot}\{([^{}]+)\}", match.group(0))
        if chars:
            return "".join(chars)
        return match.group(0)

    text = re.sub(r"(?:\\underset{\\cdot}\{[^{}]+\})+", fix_underset_stack, text)
    # 清理任何残余的单个 \underset{\cdot}{X}
    text = re.sub(r"\\underset{\\cdot}\{([^{}]+)\}", r"\1", text)

    # 清理 $ ^{*} $, $ * $, $ \cdot $, 空 $ $
    text = re.sub(r"\$\s*\^\{\*\}\s*\$", "", text)
    text = re.sub(r"\$\s*\*\s*\$", "", text)
    text = re.sub(r"\$\s*\\cdot\s*\$", "", text)
    text = re.sub(r"\$\s*\$", "", text)
    text = re.sub(r"\^\{\*?\}", "", text)
    text = re.sub(r"\^\{[^\}]{1,4}\}", "", text)

    return text


def clean_ocr_pinyin_noise(text: str) -> str:
    """清除 OCR 产生的拼音/罗马化噪声行和乱码。

    常见模式：
    - MAKESI ZHU V  ZHE XUES SHI  马克思主义 北京出版社哲学史
    - Makesi zhuyi zhexueshi (Diliujuan)
    - 2630/13（印刷批次号）
    - 孤立的单字母行如 R（OCR 碎片）
    """
    lines = text.splitlines()
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue

        # 删除纯拼音行：以拉丁字母为主，夹杂少量中文的 OCR 乱码标题行
        latin_chars = len(re.findall(r"[a-zA-Z]", stripped))
        cjk_chars = len(re.findall(r"[一-鿿]", stripped))
        total_alnum = latin_chars + cjk_chars
        if total_alnum > 0 and latin_chars > total_alnum * 0.45 and cjk_chars < 15:
            # 行中有大量拉丁字母但中文很少 → 很可能是拼音噪声
            # 例如 "MAKESI ZHU V  ZHE XUES SHI  马克思主义"
            # 但如果中文占比也高（如中英混排正文），则保留
            if cjk_chars < 6:
                continue  # 删除此行

        # 删除含拼音括号注释的行，如 "马克思主义哲学史（第六卷）Makesi zhuyi zhexueshi (Diliujuan)"
        # 这种是版权页的拼音标注，不是正文
        if re.search(r"[a-z]{4,}\s+[a-z]{4,}", stripped, re.I):
            # 但保留包含中文较多的正文行
            if cjk_chars < 20 and latin_chars > 20:
                continue

        # 混合中拼音行：中文 + 3个以上拉丁"单词" + 更多中文 → OCR拼音噪声
        # 如 "马克思主义哲学史（第六卷）Makesi zhuyi zhexueshi (Diliujuan)主编..."
        latin_words = re.findall(r"[a-zA-Z]{3,}", stripped)
        if cjk_chars > 5 and len(latin_words) >= 3:
            continue

        # 删除孤立的 1-2 个大写字母行（OCR 碎片如 "R"）
        if re.fullmatch(r"[A-Z]{1,2}", stripped):
            continue

        # 删除印刷批次号行，如 "2630/13"
        if re.fullmatch(r"\d{3,6}/\d{1,3}", stripped):
            continue

        # 删除 OCR 行尾乱码 =#
        stripped = re.sub(r"=#+\s*$", "", stripped)
        if stripped != line.strip():
            line = stripped

        cleaned.append(line)

    result = "\n".join(cleaned)
    # 删除产生的连续空行
    result = re.sub(r"\n{4,}", "\n\n\n", result)
    return result


def convert_page_markers_for_output(text: str) -> str:
    if not CONVERT_PAGE_MARKERS_ON_OUTPUT:
        return text
    def repl(m: re.Match) -> str:
        page = m.group(1)
        marker = PAGE_MARKER_OUTPUT_TEMPLATE.format(page=page)
        if ADD_PAGE_ANCHORS:
            return f'<span id="page-{page}"></span>\n{marker}'
        return marker
    return re.sub(r"(?m)^\s*" + PAGE_MARKER_PATTERN + r"\s*$", repl, text)

def clean_text_for_output(text: str) -> str:
    text = strip_links_and_watermarks(text)
    text = clean_latex_ocr_artifacts(text)
    text = clean_ocr_pinyin_noise(text)
    text = convert_page_markers_for_output(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip() + "\n"

def normalize_title_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    # 去掉 OCR/Markdown 中常见的脚注号和强调符号，避免“题名 $^{*}$”无法匹配目录题名。
    s = re.sub(r"\$\s*\^\{[^{}]{1,8}\}\s*\$", "", s)
    s = re.sub(r"\$\s*\*\s*\$", "", s)
    s = re.sub(r"\^\{[^}]{1,8}\}", "", s)
    s = re.sub(r"〔[^〕]{1,8}〕", "", s)
    # 目录题名常带写作时间，正文标题常不带；匹配时不把年份作为核心题名。
    s = re.sub(r"[（(]\s*(?:19|20)\d{2}(?:\.\d{1,2})?(?:\s*[—\-–至]\s*(?:19|20)?\d{2}(?:\.\d{1,2})?)?\s*[）)]", "", s)
    s = re.sub(r"第\s*([一二三四五六七八九十百千万零〇两\d]+)\s*([章节编部])", r"第\1\2", s)
    return re.sub(r"[#*_`~\[\]（）()\s　:：;；,，.。!！?？、·《》〈〉“”\"'‘’—\-–_\/／]+", "", s)

def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title_for_match(a), normalize_title_for_match(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        # 目录题名常常比正文标题短，如“第七卷代引言”对应
        # “第七卷代引言——在《胡绳全书》座谈会上的发言”。
        # 对足够长的包含关系应视作强匹配，而不是按长度比降到很低。
        if min(len(na), len(nb)) >= 5:
            return max(0.86, ratio)
        return ratio
    return SequenceMatcher(None, na, nb).ratio()

def clean_heading_line(line: str) -> str:
    line = unicodedata.normalize("NFKC", line or "").strip()
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"【原书页码标记：\d{1,4}】", "", line)
    return re.sub(r"\s+", " ", line).strip()

def is_generated_md(path: Path) -> bool:
    if not SKIP_GENERATED_OR_HELPER_MD:
        return False
    n = path.name
    return bool(n.startswith(("00_", "00A_", "00B_")) or re.match(r"^\d{2}_", n) or n.startswith("batch_split_"))

def split_off_back_matter(text: str) -> Tuple[str, str, str]:
    # 原有基于目录的切割
    if len(text) < 2000:
        return text, "", "none"
    start = max(0, len(text) - BACK_MATTER_SCAN_CHARS)
    tail = text[start:]
    pats = [
        r"(?m)^#{1,6}\s*(?:目录|目\s*录|目次|日\s*录)\s*$",
        r"(?m)^(?:目录|目\s*录|目次|日\s*录)\s*$",
        r"(?m)^#{1,6}\s*(?:作者简介|著者简介|译者简介|内容简介|版权信息|版权页|图书在版编目|CIP)\s*$",
        r"(?m)^(?:作者简介|著者简介|译者简介|内容简介|版权信息|版权页|图书在版编目|CIP)\s*$",
        r"(?m)^图书在版编目.*$",
        r"(?m)^中国版本图书馆CIP.*$",
    ]
    cuts: List[int] = []
    for p in pats:
        for m in re.finditer(p, tail, flags=re.I):
            cuts.append(start + m.start())
    if cuts:
        cut = min(cuts)
        return text[:cut].rstrip(), text[cut:].strip(), "back_matter"
    # v12.1 修正：不能仅凭“ISBN/定价/出版发行”等词在尾部出现就截断。
    # 很多正文会讨论“出版发行”“ISBN”等，旧规则会把后半本书误删。
    # 只有上面的明确目录/版权页标题才触发后置材料切分。
    return text, "", "none"

DISPLAY_METADATA_FIELDS = [
    "book_title",
    "author",
    "editor",
    "translator",
    "document_type",
    "institution",
    "publisher",
    "publication_place",
    "publication_year",
    "volume",
    "edition",
    "isbn",
    "metadata_source",
]

def empty_metadata() -> Dict[str, object]:
    return {
        "book_title": "",
        "author": [],
        "editor": [],
        "translator": [],
        "document_type": "book",
        "institution": "",
        "publisher": "",
        "publication_place": "",
        "publication_year": "",
        "volume": "",
        "edition": "",
        "series": "",
        "isbn": "",
        "metadata_verified": False,
        "metadata_source": "auto_detected",
        "manual_metadata_raw": "",
    }

def split_people(s: str) -> List[str]:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"(总主编|本卷主编|主编|编著|著|编)$", "", s)
    s = re.sub(r"(参加编写者|编委).*$", "", s)
    parts = re.split(r"[、,，;；\s/／]+", s)
    return [p.strip() for p in parts if p.strip()]

def infer_place_from_publisher(publisher: str) -> str:
    m = re.match(r"^(北京|上海|天津|重庆|南京|桂林|广州|武汉|长沙|杭州|济南|成都|西安|长春|沈阳|哈尔滨|郑州|福州|昆明|兰州|南昌|合肥|太原|呼和浩特|乌鲁木齐|拉萨|银川|西宁|海口)", publisher or "")
    return m.group(1) if m else ""

def extract_named_people_after_label(text: str, label: str) -> List[str]:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    label = re.sub(r"\s+", "", label)
    m = re.search(label + r"(.+?)(?:本卷主编|主编编委|参加编写者|北京出版社|出版社|出版|ISBN|第[一二三四五六七八九十百千万零〇两\d]+卷|$)", compact)
    if not m:
        return []
    names = m.group(1).strip()
    known = [
        "黄楠森", "庄福龄", "林利", "宋一秀", "宋秀", "孙克信", "苏厚重",
        "张念丰", "余源培", "孙伯铁", "余共铨", "吴仕康", "易克信",
        "施德福", "徐琳", "高齐云", "商英伟", "曾盛", "靳辉明",
    ]
    found: List[str] = []
    for name in known:
        if name in names and name not in found:
            found.append(name)
    return found or split_people(names)

def merge_people(existing, new) -> List[str]:
    out: List[str] = []
    for item in as_list(existing) + as_list(new):
        if item and item not in out:
            out.append(item)
    return out

def set_if_empty(meta: Dict[str, object], key: str, value) -> None:
    if value in (None, "", []):
        return
    if key in {"author", "editor", "translator"}:
        meta[key] = merge_people(meta.get(key), value)
    elif not meta.get(key):
        meta[key] = str(value).strip()


def finalize_metadata(meta: Dict[str, object]) -> Dict[str, object]:
    # 先从书名中拆出卷册，避免“书名里有卷册 + volume 字段也有卷册”导致重复。
    title = meta.get("book_title")
    if isinstance(title, list):
        title = max(title, key=len) if title else ""
    title = str(title or "").strip()
    if title:
        base_title, title_volume = split_title_and_volume(title)
        meta["book_title"] = base_title
        if title_volume and not meta.get("volume"):
            meta["volume"] = title_volume

    if meta.get("volume"):
        meta["volume"] = normalize_volume_text(meta.get("volume"))

    # 作者去重
    authors = as_list(meta.get("author"))
    if authors:
        seen = set()
        unique_authors = []
        for a in authors:
            if a not in seen:
                seen.add(a)
                unique_authors.append(a)
        meta["author"] = unique_authors[:2]

    editors = as_list(meta.get("editor"))
    if editors:
        seen = set()
        unique_editors = []
        for e in editors:
            if e not in seen:
                seen.add(e)
                unique_editors.append(e)
        meta["editor"] = unique_editors[:2]

    if not as_list(meta.get("author")) and editors:
        meta["author"] = editors[:2]
    return meta


def first_people_text(v, max_people: int = 2) -> str:
    return "、".join(as_list(v)[:max_people])


CN_NUM_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_NUM_CHARS = "零一二三四五六七八九"


def cn_number_to_int(s: str) -> Optional[int]:
    """
    把中文数字或阿拉伯数字转为整数。
    支持：一、二、十、十一、二十、二十一、三十六、100以内常见卷册数。
    """
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        left, right = s.split("十", 1)
        tens = CN_NUM_DIGITS.get(left, 1) if left else 1
        ones = CN_NUM_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in s:
        if ch not in CN_NUM_DIGITS:
            return None
        total = total * 10 + CN_NUM_DIGITS[ch]
    return total if total > 0 else None


def int_to_cn_number(n: int) -> str:
    """把 1-99 的整数转为中文数字，用于卷册显示。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return CN_NUM_CHARS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN_NUM_CHARS[n % 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return CN_NUM_CHARS[tens] + "十" + (CN_NUM_CHARS[ones] if ones else "")
    return str(n)


def normalize_volume_text(v) -> str:
    """
    统一卷册字段，固定使用阿拉伯数字显示：
    第一卷 -> 第1卷
    第七卷 -> 第7卷
    第1卷 -> 第1卷
    第一册 -> 第1册
    上册/中册/下册保持不变。
    """
    vals = as_list(v)
    s = vals[0] if vals else str(v or "")
    s = unicodedata.normalize("NFKC", s or "").strip()
    if not s or s in {"无", "空", "待核对", "None", "none", "-"}:
        return ""
    try:
        s = normalize_metadata_cell_value("volume", s)
    except NameError:
        s = re.sub(r"\s+", "", s)
    s = s.replace("（", "(").replace("）", ")")
    s = s.strip("()[]【】 ")

    m = re.fullmatch(r"第\s*([一二三四五六七八九十百千万零〇两\d]+)\s*([卷册部])", s)
    if m:
        num = cn_number_to_int(m.group(1))
        unit = m.group(2)
        if num is not None:
            return f"第{num}{unit}"

    m = re.fullmatch(r"([一二三四五六七八九十百千万零〇两\d]+)\s*([卷册部])", s)
    if m:
        num = cn_number_to_int(m.group(1))
        unit = m.group(2)
        if num is not None:
            return f"第{num}{unit}"

    m = re.fullmatch(r"([上下中])\s*册", s)
    if m:
        return f"{m.group(1)}册"

    return s


def normalize_volume_in_text(text: str) -> str:
    """把标题里的 第一卷、第七卷 等统一为阿拉伯数字卷册格式。"""
    text = unicodedata.normalize("NFKC", str(text or "")).strip()

    def repl(m: re.Match) -> str:
        return normalize_volume_text(f"第{m.group(1)}{m.group(2)}") or m.group(0)

    return re.sub(
        r"第\s*([一二三四五六七八九十百千万零〇两\d]+)\s*([卷册部])",
        repl,
        text,
    )


def split_title_and_volume(title: str) -> Tuple[str, str]:
    """
    从书名中拆出卷册：
    胡绳全书(第一卷) -> 胡绳全书, 第1卷
    胡绳全书.第7卷 -> 胡绳全书, 第7卷
    马克思主义哲学史（第七卷） -> 马克思主义哲学史, 第7卷
    """
    title = normalize_volume_in_text(title)
    title = title.replace("（", "(").replace("）", ")").strip()

    m = re.match(r"^(.*?)\s*\(\s*((?:第)?[一二三四五六七八九十百千万零〇两\d]+[卷册部]|[上下中]册)\s*\)\s*$", title)
    if m:
        return m.group(1).strip(" .。·_-"), normalize_volume_text(m.group(2))

    m = re.match(r"^(.*?)\s*[\.．·_\-— ]+\s*((?:第)?[一二三四五六七八九十百千万零〇两\d]+[卷册部])\s*$", title)
    if m:
        return m.group(1).strip(" .。·_-"), normalize_volume_text(m.group(2))

    return title.strip(), ""


def metadata_display_title(meta: Dict[str, object], fallback: str = "") -> str:
    """
    用于文件名、输出文件夹、整本清洗版标题的显示书名：
    艾思奇全书(第1卷)
    胡绳全书(第7卷)
    """
    raw_title = str(meta.get("book_title") or fallback or "未命名").strip()
    base_title, title_volume = split_title_and_volume(raw_title)
    volume = normalize_volume_text(meta.get("volume")) or title_volume
    if volume:
        return f"{base_title}({volume})"
    return normalize_volume_in_text(base_title)


def book_title_with_volume(meta: Dict[str, object], fallback: str = "") -> str:
    """兼容旧调用，等同于 metadata_display_title。"""
    return metadata_display_title(meta, fallback)


def metadata_biblio_string(meta: Dict[str, object], fallback: str = "") -> str:
    """
    生成核对清单里的书目信息：
    作者：《书名》（第1卷），出版地：出版社，年份年
    """
    author = first_people_text(meta.get("author")) or first_people_text(meta.get("editor")) or "待核对"
    display_title = metadata_display_title(meta, fallback)
    base_title, title_volume = split_title_and_volume(display_title)
    volume = normalize_volume_text(meta.get("volume")) or title_volume
    publication_place = display_meta_value(meta.get("publication_place"))
    publisher = display_meta_value(meta.get("publisher"))
    publication_year = display_meta_value(meta.get("publication_year"))
    title_part = f"《{base_title}》（{volume}）" if volume else f"《{base_title}》"
    return f"{author}：{title_part}，{publication_place}：{publisher}，{publication_year}年"


def metadata_filename_stem(meta: Dict[str, object], fallback: str = "") -> str:
    title = clean_output_title(metadata_display_title(meta, fallback))
    people = first_people_text(meta.get("author")) or first_people_text(meta.get("editor"))
    parts = [p for p in (title, people) if p]
    return safe_filename("_".join(parts), max_len=140)

def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{i:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重名文件名：{path}")

def rename_source_markdown(md_path: Path, meta: Dict[str, object]) -> Path:
    if not RENAME_SOURCE_MD_ON_SPLIT:
        return md_path

    # 自动识别且缺少关键字段时，不改原 md 文件名
    missing = metadata_missing_fields(meta)
    if meta.get("metadata_source") == "auto_detected" and missing:
        return md_path

    target = md_path.with_name(metadata_filename_stem(meta, md_path.stem) + md_path.suffix)
    if target == md_path:
        return md_path

    target = unique_path(target)
    md_path.rename(target)
    return target

def clean_auto_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "").strip()
    title = re.sub(r"^#+\s*", "", title)
    if re.search(r"[\u4e00-\u9fff]", title):
        title = re.sub(r"[A-Za-z]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .。:：")
    return title

def looks_like_series_title(title: str) -> bool:
    return bool(re.search(r"(文库|丛书|全集|选集|系列|书系|Collection\s+of)", title or "", re.I))

def normalize_cover_title(title: str) -> str:
    title = clean_auto_title(title)
    title = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", title)
    title = re.sub(r"\s*[:：]\s*", "：", title)
    return title.strip()

def extract_cover_title_author(text: str) -> Tuple[str, str]:
    head = unicodedata.normalize("NFKC", text[:12000] or "")
    head = re.split(r"图书在版编目|CIP|出版发行|版权所有", head, maxsplit=1)[0]
    lines = [x.strip() for x in head.splitlines() if x.strip()]
    title_candidates: List[str] = []
    author = ""

    for line in lines[:160]:
        clean = normalize_cover_title(clean_heading_line(line))
        if not author:
            m = re.match(r"^([一-龥·]{2,12})\s*著$", clean)
            if m:
                author = m.group(1)
        if re.match(r"^#{1,3}\s+", line):
            if (
                4 <= len(clean) <= 60
                and not looks_like_series_title(clean)
                and not re.search(r"(总序|编委会|出版社|ISBN|CIP|目录|Contents|作者简介|版权)", clean, re.I)
            ):
                title_candidates.append(clean)

    return (title_candidates[-1] if title_candidates else ""), author

def extract_auto_metadata(full_text: str, inferred: str, cand: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    meta = empty_metadata()
    text = unicodedata.normalize("NFKC", full_text or "")
    # 原头部和尾部扫描
    scan = "\n\n".join([text[:METADATA_SCAN_CHARS], text[-METADATA_SCAN_CHARS:]])
    scan = re.sub(r"(?m)^\s*\d{1,4}\s*$", "", scan)
    lines = [x.strip() for x in scan.splitlines() if x.strip()]
    cover_title, cover_author = extract_cover_title_author(text)
    if cover_title:
        meta["book_title"] = cover_title
    if cover_author:
        meta["author"] = [cover_author]

    # 原有 ISBN 和 CIP 提取（从扫描区）
    isbn = ""
    if cand:
        isbn = cand.get("candidate_isbn", "")
    if not isbn or "未自动识别" in isbn:
        m = re.search(r"ISBN\s*[:：]?\s*([0-9Xx\-—– ]{10,30})", scan, re.I)
        isbn = m.group(1).strip() if m else ""
    set_if_empty(meta, "isbn", isbn if "未自动识别" not in isbn else "")

    cip = re.search(
        r"(?m)^\s*(?P<title>[^/\n]{2,80}?)\s*/\s*(?P<resp>[^．。\n]{1,80})[．.。]\s*[一\-—–]?\s*(?P<place>[^：:\n]{1,30})\s*[:：]\s*(?P<publisher>[^，,\n]{2,60})\s*[，,]\s*(?P<year>\d{4})",
        scan,
    )
    if cip:
        cip_title = clean_auto_title(cip.group("title"))
        if looks_like_series_title(cip_title):
            set_if_empty(meta, "series", cip_title)
        else:
            set_if_empty(meta, "book_title", cip_title)

        resp = cip.group("resp").strip()
        if re.search(r"总主编|主编|\b编$", resp):
            set_if_empty(meta, "editor", split_people(resp))
        elif resp.endswith("著"):
            set_if_empty(meta, "author", split_people(resp))
        else:
            set_if_empty(meta, "author", split_people(resp))

        set_if_empty(meta, "publication_place", cip.group("place").strip())
        set_if_empty(meta, "publisher", cip.group("publisher").strip())
        set_if_empty(meta, "publication_year", cip.group("year").strip())

    # 原有其他元数据提取（从扫描区）
    for line in lines[:120]:
        if re.match(r"^#{1,3}\s*", line):
            title = clean_auto_title(line)
            if re.search(r"[\u4e00-\u9fff]", title) and not re.search(r"内容提要|目录|编者的话|前言|序言|Copyright|ISBN|出版社", title, re.I):
                title = re.sub(r"\s+", "", title) if len(title) <= 25 else title
                set_if_empty(meta, "book_title", title)
                vm = re.search(r"(第[一二三四五六七八九十百千万零〇两\d]+卷)", title)
                if vm:
                    set_if_empty(meta, "volume", vm.group(1))
                break

    for line in lines[:180]:
        if re.search(r"主编编委|参加编写者", line):
            continue
        if not meta.get("volume"):
            vm = re.fullmatch(r"(第[一二三四五六七八九十百千万零〇两\d]+[卷册部]|[上下中]册)", line)
            if vm:
                set_if_empty(meta, "volume", vm.group(1))
        editor_line = re.match(r"^#?\s*(?:总主编|主编|本卷主编)\s*[/／：:]?\s*(.+)$", line)
        inline_editor = re.match(r"^#?\s*([一-龥·、,，\s]{2,40})(?:总主编|主编|本卷主编)\s*$", line)
        if editor_line:
            set_if_empty(meta, "editor", split_people(editor_line.group(1)))
        elif inline_editor:
            set_if_empty(meta, "editor", split_people(inline_editor.group(1)))
        m = re.match(r"^(?:作者|著者)\s+(.+)$", line)
        if m:
            set_if_empty(meta, "author", split_people(m.group(1)))
        m = re.match(r"^#?\s*([一-龥·]{2,12})\s*著\s*$", line)
        if m:
            set_if_empty(meta, "author", [m.group(1)])

    pub_line = ""
    for line in lines:
        if "出版发行" in line:
            pub_line = line
            m = re.search(r"出版发行\s+(.+)$", line)
            if m:
                set_if_empty(meta, "publisher", m.group(1).strip())
            break
        m = re.search(r"([^，,。\s]{2,40}出版社)出版", line)
        if m:
            pub_line = line
            set_if_empty(meta, "publisher", m.group(1).strip())
            break
    if pub_line and not meta.get("publication_place"):
        pm = re.search(r"[（(]([^）)]+)[）)]", pub_line)
        if pm:
            addr = pm.group(1)
            for known_place in ("北京", "上海", "天津", "重庆", "南京", "桂林", "广州", "武汉", "长沙", "杭州", "济南", "成都", "西安"):
                if addr.startswith(known_place):
                    set_if_empty(meta, "publication_place", known_place)
                    break
            place = re.match(r"([^省市区县]{1,10}[市省区县])", addr)
            if place:
                set_if_empty(meta, "publication_place", place.group(1))
    if meta.get("publisher") and not meta.get("publication_place"):
        set_if_empty(meta, "publication_place", infer_place_from_publisher(str(meta.get("publisher"))))

    if not meta.get("publisher"):
        for line in lines[:220]:
            m = re.fullmatch(r"#?\s*([^#\s，,。]{2,40}出版社)\s*", line)
            if m:
                set_if_empty(meta, "publisher", m.group(1))
                set_if_empty(meta, "publication_place", infer_place_from_publisher(m.group(1)))
                break

    simple_year = re.search(r"(?m)(\d{4})\s*年\s*[·.・]?\s*([\u4e00-\u9fff]{1,10})", scan)
    if simple_year:
        set_if_empty(meta, "publication_year", simple_year.group(1))
        if not meta.get("publication_place"):
            set_if_empty(meta, "publication_place", simple_year.group(2))

    year_match = re.search(r"(?m)(\d{4})\s*年\s*\d{1,2}\s*月[^\n]{0,20}第\s*([一二三四五六七八九十\d]+)\s*版", scan)
    if year_match:
        set_if_empty(meta, "publication_year", year_match.group(1))
        set_if_empty(meta, "edition", f"第{year_match.group(2)}版")
        if not meta.get("publication_place"):
            around = year_match.group(0)
            pm = re.search(r"\d{1,2}\s*月\s*([^第\s]{1,12})\s*第", around)
            if pm:
                set_if_empty(meta, "publication_place", pm.group(1).strip())

    if not as_list(meta.get("author")) and not as_list(meta.get("editor")):
        m = re.search(r"作者简介[\s\S]{0,200}?([一-龥·]{2,10})", scan)
        if m:
            set_if_empty(meta, "author", [m.group(1)])
    if not meta.get("book_title"):
        meta["book_title"] = inferred

    # 新增：全文搜索 ISBN 和 CIP（不限于头尾）
    full_isbn = re.search(r"ISBN\s*[:：]?\s*([0-9Xx\-—–]{10,30})", full_text, re.I)
    if full_isbn:
        set_if_empty(meta, "isbn", full_isbn.group(1).strip())
    full_cip = re.search(r"图书在版编目.*?ISBN", full_text, re.I | re.S)
    if full_cip and not meta.get("publisher"):
        pub_match = re.search(r"出版发行[：:]\s*([^，,]{2,40}出版社)", full_cip.group(0))
        if pub_match:
            set_if_empty(meta, "publisher", pub_match.group(1))

    return finalize_metadata(meta)

def get_metadata(md_path: Path, inferred: str, cand: Optional[Dict[str, str]] = None, full_text: str = "") -> Dict[str, object]:
    meta = empty_metadata()
    auto = extract_auto_metadata(full_text, inferred, cand) if full_text else empty_metadata()
    meta.update(auto)
    manual = MANUAL_METADATA_BY_FILENAME.get(md_path.name) or MANUAL_METADATA_BY_FILENAME.get(md_path.stem) or {}
    for key in ("book_title", "document_type", "institution", "publisher", "publication_place", "publication_year", "volume", "edition", "series", "isbn"):
        if manual.get(key):
            meta[key] = manual.get(key)
    for key in ("author", "editor", "translator"):
        if manual.get(key):
            meta[key] = as_list(manual.get(key))
    if DEFAULT_AUTHOR and not as_list(meta.get("author")):
        meta["author"] = as_list(DEFAULT_AUTHOR)
    meta["metadata_source"] = "auto_detected"
    return postprocess_metadata(finalize_metadata(meta))

def parse_manual_metadata(raw_input: str) -> Dict[str, object]:
    raw_original = (raw_input or "").strip()
    raw = unicodedata.normalize("NFKC", raw_original).strip()
    meta = empty_metadata()
    meta["metadata_source"] = "manual_input"
    meta["manual_metadata_raw"] = raw_original

    bare_thesis = re.match(r"^\s*(?P<resp>.+?)\s*[:：]\s*(?P<institution>[^，,：:]+?)\s*(?P<degree>博士|硕士|学士)\s*论文\s*[，,]\s*(?P<year>\d{4})\s*年?\s*$", raw)
    if bare_thesis:
        resp = bare_thesis.group("resp").strip()
        meta["author"] = split_people(resp[:-1] if resp.endswith("著") else resp)
        meta["document_type"] = f'{bare_thesis.group("degree")}论文'
        meta["institution"] = bare_thesis.group("institution").strip()
        meta["publication_year"] = bare_thesis.group("year").strip()
        meta["book_title"] = f'{meta["institution"]}{meta["document_type"]}'
        return postprocess_metadata(finalize_metadata(meta))

    loose_thesis = re.match(r"^\s*(?P<resp>.+?)\s*[:：]\s*[《“\"「]?(?P<title>.+?)[》”\"」]?\s*[，,]\s*(?P<institution>[^，,：:]+?)\s*(?P<degree>博士|硕士|学士)\s*论文\s*[，,]?\s*(?P<year>\d{4})\s*年?[。.]?\s*$", raw)
    if loose_thesis:
        resp = loose_thesis.group("resp").strip()
        meta["author"] = split_people(resp[:-1] if resp.endswith("著") else resp)
        meta["book_title"] = re.sub(r"[《》“”\"「」]", "", loose_thesis.group("title")).strip()
        meta["document_type"] = f'{loose_thesis.group("degree")}论文'
        meta["institution"] = loose_thesis.group("institution").strip()
        meta["publication_year"] = loose_thesis.group("year").strip()
        return postprocess_metadata(finalize_metadata(meta))

    m = re.match(r"^\s*(?P<resp>.+?)\s*[:：]\s*《(?P<title>[^》]+)》(?P<rest>.*)$", raw)
    if not m:
        return meta

    resp = m.group("resp").strip()
    title = m.group("title").strip()
    rest = (m.group("rest") or "").strip()
    meta["book_title"] = title

    # 支持用户输入“作者：《书名》（第X卷），出版地：出版社，年份年”
    # 即卷册写在书名号后面的情况。
    volume_after_title = re.match(
        rf"^\s*[（(]?\s*(第[{CN_NUM}\d]+[卷册部]|[上下中]册|[{CN_NUM}\d]+册)\s*[）)]?",
        rest
    )
    if volume_after_title:
        meta["volume"] = normalize_volume_text(volume_after_title.group(1))
        rest = rest[volume_after_title.end():].strip(" ，,;；。")

    if resp.endswith("主编"):
        meta["editor"] = split_people(resp[:-2])
    elif resp.endswith("编"):
        meta["editor"] = split_people(resp[:-1])
    elif resp.endswith("著"):
        meta["author"] = split_people(resp[:-1])
    else:
        meta["author"] = split_people(resp)

    thesis_match = re.search(r"(?:^|[，,])\s*(?P<institution>[^，,：:]+?)\s*(?P<degree>博士|硕士|学士)\s*论文\s*[，,]\s*(?P<year>\d{4})\s*年?", rest)
    if thesis_match:
        meta["document_type"] = f'{thesis_match.group("degree")}论文'
        meta["institution"] = thesis_match.group("institution").strip()
        meta["publication_year"] = thesis_match.group("year").strip()
        before_pub = rest[:thesis_match.start()].strip(" ，,")
    else:
        before_pub = rest

    pub_match = None if thesis_match else re.search(r"(?:^|[，,])\s*(?P<place>[^，,：:]+?)\s*[:：]\s*(?P<publisher>[^，,]+?)\s*[，,]\s*(?P<year>\d{4})\s*年?", rest)
    if pub_match:
        meta["publication_place"] = pub_match.group("place").strip()
        meta["publisher"] = pub_match.group("publisher").strip()
        meta["publication_year"] = pub_match.group("year").strip()
        before_pub = rest[:pub_match.start()].strip(" ，,")

    first_segment = re.split(r"[，,]", before_pub, maxsplit=1)[0].strip()
    vm = re.match(r"^(第[一二三四五六七八九十百千万零〇两\d]+[卷册部]|[上下中]册|[一二三四五六七八九十百千万零〇两\d]+册)", first_segment)
    if vm:
        meta["volume"] = vm.group(1).strip()
        before_pub = before_pub[vm.end():].strip(" ，,")

    translator_match = re.search(r"([^，,]+?)\s*译(?:[，,]|$)", before_pub)
    if translator_match:
        meta["translator"] = split_people(translator_match.group(1))

    isbn_match = re.search(r"ISBN\s*[:：]?\s*([0-9Xx\-—– ]{10,30})", raw, re.I)
    if isbn_match:
        meta["isbn"] = isbn_match.group(1).strip()
    return postprocess_metadata(finalize_metadata(meta))

def metadata_missing_fields(meta: Dict[str, object]) -> List[str]:
    missing = []
    if not meta.get("book_title"):
        missing.append("book_title")
    if not as_list(meta.get("author")) and not as_list(meta.get("editor")):
        missing.append("author/editor")
    if str(meta.get("document_type") or "") in {"博士论文", "硕士论文", "学士论文"}:
        if not meta.get("institution"):
            missing.append("institution")
        if not meta.get("publication_year"):
            missing.append("publication_year")
    else:
        for key in ("publisher", "publication_place", "publication_year"):
            if not meta.get(key):
                missing.append(key)
    return missing

def print_metadata(meta: Dict[str, object]) -> None:
    for key in DISPLAY_METADATA_FIELDS:
        print(f"{key}: {display_meta_value(meta.get(key))}")
    if meta.get("manual_metadata_raw"):
        print(f"manual_metadata_raw: {meta['manual_metadata_raw']}")

def prompt_yes_no(prompt: str) -> str:
    while True:
        ans = input(prompt).strip().lower()
        if ans in {"y", "n", "q", "quit"}:
            return ans
        print("请输入 y/n，或输入 q/quit 终止脚本。")

def prompt_manual_metadata() -> Dict[str, object]:
    while True:
        print("\n请输入书目信息，建议格式如下：\n")
        print("著者：《书名》，译者译，出版地：出版社，出版年份\n")
        print("示例：")
        print("张一兵：《回到马克思——经济学语境中的哲学话语》，南京：江苏人民出版社，1999年")
        print("卡尔·马克思、弗里德里希·恩格斯：《马克思恩格斯全集》第3卷，中共中央马克思恩格斯列宁斯大林著作编译局译，北京：人民出版社，1960年")
        print("胡绳：《童稚集》，北京：人民出版社，1994年")
        print("胡绳：《胡绳全书》第4卷，北京：人民出版社，1998年\n")
        print("张华：扬州大学博士论文，2011年\n")
        raw = input("请输入：").strip()
        if raw.lower() in {"q", "quit"}:
            raise SystemExit("用户终止脚本。")

        meta = parse_manual_metadata(raw)
        missing = metadata_missing_fields(meta)
        if missing:
            if not meta.get("book_title") and not as_list(meta.get("author")) and not as_list(meta.get("editor")):
                print("\n未能完整解析该书目信息，请检查格式，或按示例重新输入。")
                print("原始输入为：")
                print(raw)
                print("\n已解析出部分元数据：\n")
            else:
                print("\n已解析出部分元数据：\n")
            print_metadata(meta)
            print("\n未解析字段：" + "、".join(missing))
            ans = prompt_yes_no("\n是否接受？请输入 y/n：")
        else:
            print("\n已根据手动输入解析为：\n")
            print_metadata(meta)
            ans = prompt_yes_no("\n是否确认？请输入 y/n：")

        if ans == "y":
            meta["metadata_verified"] = True
            return meta
        if ans in {"q", "quit"}:
            raise SystemExit("用户终止脚本。")

def parse_review_number_selection(text: str, max_n: int) -> List[int]:
    """
    解析用户输入的编号。
    支持：
    10
    10,11
    10 11
    10-15
    10，11，15-18
    """
    text = unicodedata.normalize("NFKC", text or "").strip()
    if not text:
        return []

    nums: List[int] = []

    for part in re.split(r"[,\s，、]+", text):
        part = part.strip()
        if not part:
            continue

        m = re.fullmatch(r"(\d+)\s*[-~—–至]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            nums.extend(range(a, b + 1))
            continue

        if part.isdigit():
            nums.append(int(part))
            continue

        print(f"无法识别编号：{part}")

    cleaned: List[int] = []
    for n in nums:
        if 1 <= n <= max_n and n not in cleaned:
            cleaned.append(n)
        elif not (1 <= n <= max_n):
            print(f"编号超出范围，已忽略：{n}")

    return cleaned

def edit_metadata_items_interactively(
    items: List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]]
) -> List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]]:
    """
    在正式分割前，允许用户按编号修正元数据。
    """

    items = list(items)

    problem_nums = []
    for i, (_p, meta, _raw, entries, curated) in enumerate(items, 1):
        missing = metadata_missing_fields(meta)
        if missing or (not curated and len(entries) == 0):
            problem_nums.append(i)

    if problem_nums:
        print("\n建议优先核对以下编号：")
        print("  " + ", ".join(str(x) for x in problem_nums))
    else:
        print("\n未发现明显缺失字段。")

    while True:
        raw_input_text = input(
            "\n请输入要修正的编号，例如 10,11 或 10-15；输入 all 修正全部建议项；直接回车跳过："
        ).strip()

        if not raw_input_text:
            return items

        if raw_input_text.lower() in {"q", "quit"}:
            raise SystemExit("用户终止脚本。")

        if raw_input_text.lower() in {"all", "a", "*"}:
            nums = problem_nums
        else:
            nums = parse_review_number_selection(raw_input_text, len(items))

        if not nums:
            print("没有有效编号，请重新输入。")
            continue

        for n in nums:
            p, meta, raw_text, entries, curated = items[n - 1]

            print("\n" + "=" * 60)
            print(f"正在修正第 {n} 项：")
            print(metadata_review_line(n, p, meta, curated, entries))
            print("=" * 60)

            new_meta = prompt_manual_metadata()
            new_meta["metadata_verified"] = True
            new_meta["metadata_source"] = "manual_input"

            items[n - 1] = (
                p,
                postprocess_metadata(finalize_metadata(new_meta)),
                raw_text,
                entries,
                curated,
            )

            print("\n修正后的显示结果：")
            print(metadata_review_line(n, p, items[n - 1][1], curated, entries))

        ans = prompt_yes_no("\n是否继续修正其他编号？请输入 y/n：")
        if ans == "n":
            return items
        if ans in {"q", "quit"}:
            raise SystemExit("用户终止脚本。")

def confirm_metadata(meta: Dict[str, object]) -> Dict[str, object]:
    print("\n已识别到以下书籍级元数据：\n")
    print_metadata(meta)
    ans = prompt_yes_no("\n是否确认？请输入 y/n：")
    if ans == "y":
        meta["metadata_verified"] = True
        return meta
    if ans in {"q", "quit"}:
        raise SystemExit("用户终止脚本。")
    return prompt_manual_metadata()

def collect_context(text: str, keys: List[str], window: int = 4, max_blocks: int = 4) -> str:
    lines = text.splitlines()
    blocks, used = [], set()
    for i, line in enumerate(lines):
        if any(k.lower() in line.lower() for k in keys):
            a, b = max(0, i-window), min(len(lines), i+window+1)
            if (a, b) in used:
                continue
            used.add((a, b))
            block = "\n".join(lines[a:b]).strip()
            if block:
                blocks.append(block)
            if len(blocks) >= max_blocks:
                break
    return "\n\n---\n\n".join(blocks)

def metadata_candidates(full_text: str, back: str) -> Dict[str, str]:
    scan = (back or "") + "\n\n" + full_text[:METADATA_SCAN_CHARS] + "\n\n" + full_text[-METADATA_SCAN_CHARS:]
    m = re.search(r"ISBN\s*[:：]?\s*([0-9Xx\-—– ]{10,30})", scan, re.I)
    return {
        "candidate_isbn": m.group(1).strip() if m else "未自动识别到 ISBN 候选",
        "copyright_snippet": collect_context(scan, ["ISBN", "版权所有", "版权", "出版发行", "责任编辑", "版次", "出版社", "定价"], 5, 5) or "未自动识别到版权页片段",
        "cip_snippet": collect_context(scan, ["CIP", "图书在版编目", "数据核字"], 5, 3) or "未自动识别到 CIP 片段",
        "toc_snippet": collect_context(scan, ["目录", "目 录", "日 录", "目次"], 5, 3) or "未自动识别到目录片段",
        "author_snippet": collect_context(scan, ["作者简介", "著者简介", "作者", "著者"], 4, 3) or "未自动识别到作者信息片段",
    }

def reference_index(book_title: str, source_file: str, meta: Dict[str, str], cand: Dict[str, str], back: str) -> str:
    return (
        "---\n"
        f'title: "{yaml_quote(book_title)} 元数据索引"\n'
        f'book_title: "{yaml_quote(book_title)}"\n'
        f'source_file: "{yaml_quote(source_file)}"\n'
        'status: "metadata_index_auto_generated"\n'
        'rag_exclude: true\n'
        'reliability: "metadata_from_curated_or_confirmed_fields"\n'
        "---\n\n"
        "# 元数据索引\n\n"
        "> 本文件由脚本自动生成，仅用于核对当前写入各章节 YAML 的书籍级元数据。\n\n"
        "## 当前写入各章节 YAML 的字段\n\n"
        "| 字段 | 内容 |\n|---|---|\n"
        f'| 书名 | {display_meta_value(meta.get("book_title"))} |\n'
        f'| 显示书名 | {metadata_display_title(meta, book_title)} |\n'
        f'| 作者 | {display_meta_value(meta.get("author"))} |\n'
        f'| 编者 | {display_meta_value(meta.get("editor"))} |\n'
        f'| 译者 | {display_meta_value(meta.get("translator"))} |\n'
        f'| 文献类型 | {display_meta_value(meta.get("document_type"))} |\n'
        f'| 学位授予单位 | {display_meta_value(meta.get("institution"))} |\n'
        f'| 出版地 | {display_meta_value(meta.get("publication_place"))} |\n'
        f'| 出版社 | {display_meta_value(meta.get("publisher"))} |\n'
        f'| 出版年份 | {display_meta_value(meta.get("publication_year"))} |\n'
        f'| 卷册 | {display_meta_value(meta.get("volume"))} |\n'
        f'| 版次 | {display_meta_value(meta.get("edition"))} |\n'
        f'| 丛书 | {display_meta_value(meta.get("series"))} |\n'
        f'| ISBN | {display_meta_value(meta.get("isbn"))} |\n'
        f'| 已核验 | {display_meta_value(meta.get("metadata_verified"))} |\n'
        f'| 元数据来源 | {display_meta_value(meta.get("metadata_source"))} |\n'
        f'| 手动原始输入 | {display_meta_value(meta.get("manual_metadata_raw"))} |\n'
    )

def choose_toc(raw: str, back: str) -> Tuple[str, str]:
    blocks: List[Tuple[str, str]] = []
    if TOC_LOCATION in ("auto", "tail") and back:
        blocks.append(("back_matter", back))
    if TOC_LOCATION in ("auto", "tail"):
        blocks.append(("tail", raw[-BACK_MATTER_SCAN_CHARS:]))
    if TOC_LOCATION in ("auto", "head"):
        blocks.append(("head", raw[:BACK_MATTER_SCAN_CHARS]))
    for source, block in blocks:
        m = re.search(r"(?im)^\s*#{0,6}\s*(目录|目\s*录|目次|日\s*录)\s*$", block)
        if m:
            toc_block = block[m.start():]
            stop = re.search(rf"(?m)^\s*#{{1,6}}\s*(第\s*[{CN_NUM}\d]+\s*章|导论|绪论|引言|前言)\b", toc_block[m.end()-m.start():])
            if stop and stop.start() > 300:
                toc_block = toc_block[:m.end()-m.start()+stop.start()]
            return toc_block, source
    return (back, "back_matter_no_toc_marker") if back else ("", "none")

def remove_toc_block_from_raw(raw: str, toc_text: str) -> str:
    if not toc_text or len(toc_text) < 300:
        return raw
    if not re.search(r"(?im)^\s*#{0,6}\s*(目录|目\s*录|目次|日\s*录)\s*$", toc_text):
        return raw
    # v12.1：目录块若异常过长，宁可不删，也不能把目录后面的正文一起删掉。
    if len(toc_text) > 60000 or len(toc_text) > max(30000, len(raw) * 0.25):
        return raw
    pos = raw.find(toc_text)
    if pos < 0:
        return raw
    return raw[:pos].rstrip() + "\n\n" + raw[pos + len(toc_text):].lstrip()

def preprocess_toc(t: str) -> str:
    t = unicodedata.normalize("NFKC", t or "").replace("\u3000", " ")
    t = re.sub(r"【原书页码标记：\d{1,4}】", "\n", t)
    t = re.sub(r"(?m)^\s*\d{1,4}\s*$", "\n", t)
    t = re.sub(r"[·.。]{3,}|…{2,}", " ", t)
    markers = [
        rf"第\s*[{CN_NUM}\d]+\s*[编部]",
        rf"第\s*[{CN_NUM}\d]+\s*章",
        rf"第\s*[{CN_NUM}\d]+\s*节",
        rf"[{CN_NUM}]\s*、",
        rf"[（(]\s*[{CN_NUM}]\s*[）)]",
        r"导论", r"绪论", r"引言", r"前言", r"后记", r"参考文献", r"参考书目", r"索引",
    ]
    for p in markers:
        t = re.sub(rf"(?<!^)(?<!\n)\s*({p})", r"\n\1", t)
    return re.sub(r"\n{2,}", "\n", t)

def merge_toc_continuation_lines(toc_text: str) -> str:
    lines = [x.strip() for x in (toc_text or "").splitlines() if x.strip()]
    merged: List[str] = []
    start_pat = rf"^(第\s*[{CN_NUM}\d]+\s*[编部章节]|[{CN_NUM}]\s*、|[（(]\s*[{CN_NUM}]\s*[）)]|总序|导论|绪论|引言|前言|后记|参考文献|参考书目|索引)"

    for line in lines:
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        if not line or is_toc_heading_title(line):
            continue
        if merged and not re.match(start_pat, line):
            merged[-1] = merged[-1].rstrip() + line
        else:
            merged.append(line)
    return "\n".join(merged)

def classify_title(title: str) -> str:
    raw = unicodedata.normalize("NFKC", title or "").strip()
    n = normalize_title_for_match(title)
    if re.match(rf"^第[{CN_NUM}\d]+[编部]", n): return "part"
    if re.match(rf"^第[{CN_NUM}\d]+章", n): return "chapter"
    if re.match(rf"^第[{CN_NUM}\d]+节", n): return "section"
    if re.match(rf"^[{CN_NUM}]{{1,3}}\s+\S+", raw) and not re.match(rf"^[{CN_NUM}]、", raw):
        return "section_group"
    if re.match(rf"^[{CN_NUM}]、", raw): return "section"
    if re.match(rf"^[（(]\s*[{CN_NUM}]\s*[）)]", raw): return "section"
    if n in {"总序", "导论", "绪论", "引言", "前言", "概论", "绪言", "后记"}: return "prelim"
    if n in {"参考文献", "参考书目", "索引"}: return "reference"
    return "other"

def markdown_heading_level(line: str) -> int:
    m = re.match(r"^\s*(#{1,6})\s+", line or "")
    return len(m.group(1)) if m else 0

def is_false_positive_enumeration(title: str) -> bool:
    s = clean_heading_line(title)
    return bool(re.match(rf"^(?:第?[{CN_NUM}]+|\d+)\s*[，,:：；;。]", s))

def is_loose_cn_enum_heading(title: str) -> bool:
    """识别容易和正文分条混淆的中文编号标题，如“一、……”“（一）……”。

    这类行在哲学、历史文献中常常只是作者列举观点，并非真正章节标题。
    因此默认只允许它通过目录精确匹配进入拆分，不在正文裸识别阶段主动拆分。
    """
    s = clean_heading_line(title)
    return bool(
        re.match(rf"^[{CN_NUM}]{{1,3}}\s*、", s)
        or re.match(rf"^[（(]\s*[{CN_NUM}]{{1,3}}\s*[）)]", s)
    )

def is_probable_split_candidate(c: CandidateHeading) -> bool:
    if is_toc_heading_title(c.title):
        return False
    if is_front_matter_noise_heading(c.title, ""):
        return False
    if c.source == "bare" and (looks_like_toc_entry_line(c.line) or looks_like_toc_entry_line(c.title)):
        return False
    if (not ALLOW_LOOSE_CN_ENUM_HEADINGS) and c.source in {"bare", "markdown"} and is_loose_cn_enum_heading(c.title):
        return False
    if len(c.title) > 140:
        return False
    if is_false_positive_enumeration(c.title):
        return False
    if c.source == "bare" and (len(c.title) > 80 or re.search(r"[，,。；;]", c.title)):
        return False
    if c.level in STRUCTURAL_LEVELS:
        return True
    return c.source == "markdown" and markdown_heading_level(c.line) <= 2 and len(c.title) <= 80

def parse_toc(toc_text: str) -> List[TocEntry]:
    text = preprocess_toc(toc_text)
    text = merge_toc_continuation_lines(text)
    text = unicodedata.normalize("NFKC", text or "").replace("\u3000", " ")
    text = re.sub(r"【原书页码标记：\d{1,4}】", "\n", text)
    text = re.sub(r"(?m)^\s*\d{1,4}\s*$", "\n", text)

    entries: List[TocEntry] = []

    for raw_line in text.splitlines():
        raw = raw_line.rstrip()
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        line = re.sub(r"^[-*+]\s+", "", line).strip()
        line = re.sub(r"^\d+[.)、]\s+", "", line).strip()
        if not line or is_toc_heading_title(line):
            continue
        if re.fullmatch(r"\|?\s*项目\s*\|.*", line) or re.fullmatch(r"\|?\s*-+\s*\|.*", line):
            continue

        page = None
        title = line

        slash_page = re.search(r"\s*[\/／]\s*(\d{1,4})(?:\s*[-—–至]\s*\d{1,4})?\s*$", title)
        if slash_page:
            page = int(slash_page.group(1))
            title = title[:slash_page.start()].strip()
        else:
            m = re.search(r"\s*(?:[.·。]{2,}|…{1,}|\.{2,})\s*[（(]?\s*(\d{1,4})(?:\s*[-—–至]\s*(\d{1,4}))?\s*[）)]?\s*$", title)
            if m:
                page = int(m.group(1))
                title = title[:m.start()].strip()
            else:
                matches = list(re.finditer(r"[（(]\s*(\d{1,4})(?:\s*[-—–至]\s*(\d{1,4}))?\s*[）)]", title))
                if matches:
                    m2 = matches[-1]
                    page = int(m2.group(1))
                    title = (title[:m2.start()] + title[m2.end():]).strip()
                else:
                    m3 = re.search(r"\s+(\d{1,4})(?:\s*[-—–至]\s*\d{1,4})?\s*$", title)
                    if m3 and (
                        re.match(rf"^(第\s*[{CN_NUM}\d]+\s*[编部章节]|[{CN_NUM}]\s*、|[（(]\s*[{CN_NUM}]\s*[）)]|总序|导论|绪论|引言|前言|后记)", title)
                    ):
                        page = int(m3.group(1))
                        title = title[:m3.start()].strip()

        title = strip_toc_page_suffix(title)
        title = re.sub(r"[·.。…]{2,}", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -—–·.。;；")
        if not title:
            continue

        level = classify_title(title)
        if SPLIT_GRANULARITY == "chapter" and level == "section":
            continue

        entries.append(TocEntry(len(entries) + 1, title, page, level, raw))

    return entries

def toc_is_corrupted(entries: List[TocEntry]) -> Tuple[bool, str]:
    if not entries:
        return True, "未解析出目录条目"
    if len(entries) < MIN_TOC_ENTRIES_TO_USE:
        return True, f"目录条目过少：{len(entries)}"
    for e in entries[:20]:
        n = normalize_title_for_match(e.title)
        if len(e.title) > 180:
            return True, "目录存在超长粘连条目"
        if len(re.findall(rf"第[{CN_NUM}\d]+章", n)) >= 2:
            return True, "目录条目中粘连了多个章节"
    return False, "目录基本可靠"

def line_offsets(text: str) -> List[Tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(True):
        out.append((pos, line.rstrip("\r\n")))
        pos += len(line)
    return out

def nearest_page_before(text: str, offset: int) -> Optional[int]:
    nums = re.findall(r"【原书页码标记：(\d{1,4})】", text[:offset][-9000:])
    return int(nums[-1]) if nums else None

def nearest_page_after(text: str, offset: int) -> Optional[int]:
    m = re.search(r"【原书页码标记：(\d{1,4})】", text[offset:offset+9000])
    return int(m.group(1)) if m else None

def is_bare_chapter_or_part(title: str) -> bool:
    n = normalize_title_for_match(title)
    return bool(re.fullmatch(rf"第[{CN_NUM}\d]+章", n) or re.fullmatch(rf"第[{CN_NUM}\d]+[编部]", n))

def looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s: return False
    c = clean_heading_line(s)
    n = normalize_title_for_match(c)
    return bool(
        s.startswith("#")
        or re.match(rf"^第\s*[{CN_NUM}\d]+\s*[编部章节]", c)
        or (ALLOW_LOOSE_CN_ENUM_HEADINGS and re.match(rf"^[{CN_NUM}]\s*、", c))
        or n in {"导论", "绪论", "引言", "前言", "后记", "参考文献", "参考书目", "索引"}
    )

def collect_candidate_headings(text: str) -> List[CandidateHeading]:
    offs = line_offsets(text)
    cands: List[CandidateHeading] = []
    in_toc_block = False

    for i, (offset, line) in enumerate(offs):
        stripped = line.strip()
        markdown_level = markdown_heading_level(stripped)

        if markdown_level:
            htitle = clean_heading_line(stripped)
            if is_toc_heading_title(htitle):
                in_toc_block = True
                continue
            if in_toc_block:
                in_toc_block = False

        if in_toc_block:
            continue

        if not looks_like_heading(line):
            continue

        title = clean_heading_line(line)
        if not title:
            continue

        source = "markdown" if stripped.startswith("#") else "bare"

        if source == "bare" and looks_like_toc_entry_line(line):
            continue
        if is_toc_heading_title(title):
            continue

        if is_bare_chapter_or_part(title):
            for _, next_line in offs[i+1:i+4]:
                nxt = clean_heading_line(next_line)
                if not nxt:
                    continue
                if len(nxt) <= 100 and not re.match(rf"^[{CN_NUM}]\s*、", nxt) and not looks_like_toc_entry_line(nxt):
                    title = f"{title} {nxt}"
                break

        title = clean_output_title(title)
        level = classify_title(title)
        if SPLIT_GRANULARITY == "chapter" and level == "section":
            continue

        cand = CandidateHeading(offset, stripped, title, level, nearest_page_before(text, offset), nearest_page_after(text, offset), source)
        if not is_probable_split_candidate(cand):
            continue
        cands.append(cand)

    dedup: Dict[int, CandidateHeading] = {}
    for c in cands:
        if c.offset not in dedup or len(c.title) > len(dedup[c.offset].title):
            dedup[c.offset] = c
    return sorted(dedup.values(), key=lambda x: x.offset)

def page_distance(page: Optional[int], c: CandidateHeading) -> int:
    if page is None:
        return 9999
    pages = [p for p in (c.page_before, c.page_after) if p is not None]
    return min(abs(page-p) for p in pages) if pages else 9999

def levels_compatible(entry_level: str, candidate_level: str, candidate_source: str) -> bool:
    if candidate_source == "char":
        return True
    if entry_level in STRUCTURAL_LEVELS:
        return candidate_level == entry_level
    return True

def char_candidates(entry: TocEntry, text: str, after_offset: int) -> List[CandidateHeading]:
    """
    按目录标题在正文中补找候选标题。
    修正点：旧逻辑对每个目录项扫描所有短行并做 SequenceMatcher，
    在《艾思奇全书》第2卷这类长文档中会非常慢；同时对 AI 目录中的
    个别错字（如“亦友变敌” vs 正文“亦友亦敌”）过于严格。
    现在先做廉价候选过滤，再在页码接近时允许较低相似度。
    """
    out: List[CandidateHeading] = []
    entry_norm = normalize_title_for_match(entry.title)
    if not entry_norm:
        return out
    offs = line_offsets(text)

    for offset, line in offs:
        if offset <= after_offset:
            continue
        raw = line.strip()
        if not raw or len(raw) > 160:
            continue
        if looks_like_toc_entry_line(raw):
            continue

        title = clean_output_title(clean_heading_line(raw))
        if not title or is_toc_heading_title(title):
            continue
        title_norm = normalize_title_for_match(title)
        if not title_norm:
            continue

        # 快速排除大多数正文短句，避免在长文档中对每行做昂贵相似度计算。
        raw_is_heading_like = raw.startswith("#") or looks_like_heading(raw)
        if not raw_is_heading_like:
            if len(title_norm) > max(80, len(entry_norm) + 25):
                continue
            if entry_norm not in title_norm and title_norm not in entry_norm:
                # 容忍一两个字的 OCR/AI 目录差异，但至少前两个字或后两个字要有重合。
                if len(entry_norm) >= 3 and len(title_norm) >= 3:
                    if entry_norm[:2] != title_norm[:2] and entry_norm[-2:] != title_norm[-2:]:
                        continue
                else:
                    continue

        sim = title_similarity(entry.title, title)
        title_level = classify_title(title)
        min_score = MATCH_THRESHOLD + 0.08
        if entry.level in STRUCTURAL_LEVELS and title_level == "other":
            min_score = 0.82

        nb = nearest_page_before(text, offset)
        na = nearest_page_after(text, offset)
        temp_c = CandidateHeading(offset, raw, title, entry.level if title_level == "other" else title_level, nb, na, "char")
        dist = page_distance(entry.page, temp_c)

        # 页码接近时，对短标题允许一定错字差异。
        if entry.page is not None and dist != 9999 and dist <= PAGE_WINDOW:
            min_score = min(min_score, 0.68 if len(entry_norm) <= 12 else 0.72)

        if sim >= min_score:
            out.append(temp_c)
            if len(out) >= 8:
                break
    return out

def collect_char_candidate_pool(text: str) -> List[CandidateHeading]:
    """
    一次性收集可作为目录匹配目标的正文行，避免每个目录项都全篇扫描。
    包括 Markdown 标题、明确章/节标题，以及短而无句末标点的题名式行。
    """
    out: List[CandidateHeading] = []
    for offset, line in line_offsets(text):
        raw = line.strip()
        if not raw or len(raw) > 160:
            continue
        if looks_like_toc_entry_line(raw):
            continue
        title = clean_output_title(clean_heading_line(raw))
        if not title or is_toc_heading_title(title):
            continue
        title_norm = normalize_title_for_match(title)
        if len(title_norm) < 2 or title_norm.isdigit():
            continue
        heading_like = raw.startswith("#") or looks_like_heading(raw)
        title_like_short = len(title_norm) <= 80 and not re.search(r"[。！？；;]$", raw)
        if not (heading_like or title_like_short):
            continue
        title_level = classify_title(title)
        out.append(CandidateHeading(offset, raw, title, title_level, nearest_page_before(text, offset), nearest_page_after(text, offset), "char"))
    # 去重
    dedup: Dict[int, CandidateHeading] = {}
    for c in out:
        if c.offset not in dedup or len(c.title) > len(dedup[c.offset].title):
            dedup[c.offset] = c
    return sorted(dedup.values(), key=lambda x: x.offset)


def cheap_title_plausible(entry_norm: str, cand_norm: str) -> bool:
    if not entry_norm or not cand_norm:
        return False
    if entry_norm in cand_norm or cand_norm in entry_norm:
        return True
    if len(entry_norm) <= 12 and len(cand_norm) <= 20:
        return entry_norm[:2] == cand_norm[:2] or entry_norm[-2:] == cand_norm[-2:]
    # 长题名只要有连续 4 个字命中即可进入精算。
    for i in range(0, max(1, len(entry_norm) - 3)):
        if entry_norm[i:i+4] in cand_norm:
            return True
    return False


def match_toc_to_body(entries: List[TocEntry], text: str) -> Tuple[List[MatchPoint], List[TocEntry], List[CandidateHeading]]:
    base = collect_candidate_headings(text)
    char_pool = collect_char_candidate_pool(text)

    # 合并候选池。base 用于报告样例；full_pool 用于真正匹配。
    by_offset: Dict[int, CandidateHeading] = {}
    for c in char_pool + base:
        # 优先保留 markdown/bare 明确候选，其次 char 候选。
        if c.offset not in by_offset or by_offset[c.offset].source == "char":
            by_offset[c.offset] = c
    full_pool = sorted(by_offset.values(), key=lambda x: x.offset)

    matches: List[MatchPoint] = []
    unmatched: List[TocEntry] = []
    last = -1

    for e in entries:
        entry_norm = normalize_title_for_match(e.title)
        if e.level == "other" and e.page is None and len(entry_norm) < 4:
            unmatched.append(e)
            continue

        best, best_score = None, 0.0
        for c in full_pool:
            if c.offset <= last:
                continue
            if not levels_compatible(e.level, c.level, c.source):
                continue
            cand_norm = normalize_title_for_match(c.title)
            if not cheap_title_plausible(entry_norm, cand_norm):
                continue

            sim = title_similarity(e.title, c.title)
            dist = page_distance(e.page, c)

            min_score = MATCH_THRESHOLD
            if e.level in STRUCTURAL_LEVELS and c.level == "other":
                min_score = 0.82
            if e.page is not None and dist != 9999 and dist <= PAGE_WINDOW:
                min_score = min(min_score, 0.68 if len(entry_norm) <= 12 else 0.72)
            if sim < min_score:
                continue
            # 页码锚点可能因原书前后目录、插页或 OCR 重编号而错位；
            # 题名高度相似时，不因页码距离过大而丢掉匹配。
            if e.page is not None and dist != 9999 and dist > 12 and sim < 0.85:
                continue

            score = sim + (0.10 if c.source == "markdown" else 0.05 if c.source == "bare" else -0.02)
            if e.page is not None and dist != 9999:
                score += 0.08 if dist <= PAGE_WINDOW else -0.05
            if score > best_score:
                best, best_score = c, score

        if best is None:
            unmatched.append(e)
            continue

        matches.append(MatchPoint(e.order, best.title, e.page, e.level, best.offset, min(best_score, 1.0), best.line, best.source))
        last = best.offset

    cleaned, used = [], set()
    for m in sorted(matches, key=lambda x: (x.offset, -x.score)):
        if m.offset in used:
            continue
        used.add(m.offset)
        cleaned.append(m)
    return sorted(cleaned, key=lambda x: x.offset), unmatched, base

def markdown_split_points(text: str) -> List[MatchPoint]:
    points = []
    for c in collect_candidate_headings(text):
        ml = markdown_heading_level(c.line)
        # 回退拆分必须保守：只认较高层级 Markdown 标题，不把正文内部三级小标题和“一 二 三”层次拆成文件。
        if c.source == "markdown" and ml and ml > FALLBACK_MARKDOWN_MAX_LEVEL:
            continue
        if c.level == "section_group":
            continue
        if c.source != "markdown" and len(c.title) > 100:
            continue
        points.append(MatchPoint(len(points) + 1, c.title, c.page_after or c.page_before, c.level, c.offset, 0.0, c.line, c.source))
    return points

def supplement_body_heading_points(points: List[MatchPoint], candidates: List[CandidateHeading]) -> List[MatchPoint]:
    used_offsets = {p.offset for p in points}
    out = list(points)
    order = max((p.order for p in points), default=0)
    for c in candidates:
        if c.offset in used_offsets or not is_probable_split_candidate(c):
            continue
        # 补充拆分点只使用 Markdown 顶层标题；裸文本候选和内部小标题很容易造成误拆。
        if c.source != "markdown":
            continue
        ml = markdown_heading_level(c.line)
        if ml and ml > SUPPLEMENT_MARKDOWN_MAX_LEVEL:
            continue
        if c.level == "section_group":
            continue
        cn = normalize_title_for_match(c.title)
        if any(
            abs(c.offset - p.offset) < 500
            and (
                title_similarity(c.title, p.title) >= 0.92
                or (
                    cn
                    and (pn := normalize_title_for_match(p.title))
                    and (cn in pn or pn in cn)
                    and min(len(cn), len(pn)) / max(len(cn), len(pn)) >= 0.55
                )
            )
            for p in out
        ):
            continue
        if SPLIT_GRANULARITY == "chapter" and c.level == "section":
            continue
        order += 1
        out.append(MatchPoint(order, c.title, c.page_after or c.page_before, c.level, c.offset, 0.0, c.line, f"{c.source}_supplement"))
        used_offsets.add(c.offset)
    return sorted(out, key=lambda x: x.offset)

def clean_body_len(text: str, title: str) -> int:
    t = re.sub(r"(?m)^#{1,6}\s*", "", text)
    t = t.replace(title, "", 1)
    t = re.sub(r"【原书页码标记：\d{1,4}】", "", t)
    t = re.sub(r"\s+", "", t)
    return len(t)

def effective_body_len(text: str, heading_title: str) -> int:
    t = re.sub(rf"^{re.escape(heading_title)}\s*\n", "", text, flags=re.M)
    t = re.sub(r"【原书页码标记：\d+】", "", t)
    t = re.sub(r"%%\s*原书页码：\s*\d+\s*%%", "", t)
    lines = [ln for ln in t.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return sum(len(ln.strip()) for ln in lines)

def is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|")

def markdown_blocks(text: str) -> List[str]:
    blocks: List[str] = []
    cur: List[str] = []
    in_code = False
    in_table = False

    for line in text.splitlines():
        stripped = line.strip()
        starts_code = stripped.startswith("```") or stripped.startswith("~~~")
        table_line = is_table_line(line)

        if starts_code:
            cur.append(line)
            in_code = not in_code
            continue

        if in_code:
            cur.append(line)
            continue

        if table_line:
            if cur and not in_table:
                if any(x.strip() for x in cur):
                    blocks.append("\n".join(cur).strip())
                cur = []
            in_table = True
            cur.append(line)
            continue

        if in_table:
            blocks.append("\n".join(cur).strip())
            cur = []
            in_table = False

        if not stripped:
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
            continue

        cur.append(line)

    if cur:
        blocks.append("\n".join(cur).strip())
    return [b for b in blocks if b]

def split_long_text_block(block: str, max_chars: int) -> List[str]:
    if len(block) <= max_chars:
        return [block]
    pieces = re.split(r"(?<=[。！？.!?])\s*", block)
    chunks, cur = [], ""
    for piece in pieces:
        if not piece:
            continue
        if cur and len(cur) + len(piece) > max_chars:
            chunks.append(cur.strip())
            cur = piece
        else:
            cur = (cur + piece) if cur else piece
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [block]

def split_body_for_rag(body: str) -> List[str]:
    if not RAG_CHUNK_LONG_SECTIONS or len(body) <= MAX_RAG_CHUNK_CHARS:
        return [body]

    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for block in markdown_blocks(body):
        sub_blocks = split_long_text_block(block, MAX_RAG_CHUNK_CHARS) if len(block) > MAX_RAG_CHUNK_CHARS and not block.lstrip().startswith(("```", "~~~", "|")) else [block]
        for sub in sub_blocks:
            add_len = len(sub) + 2
            if cur and cur_len + add_len > MAX_RAG_CHUNK_CHARS and cur_len >= MIN_RAG_CHUNK_CHARS:
                chunks.append("\n\n".join(cur).strip())
                cur, cur_len = [], 0
            cur.append(sub)
            cur_len += add_len

    if cur:
        chunks.append("\n\n".join(cur).strip())
    return chunks or [body]

def heading_path(book: str, pp: str, pc: str, title: str) -> List[str]:
    path: List[str] = [book]
    for item in (pp, pc, title):
        item = clean_output_title(item)
        if item and item not in path:
            path.append(item)
    return path

def nearest_parent(entries: List[TocEntry], order: int, level: str) -> str:
    for e in reversed(entries):
        if e.order < order and e.level == level:
            return e.title
    return ""

def nearest_heading_parent(entries: List[TocEntry], order: int) -> str:
    return nearest_parent(entries, order, "chapter") or nearest_parent(entries, order, "section_group")

def generic_intro(title: str) -> bool:
    return normalize_title_for_match(title) in {"引言", "导论", "绪论", "前言", "概论", "绪言"}

def infer_output(mp: MatchPoint, next_mp: Optional[MatchPoint], entries: List[TocEntry], cur_part: str, cur_ch: str, body: str) -> Tuple[str, str, str, str, str, str]:
    if mp.level == "part":
        cur_part, cur_ch = mp.title, ""
        if next_mp and next_mp.level in {"chapter", "section_group", "section"} and clean_body_len(body, mp.title) >= 120:
            return cur_part, cur_ch, f"{mp.title}（编引言）", "part_intro", mp.title, ""
        return cur_part, cur_ch, mp.title, "part", "", ""
    if mp.level == "chapter":
        cur_ch = mp.title
        pp = cur_part or nearest_parent(entries, mp.order, "part")
        if next_mp and next_mp.level in {"section", "section_group", "other"} and clean_body_len(body, mp.title) >= 120:
            return cur_part, cur_ch, f"{mp.title}（章引言）", "chapter_intro", pp, mp.title
        return cur_part, cur_ch, mp.title, "chapter", pp, ""
    if mp.level == "section_group":
        cur_ch = mp.title
        pp = cur_part or nearest_parent(entries, mp.order, "part")
        return cur_part, cur_ch, mp.title, "section_group", pp, ""
    if mp.level == "prelim" and generic_intro(mp.title):
        pp = cur_part or nearest_parent(entries, mp.order, "part")
        pc = cur_ch or nearest_heading_parent(entries, mp.order)
        if pp:
            return cur_part, cur_ch, f"{pp}（编引言）", "part_intro", pp, ""
        if pc:
            return cur_part, cur_ch, f"{pc}（章引言）", "chapter_intro", pp, pc
    pp = cur_part or nearest_parent(entries, mp.order, "part")
    pc = cur_ch or nearest_heading_parent(entries, mp.order)
    return cur_part, cur_ch, mp.title, mp.level, pp, pc if mp.level == "section" else ""

def render_metadata_table(meta: Dict[str, object]) -> str:
    rows = [
        ("书名", meta.get("book_title")),
        ("显示书名", metadata_display_title(meta, "")),
        ("作者", meta.get("author")),
        ("编者", meta.get("editor")),
        ("译者", meta.get("translator")),
        ("文献类型", meta.get("document_type")),
        ("机构", meta.get("institution")),
        ("出版社", meta.get("publisher")),
        ("出版地", meta.get("publication_place")),
        ("出版年份", meta.get("publication_year")),
        ("卷册", meta.get("volume")),
        ("版次", meta.get("edition")),
        ("丛书", meta.get("series")),
        ("ISBN", meta.get("isbn")),
        ("元数据来源", meta.get("metadata_source")),
        ("元数据已核验", "是" if meta.get("metadata_verified") else "否"),
    ]
    lines = ["| 字段 | 内容 |", "|---|---|"]
    for key, value in rows:
        val = display_meta_value(value)
        val = val.replace("|", "\\|")
        lines.append(f"| {key} | {val} |")
    return "\n".join(lines) + "\n"

def render_toc_markdown(entries: List[TocEntry]) -> str:
    if not entries:
        return "> 未能自动解析目录，请核对原文目录。\n"
    lines: List[str] = []
    for e in entries:
        title = clean_output_title(e.title)
        page = f"……{e.page}" if e.page is not None else ""
        if re.match(rf"^[（(]\s*[{CN_NUM}]\s*[）)]", title):
            indent = "    "
        elif e.level == "section":
            indent = "  "
        elif e.level == "other":
            indent = "  "
        else:
            indent = ""
        lines.append(f"{indent}- {title}{page}")
    return "\n".join(lines) + "\n"

def find_main_body_start(points: List[MatchPoint], text: str) -> int:
    m = re.search(rf"(?m)^\s*#{{1,6}}\s*(第\s*[{CN_NUM}\d]+\s*[编部章]|导论|绪论|引言|前言)\b", text)
    if m:
        return m.start()
    for p in points:
        if p.level in {"part", "chapter"}:
            return p.offset
    m = re.search(rf"(?m)^\s*(第\s*[{CN_NUM}\d]+\s*[编部章]|导论|绪论|引言|前言)\b", text)
    return m.start() if m else (points[0].offset if points else 0)

def build_clean_full_markdown(book: str, source: str, meta: Dict[str, object], entries: List[TocEntry], body: str, method: str) -> str:
    body = clean_text_for_output(body)
    parts = [
        f"# {book}",
        "",
        "## 元数据",
        "",
        render_metadata_table(meta).rstrip(),
        "",
        "## 目录",
        "",
        render_toc_markdown(entries).rstrip(),
        "",
        "## 正文",
        "",
        body.rstrip(),
        "",
    ]
    return "\n".join(parts)

def build_yaml(title: str, book: str, source: str, idx: int, page_range: str, method: str, cls: str, pp: str, pc: str, meta: Dict[str, str], toc_page: str = "", score: str = "", match_source: str = "", path: Optional[List[str]] = None, chunk_index: int = 1, chunk_count: int = 1, parent_section_title: str = "") -> str:
    path = path or heading_path(book, pp, pc, title)
    return (
        "---\n"
        f'title: "{yaml_quote(title)}"\nbook_title: "{yaml_quote(book)}"\n'
        f"heading_path: {yaml_list(path)}\n"
        f'heading_path_text: "{yaml_quote(" > ".join(path))}"\n'
        f'author: {yaml_meta_value(meta.get("author"))}\n'
        f'editor: {yaml_meta_value(meta.get("editor"))}\n'
        f'translator: {yaml_meta_value(meta.get("translator"))}\n'
        f'document_type: "{yaml_quote(str(meta.get("document_type") or ""))}"\n'
        f'institution: "{yaml_quote(str(meta.get("institution") or ""))}"\n'
        f'publisher: "{yaml_quote(str(meta.get("publisher") or ""))}"\n'
        f'publication_place: "{yaml_quote(str(meta.get("publication_place") or ""))}"\n'
        f'publication_year: "{yaml_quote(str(meta.get("publication_year") or ""))}"\n'
        f'edition: "{yaml_quote(str(meta.get("edition") or ""))}"\n'
        f'volume: "{yaml_quote(str(meta.get("volume") or ""))}"\n'
        f'series: "{yaml_quote(str(meta.get("series") or ""))}"\n'
        f'isbn: "{yaml_quote(str(meta.get("isbn") or ""))}"\n'
        'source_type: "full_original_text"\n'
        'full_text_available: true\n'
        'quote_source: true\n'
        'quote_verified: false\n'
        f'year: "{yaml_quote(str(meta.get("publication_year") or ""))}"\n'
        f'page_range: "{yaml_quote(page_range)}"\n'
        f'metadata_verified: {str(bool(meta.get("metadata_verified"))).lower()}\n'
        f'metadata_source: "{yaml_quote(str(meta.get("metadata_source") or ""))}"\n'
        f'manual_metadata_raw: "{yaml_quote(str(meta.get("manual_metadata_raw") or ""))}"\n'
        f'source_file: "{yaml_quote(source)}"\nsplit_method: "{yaml_quote(method)}"\nsection_index: {idx}\n'
        f'parent_section_title: "{yaml_quote(parent_section_title or title)}"\nchunk_index: {chunk_index}\nchunk_count: {chunk_count}\n'
        f'heading_class: "{yaml_quote(cls)}"\nparent_part: "{yaml_quote(pp)}"\nparent_chapter: "{yaml_quote(pc)}"\n'
        f'toc_page: "{yaml_quote(toc_page)}"\nmatch_score: "{yaml_quote(score)}"\nmatch_source: "{yaml_quote(match_source)}"\n'
        f'detected_page_marker_range: "{yaml_quote(page_range)}"\n'
        'page_numbering: "沿用原文OCR页码标记，不从章节重新编号"\n'
        'reliability: "pdf_converted_by_ocr"\nstatus: "raw_split_with_page_anchors"\n'
        'notes: "目录、Markdown标题、正文字符匹配与页码锚点共同辅助拆分；书籍级元数据已由用户核验或手动确认。"\n'
        "---\n\n"
    )
def match_report(entries: List[TocEntry], corrupt: bool, reason: str, matches: List[MatchPoint], unmatched: List[TocEntry], strategy: str, toc_source: str, candidates: List[CandidateHeading]) -> str:
    lines = ["# 目录解析与匹配报告", "", f"- 实际策略：{strategy}", f"- 目录来源：{toc_source}", f"- 目录条目数：{len(entries)}", f"- 目录是否异常：{corrupt}", f"- 判断说明：{reason}", f"- 成功匹配数：{len(matches)}", f"- 未匹配数：{len(unmatched)}", "", "## 成功匹配条目", "", "| 序号 | 标题 | 目录页码 | 类型 | 匹配分数 | 来源 | 正文匹配行 |", "|---:|---|---:|---|---:|---|---|"]
    for m in matches:
        lines.append(f"| {m.order} | {m.title} | {m.page or ''} | {m.level} | {m.score:.3f} | {m.source} | {m.matched_line} |")
    lines += ["", "## 未匹配目录条目", "", "| 序号 | 目录标题 | 目录页码 | 类型 |", "|---:|---|---:|---|"]
    for e in unmatched:
        lines.append(f"| {e.order} | {e.title} | {e.page or ''} | {e.level} |")
    lines += ["", "## 正文候选标题样例", "", "| 序号 | 页码前 | 页码后 | 类型 | 来源 | 候选标题 | 原始行 |", "|---:|---:|---:|---|---|---|---|"]
    for i, c in enumerate(candidates[:220], 1):
        lines.append(f"| {i} | {c.page_before or ''} | {c.page_after or ''} | {c.level} | {c.source} | {c.title} | {c.line} |")
    return "\n".join(lines) + "\n"

# =========================
# CURATED METADATA WORKFLOW
# =========================

FIELD_MAP = {
    "书名": "book_title",
    "题名": "book_title",
    "标题": "book_title",
    "作者": "author",
    "著者": "author",
    "编者": "editor",
    "主编": "editor",
    "本卷主编": "editor",
    "分卷主编": "editor",
    "译者": "translator",
    "文献类型": "document_type",
    "类型": "document_type",
    "机构": "institution",
    "学位授予单位": "institution",
    "出版社": "publisher",
    "出版地": "publication_place",
    "出版地点": "publication_place",
    "出版年份": "publication_year",
    "出版年": "publication_year",
    "出版时间": "publication_year",
    "出版日期": "publication_year",
    "出版年月": "publication_year",
    "年份": "publication_year",
    "卷册": "volume",
    "卷次": "volume",
    "册次": "volume",
    "卷": "volume",
    "版次": "edition",
    "版本": "edition",
    "印次": "edition",
    "印刷": "edition",
    "丛书": "series",
    "丛书名": "series",
    "所属": "series",
    "ISBN": "isbn",
    "isbn": "isbn",
}

def markdown_table_rows(text: str) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells):
            continue
        if cells[0] in {"字段", "项目", "元数据项", "key", "Key"}:
            continue
        value = " | ".join(cells[1:]).strip()
        rows.append((cells[0], value))
    return rows

def extract_section(text: str, heading_names: List[str], stop_names: Optional[List[str]] = None) -> str:
    stop_names = stop_names or []
    names_pat = "|".join(re.escape(x) for x in heading_names)
    m = re.search(rf"(?im)^\s*#{{1,6}}\s*(?:{names_pat})\s*$", text)
    if not m:
        return ""
    start = m.end()
    stop_patterns = []
    for name in stop_names:
        stop_patterns.append(rf"^\s*#{{1,6}}\s*{re.escape(name)}\s*$")
    stop_re = re.compile("(?im)" + "|".join(stop_patterns)) if stop_patterns else None
    if stop_re:
        sm = stop_re.search(text, start)
        end = sm.start() if sm else len(text)
    else:
        sm = re.search(r"(?m)^\s*#{1,6}\s+", text[start:])
        end = start + sm.start() if sm else len(text)
    return text[start:end].strip()

def normalize_metadata_cell_value(field: str, value: str) -> str:
    v = str(value or "").strip()
    v = re.sub(r"<br\s*/?>", " ", v, flags=re.I)
    v = re.sub(r"\n+", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    if field == "volume":
        v = unicodedata.normalize("NFKC", v)
        v = v.replace(" | ", "").replace("|", "")
        v = re.sub(r"\s+", "", v)
    return v

def infer_volume_from_title(title: str) -> str:
    s = unicodedata.normalize("NFKC", title or "")
    m = re.search(r"[（(]\s*(第[一二三四五六七八九十百千万零〇两\d]+卷)\s*[）)]\s*([上下中]册?|上|下|中)?", s)
    if m:
        vol = m.group(1)
        suffix = m.group(2) or ""
        suffix = {"上": "上册", "下": "下册", "中": "中册"}.get(suffix, suffix)
        return vol + suffix
    m = re.search(r"(第[一二三四五六七八九十百千万零〇两\d]+卷)\s*([上下中]册?)?", s)
    if m:
        return m.group(1) + (m.group(2) or "")
    return ""

def postprocess_metadata(meta: Dict[str, object]) -> Dict[str, object]:
    # 先从书名中提取卷册；若表格里已有卷册，则统一格式。
    if not meta.get("volume"):
        vol = infer_volume_from_title(str(meta.get("book_title") or ""))
        if vol:
            meta["volume"] = vol
    else:
        meta["volume"] = normalize_volume_text(meta.get("volume"))

    if meta.get("publication_year"):
        meta["publication_year"] = normalize_metadata_cell_value("publication_year", str(meta.get("publication_year")))
    return meta

def parse_curated_metadata_from_block(block: str, fallback_title: str = "") -> Dict[str, object]:
    meta = empty_metadata()
    meta["metadata_source"] = "curated_ai_block"
    meta["metadata_verified"] = True

    h1 = re.search(r"(?m)^\s*#\s+(.+?)\s*$", block)
    if h1:
        title = clean_output_title(h1.group(1))
        if title and title not in {"元数据", "目录", "正文"}:
            set_if_empty(meta, "book_title", title)

    meta_section = extract_section(block, ["元数据", "书籍元数据", "整理后元数据"], ["目录", "正文"])
    source_for_rows = meta_section or block
    for key, value in markdown_table_rows(source_for_rows):
        k = re.sub(r"\s+", "", key)
        v = value.strip().strip("` ")
        if v in {"", "无", "空", "待核对", "None", "none", "-"}:
            continue
        field = FIELD_MAP.get(k)
        if not field:
            continue
        v = normalize_metadata_cell_value(field, v)
        if field in {"author", "editor", "translator"}:
            meta[field] = merge_people(meta.get(field), split_people(v))
        else:
            meta[field] = v

    for line in source_for_rows.splitlines():
        m = re.match(r"^\s*([^：:|]{2,12})\s*[：:]\s*(.+?)\s*$", line.strip())
        if not m:
            continue
        k = re.sub(r"\s+", "", m.group(1))
        v = m.group(2).strip().strip('"` ')
        if v in {"", "无", "空", "待核对", "None", "none", "-"}:
            continue
        field = FIELD_MAP.get(k)
        if not field:
            continue
        v = normalize_metadata_cell_value(field, v)
        if field in {"author", "editor", "translator"}:
            meta[field] = merge_people(meta.get(field), split_people(v))
        else:
            set_if_empty(meta, field, v)

    if not meta.get("book_title"):
        meta["book_title"] = fallback_title
    return postprocess_metadata(finalize_metadata(meta))

def extract_toc_line_title_page(line: str) -> Tuple[str, Optional[int]]:
    """
    从一行目录文本中提取标题和页码。
    支持数字页码，也支持用户整理目录里常见的“/ 待核对”。
    页码待核对时返回 page=None，但标题必须保留，不能丢弃；否则会漏掉前半本书。
    """
    s = unicodedata.normalize("NFKC", line or "").strip()
    s = re.sub(r"^#{1,6}\s*", "", s).strip()
    s = re.sub(r"^[-*+]\s+", "", s).strip()
    s = re.sub(r"^\d+[.)、]\s+", "", s).strip()
    if not s or is_toc_heading_title(s):
        return "", None

    page = None
    title = s

    unknown_page_patterns = [
        r"\s*[\/／]\s*(?:待核对|待校对|缺页码|无页码)\s*$",
        r"\s*(?:[.·。]{2,}|…{1,}|\.{2,})\s*[（(]?(?:待核对|待校对|缺页码|无页码)[）)]?\s*$",
        r"\s+[（(]?(?:待核对|待校对|缺页码|无页码)[）)]?\s*$",
    ]
    for pat in unknown_page_patterns:
        m = re.search(pat, title)
        if m:
            title = title[:m.start()].strip()
            break

    patterns = [
        r"\s*[\/／]\s*(\d{1,4})(?:\s*[-—–至]\s*\d{1,4})?\s*$",
        r"\s*(?:[.·。]{2,}|…{1,}|\.{2,})\s*[（(]?(\d{1,4})(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]?\s*$",
        r"\s+[（(]?(\d{1,4})(?:\s*[-—–至]\s*\d{1,4})?\s*[）)]?\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, title)
        if m:
            page = int(m.group(1))
            title = title[:m.start()].strip()
            break

    title = strip_toc_page_suffix(title)
    title = re.sub(r"[·.。…]{2,}", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -—–·.。;；")
    return title, page

def is_toc_continuation_line(line: str) -> bool:
    """判断目录行是否明显是上一条目录的续行。"""
    s = unicodedata.normalize("NFKC", line or "").strip()
    return bool(
        re.match(r"^(的|之|与|和|及|并|——|—|、|[（(])", s)
        or re.match(r"^[-—–]", s)
    )


def parse_curated_toc_lines(toc_section: str) -> List[TocEntry]:
    """
    专门解析用户/AI 整理块中的 Markdown 目录。
    目的：避免把“文集……1 评论……5 关于……34”粘成少数超长条目。
    """
    raw_lines: List[str] = []
    for line in (toc_section or "").splitlines():
        s = unicodedata.normalize("NFKC", line or "").strip()
        s = re.sub(r"^[-*+]\s+", "", s).strip()
        s = re.sub(r"^\d+[.)、]\s+", "", s).strip()
        if not s or is_toc_heading_title(s):
            continue
        if re.fullmatch(r"\|?\s*-+\s*\|.*", s):
            continue
        raw_lines.append(s)

    merged: List[str] = []
    pending = ""

    for line in raw_lines:
        title, page = extract_toc_line_title_page(line)
        if not title:
            continue

        if page is not None:
            if pending and is_toc_continuation_line(line):
                merged.append((pending + title).strip())
                pending = ""
            else:
                # pending 多半是上一条的副标题，如“在《胡绳全书》座谈会上的发言”，不单独作为拆分目录。
                pending = ""
                merged.append(line)
            continue

        # 无数字页码行有两类：
        # 1. “/ 待核对”这类整理目录项，必须保留，否则会从第五章等有页码处才开始切。
        # 2. 真正的副标题/续行，仍不单独切分。
        has_unknown_page_marker = bool(re.search(r"(?:[\/／]|…|\.{2,}|。{2,})\s*(?:待核对|待校对|缺页码|无页码)\s*$", line))
        looks_structural = bool(re.match(r"^(上篇|中篇|下篇|附录|后记|楔子|导论|绪论|第[一二三四五六七八九十百千万零〇两\d]+[章节编部篇卷]|[一二三四五六七八九十]+、)", title))
        looks_short_collection = bool(len(title) <= 12 and re.search(r"(文集|评论|随笔|回忆|诗存|辑|卷后记|后记|附录)", title))
        if has_unknown_page_marker or looks_structural or looks_short_collection:
            merged.append(line)
            pending = ""
        else:
            pending = title

    entries: List[TocEntry] = []
    for raw in merged:
        title, page = extract_toc_line_title_page(raw)
        if not title:
            continue
        level = classify_title(title)
        entries.append(TocEntry(len(entries) + 1, title, page, level, raw))
    return entries


def parse_curated_toc_from_block(block: str) -> List[TocEntry]:
    toc_section = extract_section(block, ["目录", "整理后目录", "目次"], ["正文"])
    if not toc_section:
        return []
    entries = parse_curated_toc_lines(toc_section)
    # 若专用解析失败，再退回原通用目录解析。
    if len(entries) >= MIN_TOC_ENTRIES_TO_USE:
        return entries
    lines = []
    for line in toc_section.splitlines():
        s = line.strip()
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\d+[.)、]\s+", "", s)
        lines.append(s)
    return parse_toc("\n".join(lines))

def find_curated_region(raw: str) -> Tuple[str, str, bool]:
    if not USE_CURATED_METADATA_BLOCK:
        return "", raw, False

    for sm in CURATED_START_MARKERS:
        start = raw.find(sm)
        if start < 0:
            continue
        for em in CURATED_END_MARKERS:
            end = raw.find(em, start + len(sm))
            if end >= 0:
                block = raw[start + len(sm):end].strip()
                cleaned_raw = raw[:start] + "\n\n" + raw[end + len(em):]
                return block, cleaned_raw.strip(), True

    head = raw[:METADATA_SCAN_CHARS]
    mm = re.search(r"(?im)^\s*#{1,6}\s*(元数据|书籍元数据|整理后元数据)\s*$", head)
    tm = re.search(r"(?im)^\s*#{1,6}\s*(目录|整理后目录|目次)\s*$", head)
    if not (mm and tm and mm.start() < tm.start()):
        return "", raw, False

    start = 0
    before_meta = head[:mm.start()]
    h1s = list(re.finditer(r"(?m)^\s*#\s+.+$", before_meta))
    if h1s:
        start = h1s[-1].start()
    elif mm.start() < 2000:
        start = 0
    else:
        start = mm.start()

    after_toc_start = tm.end()
    after = raw[after_toc_start:]
    body_heading = re.search(r"(?im)^\s*#{1,6}\s*正文\s*$", after)
    if body_heading and body_heading.start() < 60000:
        end = after_toc_start + body_heading.end()
        block = raw[start:end].strip()
        cleaned_raw = raw[:start] + "\n\n" + raw[end:]
        return block, cleaned_raw.strip(), True

    next_heading = re.search(r"(?m)^\s*#{1,6}\s+.+$", after)
    if next_heading and next_heading.start() < 60000:
        end = after_toc_start + next_heading.start()
        block = raw[start:end].strip()
        cleaned_raw = raw[:start] + "\n\n" + raw[end:]
        return block, cleaned_raw.strip(), True

    block = raw[start:min(len(raw), start + METADATA_SCAN_CHARS)].strip()
    return block, raw, True

def curated_or_auto_metadata(md_path: Path, raw: str) -> Tuple[Dict[str, object], List[TocEntry], str, bool]:
    inferred = infer_book_title(md_path)
    curated_block, raw_without_curated, found = find_curated_region(raw)
    if found:
        meta = parse_curated_metadata_from_block(curated_block, inferred)
        entries = parse_curated_toc_from_block(curated_block)
        return postprocess_metadata(finalize_metadata(meta)), entries, raw_without_curated, True

    raw_main, raw_back, _ = split_off_back_matter(raw)
    cand = metadata_candidates(raw, raw_back)
    meta = get_metadata(md_path, inferred, cand, raw)
    return postprocess_metadata(finalize_metadata(meta)), [], raw, False

REVIEW_COLOR_OUTPUT = False
REVIEW_NAME_MAX_WIDTH = 100

def c(text: str, color: str) -> str:
    if not REVIEW_COLOR_OUTPUT:
        return text
    colors = {
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "cyan": "\033[36m",
        "gray": "\033[90m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }
    return colors.get(color, "") + text + colors["reset"]

def shorten_middle(s: str, max_len: int = REVIEW_NAME_MAX_WIDTH) -> str:
    s = str(s or "")
    if len(s) <= max_len:
        return s
    keep = max_len - 3
    left = keep // 2
    right = keep - left
    return s[:left] + "..." + s[-right:]


def metadata_review_line(i: int, p: Path, meta: Dict[str, object], curated: bool, entries: List[TocEntry]) -> str:
    missing = metadata_missing_fields(meta)

    # 预览阶段的命名规则：
    # 1. 整理块来源：可信，允许按“书名_作者/编者”预览改名；
    # 2. 手动修正来源：可信，允许按“书名_作者/编者”预览改名；
    # 3. 自动识别但无缺失字段：暂按元数据预览改名；
    # 4. 自动识别且缺少关键字段：不建议改名，预览中沿用原文件名和原文件夹名。
    source_value = str(meta.get("metadata_source") or "")
    reliable_for_rename = curated or source_value == "manual_input" or not missing

    if reliable_for_rename:
        target_stem = metadata_filename_stem(meta, p.stem)
        target_name = f"{target_stem}{p.suffix}"
        out_folder = target_stem
    else:
        target_name = p.name
        out_folder = p.stem

    if curated or source_value == "curated_ai_block":
        source_text = "整理块"
    elif source_value == "manual_input":
        source_text = "手动修正"
    else:
        source_text = "自动识别"

    rename_changed = p.name != target_name

    if rename_changed:
        rename_flag = "[!] 将改名"
    elif reliable_for_rename:
        rename_flag = "[OK] 文件名不变"
    else:
        rename_flag = "[!] 自动识别不完整，暂不建议改名"

    if curated:
        toc_text = f"整理块目录 {len(entries)} 条"
    else:
        toc_text = "未读取整理块目录"

    biblio = metadata_biblio_string(meta, p.stem)

    missing_text = ""
    if missing:
        missing_text = "\n缺失字段：" + "、".join(missing)

    old_name = shorten_middle(p.name)
    new_name = shorten_middle(target_name)
    out_folder_display = shorten_middle(out_folder)

    return (
        f"\n[{i:03d}] {rename_flag}｜来源：{source_text}｜{toc_text}\n"
        f"原文件：\n"
        f"  {old_name}\n"
        f"新文件：\n"
        f"  {new_name}\n"
        f"输出夹：\n"
        f"  {out_folder_display}\n"
        f"书目信息：\n"
        f"  {biblio}"
        f"{missing_text}"
    )


def build_metadata_review_markdown(items: List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]]) -> str:
    lines = [
        "# 元数据核对清单",
        "",
        "| 序号 | 原文件 | 重命名后 | 书目信息 | 目录信息 | 元数据来源 | 缺失字段 |",
        "|---:|---|---|---|---|---|---|",
    ]

    def esc(x):
        return str(x or "").replace("|", "\\|").replace("\n", "<br>")

    for i, (p, meta, _raw, entries, curated) in enumerate(items, 1):
        missing_fields = metadata_missing_fields(meta)
        source_value = str(meta.get("metadata_source") or "")
        reliable_for_rename = curated or source_value == "manual_input" or not missing_fields

        if reliable_for_rename:
            target = metadata_filename_stem(meta, p.stem) + p.suffix
        else:
            target = p.name

        if curated or source_value == "curated_ai_block":
            source = "整理块"
        elif source_value == "manual_input":
            source = "手动修正"
        else:
            source = "自动识别"

        biblio = metadata_biblio_string(meta, p.stem)
        toc_info = f"整理块目录 {len(entries)} 条" if curated else "未读取整理块目录"
        missing = "、".join(missing_fields) or ""

        lines.append(
            f"| {i} | {esc(p.name)} | {esc(target)} | {esc(biblio)} | {esc(toc_info)} | {esc(source)} | {esc(missing)} |"
        )

    return "\n".join(lines) + "\n"

def preprocess_raw_for_this_book(text: str) -> str:
    """针对原始 OCR Markdown 的预处理，提前清理明显无用的图片与数学 OCR 噪声。"""
    text = strip_links_and_watermarks(text)
    text = re.sub(r'\$\\underset\{\\cdot\}\{[^}]+\}\$', '', text)
    return text

def metadata_is_reliable_for_output(meta: Dict[str, object]) -> bool:
    """
    判断是否可以用元数据生成输出目录名。
    自动识别且缺少关键字段时，容易把正文标题误当书名，因此不使用它改名。
    """
    source_value = str(meta.get("metadata_source") or "")
    if source_value in {"manual_input", "curated_ai_block"}:
        return True
    return not metadata_missing_fields(meta)

def output_stem_for_processing(md_path: Path, meta: Dict[str, object], fallback: str = "") -> str:
    """
    生成输出目录名。
    元数据可靠时使用“书名_作者/编者”；自动识别不完整时沿用原 md 文件名。
    """
    if metadata_is_reliable_for_output(meta):
        return metadata_filename_stem(meta, fallback or md_path.stem)
    return safe_filename(md_path.stem, max_len=140)


def is_front_matter_noise_heading(title: str, book_title: str = "") -> bool:
    """过滤封面、版权页、CIP、书名页中的伪标题。"""
    t = clean_output_title(title)
    n = normalize_title_for_match(t)
    bn = normalize_title_for_match(book_title)
    noise_exact = {
        "图书在版编目CIP数据", "图书在版编目数据", "版权页", "版权信息", "内容提要",
        "责任编辑", "装帧设计", "版式设计", "人民出版社", "目录", "目次"
    }
    if n in noise_exact:
        return True
    if bn and n == bn:
        return True
    if re.fullmatch(rf"第[{CN_NUM}\d]+卷", n):
        return True
    if re.fullmatch(r"[A-Z][A-Z\s]{5,}", t):
        return True
    if re.search(r"(ISBN|CIP|出版社|责任编辑|装帧|版式|定价|印张|开本|出版|经销|印刷)", t, re.I):
        return True
    return False


def first_substantive_heading_offset(text: str, entries: List[TocEntry], book_title: str = "") -> int:
    """
    找到正文真正开始的位置，跳过封面、版权页、插图说明和前置目录。
    修正点：有些 OCR 会把章号和章题拆成两行，如：
        ##### 第 一 章
        ##### 马克思主义中国化的历史进程
    原逻辑只看第一行，会错过章引言并从下一节开始截断。
    这里把“第X章/编/部”后面的短标题合并后再与目录匹配。
    """
    offs = line_offsets(text)

    def merged_title_at(i: int, raw: str) -> str:
        title = clean_output_title(clean_heading_line(raw))
        if is_bare_chapter_or_part(title):
            for _, next_line in offs[i+1:i+4]:
                nxt = clean_output_title(clean_heading_line(next_line))
                if not nxt:
                    continue
                # 当前行已经是“第X章/编/部”时，下一行即使等于书名，也可能是正式章题；
                # 例如《马克思主义中国化的历史进程》正文开头就是“第 一 章”+书名。
                if len(nxt) <= 120 and not looks_like_toc_entry_line(nxt):
                    return f"{title} {nxt}".strip()
        return title

    # 1. 先用目录第一批条目找正文起点。
    for e in entries[:12]:
        if not e.title or len(normalize_title_for_match(e.title)) < 2:
            continue
        for i, (off, line) in enumerate(offs):
            raw = line.strip()
            if not raw.startswith("#"):
                continue
            title = merged_title_at(i, raw)
            if is_front_matter_noise_heading(title, book_title):
                continue
            if title_similarity(e.title, title) >= 0.55:
                return off

    # 2. 再找明显正文标题。
    for i, (off, line) in enumerate(offs):
        raw = line.strip()
        if not raw.startswith("#"):
            continue
        title = merged_title_at(i, raw)
        if is_front_matter_noise_heading(title, book_title):
            continue
        n = normalize_title_for_match(title)
        if len(n) < 3 or n.isdigit():
            continue
        return off
    return 0

def trim_front_matter_for_splitting(text: str, entries: List[TocEntry], book_title: str = "") -> str:
    start = first_substantive_heading_offset(text, entries, book_title)
    return text[start:].lstrip() if start > 0 else text

def toc_known_page_ratio(entries: List[TocEntry]) -> float:
    if not entries:
        return 0.0
    return sum(1 for e in entries if e.page is not None) / len(entries)


def classify_document_profile(entries: List[TocEntry], corrupt: bool, toc_source: str) -> Tuple[str, str]:
    """根据目录数量、页码情况和异常判断给文档分型。"""
    if not entries:
        return "D", "未取得可用目录，只能使用保守回退或粗分"
    if corrupt:
        if len(entries) >= MIN_TOC_ENTRIES_TO_USE:
            return "C", "目录存在异常，但仍保留为辅助信息"
        return "D", "目录条目过少或异常，不能作为结构依据"
    ratio = toc_known_page_ratio(entries)
    if ratio >= 0.55:
        return "A", f"目录较完整且多数条目有页码，页码率 {ratio:.0%}"
    return "B", f"目录较完整但页码不足，按题名匹配，页码率 {ratio:.0%}"


def split_plan_is_suspicious(entries: List[TocEntry], points: List[MatchPoint], profile: str, strategy: str, main_len: int) -> Tuple[bool, str]:
    """判断当前拆分点是否可疑。可疑时应粗分，避免乱分或漏正文。"""
    if not points:
        return True, "没有得到任何拆分点"

    if len(points) < MIN_SAFE_SPLIT_POINTS and main_len > COARSE_CHUNK_CHARS * 2:
        return True, f"长文档拆分点过少：{len(points)}"

    if entries and profile in {"A", "B"}:
        ratio = len(points) / max(1, len(entries))
        if ratio < MIN_TOC_MATCH_RATIO and main_len > COARSE_CHUNK_CHARS * 2:
            return True, f"目录匹配率过低：{len(points)}/{len(entries)}={ratio:.0%}"

    # 回退 Markdown 时，如果拆分点密度异常高，通常说明 OCR # 号污染严重。
    if "fallback_markdown" in strategy:
        density = len(points) / max(1, main_len / 100000)
        if density > MAX_REASONABLE_POINTS_PER_100K:
            return True, f"Markdown 回退拆分点密度异常：每10万字约 {density:.1f} 个"

    return False, "拆分点数量基本可接受"


def coarse_split_points(text: str, book_title: str = "正文") -> List[MatchPoint]:
    """结构不可靠时使用的保守粗分。目标是保正文，不追求细目录结构。"""
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= COARSE_CHUNK_CHARS:
        return [MatchPoint(1, f"{book_title} 正文", None, "full_text", 0, 0.0, "", "coarse_full")]

    offsets = [0]
    pos = 0
    while pos + COARSE_CHUNK_CHARS < len(text):
        target = pos + COARSE_CHUNK_CHARS
        # 优先在段落边界切；找不到再在句号附近切。
        candidates = [
            text.find("\n\n", target, min(len(text), target + 4000)),
            text.find("\n#", target, min(len(text), target + 5000)),
            text.find("。", target, min(len(text), target + 3000)),
        ]
        next_pos = next((x for x in candidates if x != -1), -1)
        if next_pos == -1 or next_pos <= pos + 1000:
            next_pos = target
        offsets.append(next_pos)
        pos = next_pos

    points: List[MatchPoint] = []
    for i, off in enumerate(offsets, 1):
        points.append(MatchPoint(i, f"正文_part{i:03d}", None, "coarse_part", off, 0.0, "", "coarse_safety"))
    return points


def build_quality_report(
    md_path: Path,
    book: str,
    entries: List[TocEntry],
    points: List[MatchPoint],
    profile: str,
    profile_reason: str,
    strategy: str,
    toc_source: str,
    corrupt: bool,
    reason: str,
    quality_warning: str,
    main: str,
) -> str:
    page_ratio = toc_known_page_ratio(entries)
    match_ratio = len(points) / max(1, len(entries)) if entries else 0.0
    lines = [
        "# v12 分割质量预检报告",
        "",
        f"- 原始文件：`{md_path.name}`",
        f"- 书名：`{book}`",
        f"- 文档类型：{profile}",
        f"- 类型说明：{profile_reason}",
        f"- 目录来源：{toc_source}",
        f"- 目录异常：{corrupt}｜{reason}",
        f"- 目录条目数：{len(entries)}",
        f"- 目录页码率：{page_ratio:.2%}",
        f"- 拆分策略：{strategy}",
        f"- 拆分点数量：{len(points)}",
        f"- 目录匹配率/拆分点比：{match_ratio:.2%}",
        f"- 正文长度：{len(main)} 字符",
        f"- 质量判断：{quality_warning}",
        "",
        "## 说明",
        "",
        "- 本报告只是分割前后的结构预检，不替代一致性检查脚本。",
        "- 如果策略中出现 `coarse_safety`，说明脚本认为细分风险较高，已自动粗分以优先保正文。",
        "- 最终是否可入库，应以一致性检查脚本的输入覆盖率和输出回查率为准。",
    ]
    return "\n".join(lines) + "\n"


def normalize_for_body_metric(text: str) -> str:
    """用于脚本内部轻量自检的正文长度指标。它不追求逐字一致，只判断是否大面积误删。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = strip_links_and_watermarks(t)
    t = clean_latex_ocr_artifacts(t)
    t = clean_ocr_pinyin_noise(t)
    t = re.sub(r"【原书页码标记：\d{1,5}】", "", t)
    t = re.sub(r"%%\s*原书页码：\s*\d{1,5}\s*%%", "", t)
    t = re.sub(r"(?m)^\s*\d{1,5}\s*$", "", t)
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", "", t)
    t = re.sub(r"(?im)^\s*.*(?:z-library|z-lib|1lib|singlelogin|downloaded\s+from).*$", "", t)
    t = re.sub(r"\s+", "", t)
    return t


def body_metric_len(text: str) -> int:
    return len(normalize_for_body_metric(text))


def conservative_body_for_safety(raw: str) -> str:
    """失败兜底正文源：只做最低限度清洗，不删除疑似目录/后置材料，以保正文为第一目标。"""
    text = normalize_page_markers(raw or "")
    text = strip_links_and_watermarks(text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def short_source_hash(source_name: str) -> str:
    return hashlib.md5((source_name or "").encode("utf-8", errors="ignore")).hexdigest()[:8]


def output_dir_source_files(out_dir: Path) -> List[str]:
    """读取已存在输出目录中的 source_file，避免同名书/上下卷互相覆盖。"""
    if not out_dir.exists() or not out_dir.is_dir():
        return []
    found: List[str] = []
    for fp in sorted(out_dir.glob("*.md"))[:80]:
        try:
            txt = fp.read_text(encoding="utf-8", errors="ignore")[:5000]
        except Exception:
            continue
        for m in re.finditer(r'(?m)^source_file:\s*["\']?(.+?)["\']?\s*$', txt):
            found.append(m.group(1).strip().strip('"\''))
        for m in re.finditer(r'(?m)^- 原始文件：(.+?)\s*$', txt):
            found.append(m.group(1).strip().strip('`"\''))
    # 去重保序
    out: List[str] = []
    for x in found:
        if x and x not in out:
            out.append(x)
    return out


def unique_dir_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.name}_{i:02d}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重名输出文件夹：{path}")


def prepare_output_dir(base_stem: str, source_file: str) -> Tuple[Path, str, str]:
    """准备输出目录。若同名目录属于其他源文件，不覆盖，自动追加源文件短哈希。"""
    base_stem = safe_filename(base_stem, max_len=140)
    base = OUTPUT_ROOT / base_stem
    note = ""
    if base.exists():
        sources = output_dir_source_files(base)
        same_source = (not sources) or (source_file in sources)
        if OVERWRITE and same_source:
            shutil.rmtree(base)
            return base, base.name, "同源输出目录已清空重建" if sources else "旧输出目录已清空重建"
        # 已存在但属于其他输入文件，不能覆盖。
        hashed = OUTPUT_ROOT / safe_filename(f"{base_stem}__{short_source_hash(source_file)}", max_len=160)
        if hashed.exists():
            hsources = output_dir_source_files(hashed)
            if OVERWRITE and ((not hsources) or source_file in hsources):
                shutil.rmtree(hashed)
                return hashed, hashed.name, "同源哈希输出目录已清空重建"
            hashed = unique_dir_path(hashed)
        note = f"检测到同名输出目录可能属于其他源文件，已改用：{hashed.name}"
        return hashed, hashed.name, note
    return base, base.name, ""


def output_emit_metric_from_texts(texts: List[str]) -> int:
    return body_metric_len("\n\n".join(texts))

def process_one(md_path: Path, meta: Optional[Dict[str, object]] = None, raw: Optional[str] = None, curated_entries: Optional[List[TocEntry]] = None) -> str:
    inferred = infer_book_title(md_path)
    raw = raw if raw is not None else md_path.read_text(encoding="utf-8", errors="ignore")
    # 预处理特殊文档
    raw = preprocess_raw_for_this_book(raw)
    raw_metric_for_safety = body_metric_len(raw)
    forced_coarse_reason = ""
    extraction_ratio = 1.0
    raw_main, raw_back, _ = split_off_back_matter(raw)
    main = normalize_page_markers(raw_main)
    back = normalize_page_markers(raw_back)
    cand = metadata_candidates(raw, raw_back)
    meta = meta or confirm_metadata(get_metadata(md_path, inferred, cand, raw))
    meta = postprocess_metadata(finalize_metadata(meta))
    md_path = rename_source_markdown(md_path, meta)
    book = metadata_display_title(meta, inferred)

    output_stem = output_stem_for_processing(md_path, meta, inferred)
    out_dir, output_stem, output_dir_note = prepare_output_dir(output_stem, md_path.name)
    if out_dir.exists():
        return f"跳过：{md_path.name}｜原因：输出文件夹已存在且不允许覆盖：{out_dir}"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "00_版权目录作者信息_引文索引.md").write_text(reference_index(str(book), md_path.name, meta, cand, back), encoding="utf-8")

    if curated_entries:
        toc_text, toc_source = "", "curated_ai_block"
        entries = curated_entries
        corrupt, reason = toc_is_corrupted(entries)
        if corrupt:
            alt_toc_text, alt_toc_source = choose_toc(raw, raw_back)
            alt_entries = parse_toc(alt_toc_text) if alt_toc_text else []
            alt_corrupt, alt_reason = toc_is_corrupted(alt_entries)
            if not alt_corrupt and len(alt_entries) >= len(entries):
                toc_text, toc_source = alt_toc_text, alt_toc_source
                entries, corrupt, reason = alt_entries, alt_corrupt, alt_reason
            else:
                reason = "整理块目录不可靠；" + reason
        else:
            reason = "使用手动预处理整理块中的目录"
    else:
        toc_text, toc_source = choose_toc(raw, raw_back)
        entries = parse_toc(toc_text) if toc_text else []
        corrupt, reason = toc_is_corrupted(entries)
    # 若目录来自整理块，raw 中已经移除了整理块，不再用 choose_toc(raw) 去猜测并删除目录。
    # 否则像《胡绳全书》第7卷这种“正文前后都含目次”的文集，会把从“目录”到后文大段正文误删。
    toc_text_for_body_removal = toc_text
    if not toc_text_for_body_removal and not curated_entries:
        toc_text_for_body_removal, _ = choose_toc(raw, raw_back)
    raw_for_body = remove_toc_block_from_raw(raw, toc_text_for_body_removal)
    raw_main_for_body, _, _ = split_off_back_matter(raw_for_body)
    main = normalize_page_markers(raw_main_for_body)
    # 关键修正：封面、版权页、CIP、插图页中的 # 标题不是正文拆分点，先从真正正文标题处截断。
    main = trim_front_matter_for_splitting(main, entries, str(book))

    # v12.1：正文抽取阶段自检。若 split_off_back_matter/remove_toc/trim_front_matter
    # 已经把正文抽短，后面的拆分点预检无法发现，所以这里必须先兜底。
    main_metric = body_metric_len(main)
    if raw_metric_for_safety > 1000:
        extraction_ratio = main_metric / max(1, raw_metric_for_safety)
        if AUTO_COARSE_FALLBACK and extraction_ratio < MIN_BODY_EXTRACTION_RATIO:
            forced_coarse_reason = (
                f"正文抽取疑似误删：抽取正文长度/原始正文长度={extraction_ratio:.2%}，"
                "已改用保守正文源并强制粗分"
            )
            main = conservative_body_for_safety(raw)

    candidates = collect_candidate_headings(main)
    unmatched: List[TocEntry] = []
    profile, profile_reason = classify_document_profile(entries, corrupt, toc_source)

    if profile in {"A", "B"} and len(entries) >= MIN_TOC_ENTRIES_TO_USE:
        points, unmatched, candidates = match_toc_to_body(entries, main)
        if len(points) < MIN_TOC_ENTRIES_TO_USE:
            strategy = f"{profile}_toc_match_weak_then_markdown_fallback"
            points = markdown_split_points(main)
        else:
            strategy = f"{profile}_toc_guided_hybrid"
            points = supplement_body_heading_points(points, candidates)
    elif profile == "C":
        # 目录有问题时，只把它当辅助信息，回退到保守 Markdown 标题。
        strategy = "C_markdown_fallback_with_bad_toc"
        points = markdown_split_points(main)
    else:
        strategy = "D_no_reliable_structure_markdown_fallback"
        points = markdown_split_points(main)

    points = sorted(points, key=lambda x: x.offset)
    suspicious, suspicious_reason = split_plan_is_suspicious(entries, points, profile, strategy, len(main))
    if forced_coarse_reason:
        suspicious = True
        suspicious_reason = forced_coarse_reason
    quality_warning = f"通过预检：{suspicious_reason}"
    if output_dir_note:
        quality_warning += f"；{output_dir_note}"
    if AUTO_COARSE_FALLBACK and suspicious:
        # 关键原则：结构不可靠或正文抽取不可靠时不要强行细分，自动粗分以优先保证正文完整。
        strategy = f"{strategy}_to_coarse_safety"
        points = coarse_split_points(main, str(book))
        unmatched = entries if entries else []
        quality_warning = f"已自动粗分：{suspicious_reason}"
        if output_dir_note:
            quality_warning += f"；{output_dir_note}"

    points = sorted(points, key=lambda x: x.offset)

    if WRITE_MATCH_REPORT:
        (out_dir / "00_目录解析与匹配报告.md").write_text(match_report(entries, corrupt, reason, points, unmatched, strategy, toc_source, candidates), encoding="utf-8")

    if WRITE_QUALITY_REPORT:
        (out_dir / "00_v12_分割质量预检报告.md").write_text(
            build_quality_report(md_path, str(book), entries, points, profile, profile_reason, strategy, toc_source, corrupt, reason, quality_warning, main),
            encoding="utf-8"
        )

    method = f"v12稳定版拆分；文档类型：{profile}；实际策略：{strategy}；目录来源：{toc_source}；目录异常：{corrupt}；粒度：{SPLIT_GRANULARITY}"

    if WRITE_CLEAN_FULL_MD:
        # main 已在前面统一去除了封面/版权页/前置目录；整理清洗版和拆分文件必须基于同一份 main。
        full_body = main.strip()
        clean_full_text = build_clean_full_markdown(str(book), md_path.name, meta, entries, full_body, method)
        FULL_CLEAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        full_clean_name = f"{safe_filename(output_stem)}_整理清洗版.md"
        full_clean_path = FULL_CLEAN_OUTPUT_DIR / full_clean_name
        if full_clean_path.exists() and not OVERWRITE:
            full_clean_path = unique_path(full_clean_path)
        full_clean_path.write_text(clean_full_text, encoding="utf-8")

    manifest: List[str] = []
    if WRITE_SPLIT_MANIFEST:
        manifest = ["# 拆分清单", "", f"- 原始文件：{md_path.name}", f"- 推断书名：{inferred}", f"- YAML 书名：{book}", f"- 输出目录：{out_dir}", f"- 拆分方式：{method}", f"- 目录条目数：{len(entries)}", f"- 拆分点数量：{len(points)}", f"- 目录异常判断：{corrupt}｜{reason}", f"- 正文抽取率：{extraction_ratio:.2%}", f"- 输出目录说明：{output_dir_note or '无'}", "- 引文索引：版权页、目录、作者信息与候选元数据已合并到 00_版权目录作者信息_引文索引.md", "", "| 序号 | 文件名 | 标题 | 类型 | 父级编 | 父级章 | 目录页码 | 匹配分数 | 匹配来源 | 检测页码范围 |", "|---:|---|---|---|---|---|---:|---:|---|---|"]
    file_count = 1 + (1 if WRITE_CLEAN_FULL_MD else 0)
    out_idx = 0
    if points and points[0].offset > 0:
        pre = main[:points[0].offset].strip()
        if pre:
            fn = "00A_前置内容_版权页_目录等.md"
            pr = detect_page_range(pre)
            (out_dir / fn).write_text(build_yaml("前置内容、版权页或目录", book, md_path.name, 0, pr, method, "preface_before_first_heading", "", "", meta) + clean_text_for_output(pre), encoding="utf-8")
            if WRITE_SPLIT_MANIFEST:
                manifest.append(f"| 0 | {fn} | 前置内容、版权页或目录 | preface |  |  |  |  |  | {pr} |")
            file_count += 1

    cur_part = cur_ch = pending = ""
    for i, mp in enumerate(points):
        start = mp.offset
        end = points[i+1].offset if i+1 < len(points) else len(main)
        body = main[start:end].strip()
        nxt = points[i+1] if i+1 < len(points) else None
        if pending and mp.level == "section":
            body = pending + "\n\n---\n\n" + body
            pending = ""
        cur_part, cur_ch, title, cls, pp, pc = infer_output(mp, nxt, entries, cur_part, cur_ch, body)
        title = clean_output_title(title)
        pp = clean_output_title(pp)
        pc = clean_output_title(pc)
        if CHAPTER_INTRO_MODE == "merge_to_first_section" and cls == "chapter_intro":
            pending = body
            continue
        if CHAPTER_INTRO_MODE == "skip_if_short" and cls == "chapter_intro" and clean_body_len(body, mp.title) < 120:
            continue
        # 防止生成空文档
        if (looks_like_toc_only_block(body) and clean_body_len(body, mp.title) < 40) or (clean_body_len(body, mp.title) < 60 and not re.search(r"[。！？；;]", body)):
            continue
        # 增强：有效正文长度检查
        if effective_body_len(body, title) < 30:
            continue
        toc_page = "" if mp.page is None else str(mp.page)
        score = f"{mp.score:.3f}" if mp.score else ""
        chunks = split_body_for_rag(body)
        chunk_count = len(chunks)
        for chunk_i, chunk_body in enumerate(chunks, 1):
            out_idx += 1
            pr = detect_page_range(chunk_body)
            display_title = title if chunk_count == 1 else f"{title}（{chunk_i}/{chunk_count}）"
            suffix = "" if chunk_count == 1 else f"_part{chunk_i:02d}"
            fn = f"{out_idx:02d}_{safe_filename(title)}{suffix}.md"
            path = heading_path(book, pp, pc, title)
            (out_dir / fn).write_text(build_yaml(display_title, book, md_path.name, out_idx, pr, method, cls, pp, pc, meta, toc_page, score, mp.source, path, chunk_i, chunk_count, title) + clean_text_for_output(chunk_body), encoding="utf-8")
            if WRITE_SPLIT_MANIFEST:
                manifest.append(f"| {out_idx} | {fn} | {display_title} | {cls} | {pp} | {pc} | {toc_page} | {score} | {mp.source} | {pr} |")
            file_count += 1

    if not points:
        pr = detect_page_range(main)
        fn = f"01_{safe_filename(book)}_未识别标题_完整文本.md"
        (out_dir / fn).write_text(build_yaml(f"{book}：未识别标题的完整文本", book, md_path.name, 1, pr, "未识别到标题，未拆分", "full_text", "", "", meta) + clean_text_for_output(main), encoding="utf-8")
        if WRITE_SPLIT_MANIFEST:
            manifest.append(f"| 1 | {fn} | 未识别标题，完整保存 | full_text |  |  |  |  |  | {pr} |")
        file_count += 1

    if WRITE_SPLIT_MANIFEST:
        (out_dir / "00_拆分清单.md").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return f"完成：{md_path.name} → {file_count} 个文件｜输出：{out_dir}"

def output_dir_for(md_path: Path) -> Path:
    return OUTPUT_ROOT / safe_filename(infer_book_title(md_path))

def split_existing_and_new(files: List[Path]) -> Tuple[List[Path], List[Path]]:
    existing: List[Path] = []
    new: List[Path] = []
    for p in files:
        (existing if output_dir_for(p).exists() else new).append(p)
    return existing, new

def print_file_group(title: str, files: List[Path], show_output: bool = False) -> None:
    print(title)
    if not files:
        print("  （无）")
        return
    for i, p in enumerate(files, 1):
        if show_output:
            print(f"  {i:>2}. {p.name}｜输出：{output_dir_for(p)}")
        else:
            print(f"  {i:>2}. {p.name}")

def ask_yes_no(question: str) -> bool:
    while True:
        answer = input(f"{question} [y/n]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("请输入 y 或 n。")

def ask_start_split() -> bool:
    while True:
        answer = input("是否开始分割以上文档？直接回车或输入 y 开始，输入 n 取消：").strip().lower()
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("请输入 y/n，或直接回车开始。")

def detect_and_confirm_metadata(md_path: Path) -> Tuple[Path, Dict[str, object], str]:
    raw = md_path.read_text(encoding="utf-8", errors="ignore")
    raw_main, raw_back, _ = split_off_back_matter(raw)
    cand = metadata_candidates(raw, raw_back)
    meta = confirm_metadata(get_metadata(md_path, infer_book_title(md_path), cand, raw))
    return md_path, finalize_metadata(meta), raw

def collect_confirmed_metadata(files: List[Path]) -> List[Tuple[Path, Dict[str, object], str]]:
    confirmed: List[Tuple[Path, Dict[str, object], str]] = []
    for i, p in enumerate(files, 1):
        print("-" * 60)
        print(f"正在检测元数据：{i}/{len(files)}｜{p.name}")
        confirmed.append(detect_and_confirm_metadata(p))
    return confirmed

def print_confirmed_plan(items: List[Tuple[Path, Dict[str, object], str]]) -> None:
    print("-" * 60)
    print("已确认以下文档，准备分割：")
    for i, (p, meta, _) in enumerate(items, 1):
        target_name = metadata_filename_stem(meta, p.stem) + p.suffix
        author = first_people_text(meta.get("author")) or "待核对"
        year = str(meta.get("publication_year") or "待核对")
        print(f"{i:>3}. {p.name}")
        print(f"     → {target_name}")
        print(f"     书目信息：{metadata_biblio_string(meta, p.stem)}")

def process_files(items) -> None:
    for item in items:
        if isinstance(item, tuple) and len(item) == 5:
            p, meta, raw, entries, _curated = item
        elif isinstance(item, tuple) and len(item) == 3:
            p, meta, raw = item
            entries = []
        else:
            p, meta, raw, entries = item, None, None, []
        try:
            print(process_one(p, meta, raw, entries))
        except Exception as e:
            print(f"失败：{p.name}｜错误：{e}")

def scan_all_documents(files: List[Path]) -> List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]]:
    items: List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]] = []
    for i, p in enumerate(files, 1):
        raw = p.read_text(encoding="utf-8", errors="ignore")
        meta, curated_entries, raw_for_processing, curated = curated_or_auto_metadata(p, raw)
        items.append((p, meta, raw_for_processing, curated_entries, curated))
    return items

def print_metadata_review(items: List[Tuple[Path, Dict[str, object], str, List[TocEntry], bool]]) -> None:
    print("-" * 60)
    print("已扫描以下文档元数据，请核对。确认无误后输入 y 开始分割。")
    print("命名规则：原 md 文件与输出文件夹均使用“书名_作者”。")
    print("-" * 60)
    for i, (p, meta, _raw, entries, curated) in enumerate(items, 1):
        print(metadata_review_line(i, p, meta, curated, entries))
    print("-" * 60)

def apply_cli_args() -> None:
    """允许在 PowerShell 中临时覆盖脚本顶部 CONFIG，不填则沿用脚本内配置。"""
    global INPUT_DIR, OUTPUT_ROOT, FULL_CLEAN_OUTPUT_DIR, RECURSIVE
    global RENAME_SOURCE_MD_ON_SPLIT, SPLIT_GRANULARITY, CHAPTER_INTRO_MODE
    global COARSE_CHUNK_CHARS, AUTO_COARSE_FALLBACK, ASSUME_YES, SKIP_METADATA_EDIT

    parser = argparse.ArgumentParser(description="Markdown 书籍批量拆分脚本 v12 稳定版")
    parser.add_argument("--input-dir", help="原始整本书 md 输入目录")
    parser.add_argument("--output-root", help="分割输出根目录")
    parser.add_argument("--full-clean-output-dir", help="整本整理清洗版输出目录；默认跟 output-root 相同")
    parser.add_argument("--recursive", action="store_true", help="递归扫描输入目录")
    parser.add_argument("--no-rename-source", action="store_true", help="不重命名原始 md 文件")
    parser.add_argument("--granularity", choices=["section", "chapter"], help="拆分粒度")
    parser.add_argument("--chapter-intro-mode", choices=["separate", "merge_to_first_section", "skip_if_short"], help="章引言处理方式")
    parser.add_argument("--coarse-chars", type=int, help="粗分模式每块目标字符数")
    parser.add_argument("--no-coarse-fallback", action="store_true", help="关闭自动粗分降级")
    parser.add_argument("--yes", action="store_true", help="跳过最终确认，直接开始分割")
    parser.add_argument("--skip-metadata-edit", action="store_true", help="跳过交互式元数据修正，只生成核对清单并直接使用识别结果")
    parser.add_argument("--files", nargs="*", help="指定要处理的 md 文件路径（可多个）。不填则扫描 input-dir 下所有非辅助 md 文件。")
    args = parser.parse_args()

    if args.input_dir:
        INPUT_DIR = Path(args.input_dir)
    if args.output_root:
        OUTPUT_ROOT = Path(args.output_root)
        if not args.full_clean_output_dir:
            FULL_CLEAN_OUTPUT_DIR = OUTPUT_ROOT
    if args.full_clean_output_dir:
        FULL_CLEAN_OUTPUT_DIR = Path(args.full_clean_output_dir)
    if args.recursive:
        RECURSIVE = True
    if args.no_rename_source:
        RENAME_SOURCE_MD_ON_SPLIT = False
    if args.granularity:
        SPLIT_GRANULARITY = args.granularity
    if args.chapter_intro_mode:
        CHAPTER_INTRO_MODE = args.chapter_intro_mode
    if args.coarse_chars:
        COARSE_CHUNK_CHARS = args.coarse_chars
    if args.no_coarse_fallback:
        AUTO_COARSE_FALLBACK = False
    if args.yes:
        ASSUME_YES = True
    if args.skip_metadata_edit:
        SKIP_METADATA_EDIT = True

    return args


def main() -> None:
    args = apply_cli_args()
    if not INPUT_DIR.exists() and not args.files:
        raise FileNotFoundError(f"输入文件夹不存在：{INPUT_DIR}")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if WRITE_CLEAN_FULL_MD:
        FULL_CLEAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [Path(f).resolve() for f in args.files]
        missing = [f for f in files if not f.exists()]
        if missing:
            print(f"以下文件未找到：{', '.join(str(f) for f in missing)}")
            return
    else:
        files = list(INPUT_DIR.rglob("*.md") if RECURSIVE else INPUT_DIR.glob("*.md"))
    files = [p for p in files if not is_generated_md(p)]
    if not files:
        print(f"没有找到可处理的整本书 md 文件：{INPUT_DIR}")
        return

    print(f"发现 {len(files)} 个 md 文件。")
    print(f"输入目录：{INPUT_DIR}")
    print(f"输出目录：{OUTPUT_ROOT}")
    print(f"整本清洗版统一目录：{FULL_CLEAN_OUTPUT_DIR}")
    print(f"覆盖旧输出：{OVERWRITE}")
    print(f"拆分粒度：{SPLIT_GRANULARITY}")
    print(f"章引言处理：{CHAPTER_INTRO_MODE}")
    print(f"保留页码并转换为：{PAGE_MARKER_OUTPUT_TEMPLATE}")

    items = scan_all_documents(files)
    print_metadata_review(items)

    if not SKIP_METADATA_EDIT:
        items = edit_metadata_items_interactively(items)
        print("\n修正后的最终核对清单：")
        print_metadata_review(items)
    else:
        print("已跳过交互式元数据修正。")

    review_path = OUTPUT_ROOT / METADATA_REVIEW_LIST_FILENAME
    review_path.write_text(build_metadata_review_markdown(items), encoding="utf-8")
    print(f"元数据核对清单已写入：{review_path}")

    if ASSUME_YES or ask_start_split():
        process_files(items)
    else:
        print("已取消分割。")

    print("-" * 60)
    print("全部处理结束。")


if __name__ == "__main__":
    main()
