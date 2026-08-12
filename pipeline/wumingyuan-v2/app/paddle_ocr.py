"""PaddleOCR 官方托管 API：把扫描 PDF 解析成逐页 Markdown。"""
import os
import re


def resolve_token(cfg: dict) -> str:
    token = str(cfg.get("api_token", "")).strip()
    if token:
        return token
    return os.environ.get(cfg.get("api_token_env", "PADDLEOCR_ACCESS_TOKEN"), "").strip()


def mask_token(token: str) -> str:
    if not token:
        return ""
    return "••••" + token[-4:] if len(token) > 4 else "••••"


def _client(cfg: dict, base_dir: str):
    token = resolve_token(cfg)
    if not token:
        raise RuntimeError("未设置 PaddleOCR Access Token")

    # PaddleX 导入时会创建缓存目录；固定在应用 data 内，避免污染用户主目录。
    os.environ.setdefault(
        "PADDLE_PDX_CACHE_HOME", os.path.join(base_dir, "OCR缓存")
    )
    try:
        from paddleocr import PaddleOCRClient
    except ImportError as ex:
        raise RuntimeError("缺少 paddleocr SDK，请重新运行启动器安装依赖") from ex

    kwargs = {
        "token": token,
        "request_timeout": float(cfg.get("request_timeout", 300)),
        "poll_timeout": float(cfg.get("poll_timeout", 1200)),
    }
    base_url = str(cfg.get("base_url", "")).strip()
    if base_url:
        kwargs["base_url"] = base_url
    return PaddleOCRClient(**kwargs)


def _page_ranges(page_numbers):
    """把 [1,2,3,7,9,10] 压缩成官方 API 接受的 1-3,7,9-10。"""
    nums = sorted(set(int(n) for n in page_numbers))
    if not nums:
        return None
    spans, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(spans)


def clean_markdown(text: str) -> str:
    """保留标题/段落/表格，去掉对向量检索无意义的图片资源与 HTML 噪声。"""
    text = text or ""
    text = re.sub(r"!\[([^\]]*)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(path: str, cfg: dict, page_numbers: list, base_dir: str) -> dict:
    """调用一次官方文档解析任务，返回 {1-based页码: Markdown文本}。"""
    if not page_numbers:
        return {}
    client = _client(cfg, base_dir)
    try:
        from paddleocr import PaddleOCRVLOptions

        options = PaddleOCRVLOptions(
            use_doc_orientation_classify=bool(cfg.get("orientation", True)),
            use_doc_unwarping=bool(cfg.get("unwarping", False)),
            use_layout_detection=True,
            prettify_markdown=True,
            restructure_pages=False,
            return_markdown_images=False,
            visualize=False,
        )
        result = client.parse_document(
            file_path=path,
            model=cfg.get("model", "PaddleOCR-VL-1.6"),
            options=options,
            page_ranges=_page_ranges(page_numbers),
        )
        texts = [clean_markdown(page.markdown_text) for page in result.pages]
        if len(texts) != len(page_numbers):
            raise RuntimeError(
                f"飞桨返回 {len(texts)} 页，但请求了 {len(page_numbers)} 页，无法可靠映射页码"
            )
        return dict(zip(page_numbers, texts))
    finally:
        client.close()


def test_connection(cfg: dict, base_dir: str) -> str:
    """上传一张很小的测试图，验证 Token、模型和任务轮询均可用。"""
    import tempfile
    from PIL import Image, ImageDraw

    os.makedirs(base_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        img = Image.new("RGB", (420, 100), "white")
        ImageDraw.Draw(img).text((20, 35), "PaddleOCR API test 2026", fill="black")
        img.save(tmp)
        pages = parse_pdf(tmp, cfg, [1], base_dir)
        if not pages:
            raise RuntimeError("飞桨 API 未返回页面结果")
        return "飞桨 OCR API 可用"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
