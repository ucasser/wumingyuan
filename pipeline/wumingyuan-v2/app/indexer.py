"""持续书籍导入：PDF → 逐页文本/OCR → Markdown 全文输出。"""
import glob
import json
import os

from .library_pipeline import Cancelled, file_hash, process_pdf


def _expand_braces(pattern):
    if "{" not in pattern:
        return [pattern]
    pre, rest = pattern.split("{", 1)
    body, post = rest.split("}", 1)
    return [pre + x + post for x in body.split(",")]


def iter_files(source):
    root, seen = os.path.expanduser(source["path"]), set()
    for pat in _expand_braces(source.get("glob", "**/*.pdf")):
        for path in glob.glob(os.path.join(root, pat), recursive=True):
            if os.path.isfile(path) and path not in seen:
                seen.add(path)
                yield path


def run_ingest(cfg, sources=None, progress=None, should_cancel=None):
    """PDF → OCR → Markdown。不生成向量分块，不调用大模型。"""
    report = progress or (lambda *_: None)
    sources = sources if sources is not None else cfg.get("sources", [])
    data_root = cfg["data_root"]
    ocr_cfg = cfg.get("ocr", {})

    plan = [(p, os.path.basename(p)) for s in sources for p in iter_files(s)]
    total = len(plan)
    report(0, total, f"发现 {total} 份文献")

    added = skipped = failed = 0
    for pos, (path, name) in enumerate(plan, 1):
        if should_cancel and should_cancel():
            return {"files_added": added, "skipped": skipped, "failed": failed,
                    "cancelled": True, "total": total, "done": pos - 1}
        try:
            fh = file_hash(path)
            ocr_dir = os.path.join(data_root, "ocr", fh)
            md_path = os.path.join(ocr_dir, "document.md")
            pages_path = os.path.join(ocr_dir, "pages.json")

            if os.path.exists(md_path):
                skipped += 1
                report(pos, total, f"已存在并跳过：{name}")
                continue

            def page_progress(done, page_total, msg):
                report(pos - 1, total, f"{name} · {msg}")

            doc, meta = process_pdf(path, ocr_cfg, data_root,
                                     page_progress, should_cancel)

            unresolved = meta.get("unresolved_pages") or []
            allowed = max(1, round(int(meta.get("pages", 1)) * 0.0025))
            if len(unresolved) > allowed:
                raise RuntimeError(
                    f"仍有 {len(unresolved)} 页没有可用文字，"
                    f"超过安全容差 {allowed} 页"
                )

            os.makedirs(ocr_dir, exist_ok=True)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"# {doc.title}\n\n")
                for i, section in enumerate(doc.sections, 1):
                    heading = str(section.heading or f"第{i}节").strip()
                    f.write(f"\n## {heading}\n\n{section.text.strip()}\n")

            page_records = meta.get("page_records", [])
            with open(pages_path, "w", encoding="utf-8") as f:
                json.dump(page_records, f, ensure_ascii=False, indent=2)

            added += 1
            report(pos, total, f"已导入：{name}（{meta.get('pages', '?')} 页）")

        except Cancelled:
            report(pos - 1, total, f"已停止：{name}")
            return {"files_added": added, "skipped": skipped, "failed": failed,
                    "cancelled": True, "total": total, "done": pos - 1}
        except Exception as ex:
            failed += 1
            report(pos, total, f"失败：{name} — {ex}")

    report(total, total, f"完成：新增 {added}，跳过 {skipped}，失败 {failed}")
    return {"files_added": added, "skipped": skipped, "failed": failed,
            "cancelled": False, "total": total, "done": total}
