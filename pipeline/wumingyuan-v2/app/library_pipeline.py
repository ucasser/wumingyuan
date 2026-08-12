"""PDF 入库管线：文本质量检查、分批云 OCR、逐页缓存和可恢复 Markdown。"""
import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass

import fitz

from .parsers import Document, Section
from .text_normalizer import detect_text, inspect_pdf_page, to_simplified


class Cancelled(RuntimeError):
    pass


_manifest_lock = threading.Lock()


def clean_text(text: str) -> str:
    """清除 PDF 文本层常见控制字符，保留换行和制表符。"""
    return "".join(ch for ch in (text or "") if ch in "\n\t" or ord(ch) >= 32).strip()


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:20]


def inspect_pdf(path: str, min_chars: int = 20) -> dict:
    with fitz.open(path) as pdf:
        reports = [inspect_pdf_page(p) for p in pdf]
    low = [i + 1 for i, report in enumerate(reports)
           if report["ocr_needed"] or report["chars"] < min_chars]
    return {"pages": len(reports), "ocr_pages": low, "ocr_needed": bool(low),
            "text_pages": len(reports) - len(low), "page_quality": reports}


def visually_blank(page: fitz.Page) -> bool:
    """低分辨率检查页面是否接近纯白；用于容忍封底等真正空白页。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY,
                          alpha=False)
    samples = pix.samples
    if not samples:
        return True
    # 灰度低于 245 视为有墨迹；仅在 OCR 也返回空时使用，3% 以下视为封底噪点。
    dark = sum(1 for value in samples if value < 245)
    return dark / len(samples) < 0.03


def _paths(data_root: str, doc_id: str):
    root = os.path.join(data_root, "OCR文本", doc_id)
    return root, os.path.join(root, "pages.json"), os.path.join(root, "document.md")


def _load_pages(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _ocr_confidence(text: str, report: dict, used_ocr: bool) -> float:
    """没有逐字置信度的 OCR 服务统一折算成可审计的页级质量分。"""
    if not used_ocr:
        return 1.0
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return 0.0
    info = detect_text(text)
    useful = (info["cjk_chars"] + info["latin_chars"]) / max(1, len(compact))
    length_score = min(1.0, len(compact) / 180)
    garbage_penalty = 0.18 if "\ufffd" in text else 0.0
    original_penalty = 0.08 if "encoding_garbage" in report.get("reasons", []) else 0.0
    return round(max(0.0, min(0.96, 0.48 + 0.30 * useful +
                              0.18 * length_score - garbage_penalty -
                              original_penalty)), 3)


def _subset_pdf(source: fitz.Document, original_pages: list) -> str:
    """生成仅含指定页面的临时 PDF，供超过云端限制的大文件使用。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        batch_path = f.name
    out = fitz.open()
    try:
        for pno in original_pages:
            out.insert_pdf(source, from_page=pno - 1, to_page=pno - 1)
        out.save(batch_path, garbage=4, deflate=True)
        return batch_path
    except Exception:
        try:
            os.remove(batch_path)
        except OSError:
            pass
        raise
    finally:
        out.close()


def _split_for_upload(source: fitz.Document, pages: list, max_pages: int,
                      max_bytes: int) -> list[tuple[str, list]]:
    """只为超限文档生成尽可能大的批次，同时满足页数和文件大小限制。"""
    jobs = []
    pending = [pages[i:i + max_pages] for i in range(0, len(pages), max_pages)]
    while pending:
        group = pending.pop(0)
        path = _subset_pdf(source, group)
        if os.path.getsize(path) <= max_bytes:
            jobs.append((path, group))
            continue
        os.remove(path)
        if len(group) == 1:
            raise RuntimeError(f"第 {group[0]} 页单页文件超过飞桨本地上传大小限制")
        middle = len(group) // 2
        pending[0:0] = [group[:middle], group[middle:]]
    return jobs


def _ocr_subset(path: str, original_pages: list, cfg: dict, base_dir: str) -> dict:
    """识别物理切分后的 PDF，并映射回原书页码。"""
    from . import paddle_ocr
    local_numbers = list(range(1, len(original_pages) + 1))
    result = paddle_ocr.parse_pdf(path, cfg, local_numbers, base_dir)
    return {str(orig): result.get(local, "") for orig, local in zip(original_pages, local_numbers)}


def process_pdf(path: str, ocr_cfg: dict, data_root: str, progress=None,
                should_cancel=None) -> tuple[Document, dict]:
    doc_id = file_hash(path)
    root, pages_json, md_path = _paths(data_root, doc_id)
    os.makedirs(root, exist_ok=True)
    cached = _load_pages(pages_json)
    provider_json = os.path.join(root, "providers.json")
    page_providers = _load_pages(provider_json)
    min_chars = int(ocr_cfg.get("min_chars", 20))
    paddle_cfg = ocr_cfg.get("paddle") or {}
    # 飞桨异步 API：本地上传单次最多 1000 页、50 MB。
    max_pages = min(1000, max(1, int(paddle_cfg.get("max_pages_per_job", 1000))))
    max_bytes = min(50, max(1, int(paddle_cfg.get("max_upload_mb", 50)))) * 1024 * 1024
    with fitz.open(path) as pdf:
        title = pdf.metadata.get("title") or os.path.splitext(os.path.basename(path))[0]
        title = re.sub(r"[\x00-\x1f]+", " ", str(title))
        title = re.sub(r"\s+", " ", title).strip()
        direct = {str(i + 1): clean_text(p.get_text("text")) for i, p in enumerate(pdf)}
        quality = {str(i + 1): inspect_pdf_page(p) for i, p in enumerate(pdf)}
        needed = [i for i in range(1, len(pdf) + 1)
                  if (len(direct[str(i)]) < min_chars or quality[str(i)]["ocr_needed"])
                  and str(i) not in cached]
        if needed and not ocr_cfg.get("enabled", True):
            raise RuntimeError(f"有 {len(needed)} 页需要 OCR，但 OCR 尚未开启")
        provider = ocr_cfg.get("provider", "paddle")
        if progress:
            progress(0, len(pdf), f"文字层检测：{len(pdf)} 页中 {len(pdf)-len(needed)} 页直接提取，"
                     f"{len(needed)} 页需要 OCR")
        if provider == "paddle" and needed:
            from . import paddle_ocr
            direct_upload = len(pdf) <= max_pages and os.path.getsize(path) <= max_bytes
            if direct_upload:
                jobs = [(path, needed, False)]
            else:
                jobs = [(tmp, pages, True) for tmp, pages in
                        _split_for_upload(pdf, needed, max_pages, max_bytes)]
            try:
                for job_no, (upload_path, batch, temporary) in enumerate(jobs, 1):
                    if should_cancel and should_cancel():
                        raise Cancelled("用户停止处理；已完成页面已保存，下次将继续")
                    if progress:
                        mode = "整本一次提交" if direct_upload else f"大文件批次 {job_no}/{len(jobs)}"
                        progress(batch[0], len(pdf),
                                 f"OCR 第 {batch[0]}–{batch[-1]} 页 / 共 {len(pdf)} 页 · {mode}")
                    try:
                        if direct_upload:
                            raw = paddle_ocr.parse_pdf(
                                upload_path, paddle_cfg, batch, data_root
                            )
                            got = {str(n): raw.get(n, "") for n in batch}
                        else:
                            got = _ocr_subset(
                                upload_path, batch, paddle_cfg, data_root
                            )
                    except Exception:
                        if not ocr_cfg.get("fallback_local", True):
                            raise
                        got = {}
                    for n in batch:
                        if clean_text(got.get(str(n), "")):
                            page_providers[str(n)] = "paddle"
                    if ocr_cfg.get("fallback_local", True):
                        from .parsers import _ocr_page
                        for n in batch:
                            if not clean_text(got.get(str(n), "")):
                                got[str(n)] = _ocr_page(pdf[n - 1], ocr_cfg)
                                if clean_text(got.get(str(n), "")):
                                    page_providers[str(n)] = "tesseract"
                    cached.update(got)
                    _save_json(pages_json, cached)
                    _save_json(provider_json, page_providers)
            finally:
                for upload_path, _, temporary in jobs:
                    if temporary:
                        try:
                            os.remove(upload_path)
                        except OSError:
                            pass
        elif provider != "paddle":
            for n in needed:
                if should_cancel and should_cancel():
                    raise Cancelled("用户停止处理；已完成页面已保存，下次将继续")
                from .parsers import _ocr_page
                if progress:
                    progress(n, len(pdf), f"OCR 第 {n}–{n} 页 / 共 {len(pdf)} 页 · 本地识别")
                cached[str(n)] = _ocr_page(pdf[n - 1], ocr_cfg)
                if clean_text(cached.get(str(n), "")):
                    page_providers[str(n)] = "tesseract"
                _save_json(pages_json, cached)
                _save_json(provider_json, page_providers)

        sections, md, page_records = [], [f"# {title}\n"], []
        unresolved, blank_pages = [], []
        detected_scripts, detected_languages, detected_layouts, converters = [], [], [], []
        for i in range(1, len(pdf) + 1):
            use_ocr = len(direct[str(i)]) < min_chars or quality[str(i)]["ocr_needed"]
            original_text = (
                clean_text(cached.get(str(i), "")) if use_ocr else direct[str(i)]
            )
            text, converter = to_simplified(original_text)
            info = detect_text(original_text)
            detected_scripts.append(info["script"])
            detected_languages.append(info["language"])
            layout = (
                "vertical" if quality[str(i)].get("vertical_ratio", 0) >= 0.35
                else "horizontal"
            )
            detected_layouts.append(layout)
            converters.append(converter)
            if not text.strip():
                if visually_blank(pdf[i - 1]):
                    blank_pages.append(i)
                else:
                    unresolved.append(i)
            sections.append(Section(text=text.strip(), heading=f"p.{i}", order=i - 1))
            shown = text.strip() or ("（空白页）" if i in blank_pages else "（本页 OCR 未识别）")
            page_records.append({
                "page_no": i,
                "pdf_page": i,
                "printed_page": "",
                "raw_text": original_text,
                "normalized_text": text,
                "display_text": text,
                "ocr_used": use_ocr,
                "ocr_provider": (
                    page_providers.get(str(i), provider)
                    if use_ocr else "embedded_text"
                ),
                "ocr_confidence": _ocr_confidence(
                    original_text, quality[str(i)], use_ocr
                ),
                "language": info["language"],
                "script": info["script"],
                "layout": layout,
                "quality": quality[str(i)],
            })
            md.append(f"\n<!-- page: {i} -->\n\n## 第 {i} 页\n\n{shown}\n")
    with open(md_path + ".tmp", "w", encoding="utf-8") as f:
        f.write("".join(md))
    os.replace(md_path + ".tmp", md_path)
    meta = {"document_id": doc_id, "pdf_path": os.path.abspath(path),
            "markdown_path": md_path, "pages": len(sections), "unresolved_pages": unresolved,
            "blank_pages": blank_pages, "ocr_pages": len(cached),
            "ocr_provider": provider,
            "parser": "pymupdf+paddle" if provider == "paddle" else "pymupdf+tesseract",
            "ocr_reason_pages": {
                page: report["reasons"] for page, report in quality.items()
                if report["ocr_needed"]
            },
            "detected_language": max(set(detected_languages),
                                     key=detected_languages.count) if detected_languages else "unknown",
            "detected_script": (
                "traditional" if "traditional" in detected_scripts else "simplified"
            ),
            "detected_layout": (
                "vertical" if detected_layouts.count("vertical") >
                detected_layouts.count("horizontal") else "horizontal"
            ),
            "normalizer": (
                "opencc-t2s" if "opencc-t2s" in converters
                else "opencc-unavailable" if "opencc-unavailable" in converters
                else "unchanged"
            ),
            "manifest_path": os.path.join(root, "manifest.json"),
            "page_records": page_records,
    }
    public_meta = dict(meta)
    public_meta.pop("page_records", None)
    _save_json(os.path.join(root, "manifest.json"), public_meta)
    return Document(path=path, title=title, sections=sections), meta
