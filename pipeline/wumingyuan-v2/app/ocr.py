"""OCR 引擎（Tesseract）管理：检测安装状态、从设置面板一键安装。

安装尽力而为：Mac 用 brew，Debian/Ubuntu（含 Docker）用 apt；
找不到包管理器或无权限时返回手动安装指引，不报错。
"""
import os
import re
import shutil
import subprocess

import requests

_COMMON_PATHS = (
    "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


def tesseract_path():
    p = shutil.which("tesseract")
    if p:
        return p
    for c in _COMMON_PATHS:
        if c and os.path.exists(c):
            return c
    return None


def configure_pytesseract():
    """把检测到的 tesseract 路径告诉 pytesseract（解决 Windows 不在 PATH 的问题）。"""
    path = tesseract_path()
    if path:
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = path
        except Exception:
            pass
    return path


def status() -> dict:
    path = tesseract_path()
    version, langs = "", []
    if path:
        try:
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=10)
            blob = out.stdout or out.stderr
            if blob:
                version = blob.splitlines()[0].strip()
        except Exception:
            pass
        try:
            out = subprocess.run([path, "--list-langs"], capture_output=True,
                                 text=True, timeout=10)
            langs = [l.strip() for l in (out.stdout or "").splitlines()[1:] if l.strip()]
        except Exception:
            pass
    return {"installed": path is not None, "path": path,
            "version": version, "langs": langs}


def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: 1)() == 0


def install_stream():
    """逐行产出安装进度 dict：{status:'line'|'error'|'done', ...}。"""
    brew = shutil.which("brew")
    apt = shutil.which("apt-get")
    winget = shutil.which("winget")

    run_env = os.environ.copy()
    if brew:                                    # macOS：只装引擎本体，语言用「补装语言」下载
        cmds = [[brew, "install", "tesseract"]]
        run_env.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_CLEANUP="1",
                       HOMEBREW_NO_ENV_HINTS="1")
    elif apt:                                   # Debian/Ubuntu / Docker
        pre = [] if _is_root() else ["sudo"]
        cmds = [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y",
                       "tesseract-ocr", "tesseract-ocr-chi-sim", "tesseract-ocr-eng"]]
    elif winget:                                # Windows 10/11
        cmds = [[winget, "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
                 "--silent", "--accept-package-agreements", "--accept-source-agreements"]]
    else:
        yield {"status": "error", "error":
               "未找到可用的包管理器（brew / apt / winget）。请手动安装 Tesseract："
               "Windows 见 https://github.com/UB-Mannheim/tesseract/wiki ；"
               "装好后回来点「重新检测」，再用「补装语言」下载所需语言。"}
        return

    for cmd in cmds:
        yield {"status": "line", "line": "$ " + " ".join(cmd)}
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=run_env)
            for line in proc.stdout:
                yield {"status": "line", "line": line.rstrip()}
            proc.wait()
            if proc.returncode != 0:
                yield {"status": "error",
                       "error": f"命令失败（退出码 {proc.returncode}）。"
                                "可能需要管理员权限，或按上面提示手动安装。"}
                return
        except Exception as ex:
            yield {"status": "error", "error": str(ex)}
            return
    if winget:
        yield {"status": "line", "line":
               "提示：Windows 安装完可能需重开一次程序才能识别到 Tesseract；"
               "语言用下方「补装语言」下载即可。"}
    yield {"status": "done"}


# ---------------- 语言包下载（与引擎安装拆分，走 CDN 镜像，不依赖 brew/apt）----------------
# tessdata_fast：体积小、精度好；jsdelivr CDN 一般可直连
_TESSDATA_URL = "https://cdn.jsdelivr.net/gh/tesseract-ocr/tessdata_fast@main/{code}.traineddata"


def tessdata_dir():
    """定位 Tesseract 的 tessdata 目录（放语言数据文件的地方）。"""
    path = tesseract_path()
    if path:
        try:
            out = subprocess.run([path, "--list-langs"], capture_output=True,
                                 text=True, timeout=10)
            first = ((out.stdout or "") + (out.stderr or "")).splitlines()[0]
            m = re.search(r'"([^"]+)"', first)      # ...in "/opt/homebrew/share/tessdata/"...
            if m and os.path.isdir(m.group(1)):
                return m.group(1)
        except Exception:
            pass
        prefix = os.path.dirname(os.path.dirname(path))   # <prefix>/bin/tesseract
        cand = os.path.join(prefix, "share", "tessdata")
        if os.path.isdir(cand):
            return cand
    env = os.environ.get("TESSDATA_PREFIX")
    return env if env and os.path.isdir(env) else None


def download_lang_stream(codes):
    """把选定语言的 .traineddata 下载到 tessdata 目录，逐条产出进度。"""
    d = tessdata_dir()
    if not d:
        yield {"status": "error", "error": "未找到 tessdata 目录，请先安装 OCR 引擎。"}
        return
    if not os.access(d, os.W_OK):
        yield {"status": "error",
               "error": f"没有写入权限：{d}。可在终端手动下载 .traineddata 放进该目录。"}
        return
    for code in codes:
        code = code.strip()
        if not code:
            continue
        dest = os.path.join(d, f"{code}.traineddata")
        yield {"status": "line", "line": f"下载 {code} → {dest}"}
        try:
            r = requests.get(_TESSDATA_URL.format(code=code), stream=True, timeout=120,
                             proxies={"http": None, "https": None})
            r.raise_for_status()
            got, tmp = 0, dest + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
            if got < 1000:
                os.remove(tmp)
                yield {"status": "error", "error": f"{code} 下载内容异常（可能该语言代码不存在）"}
                return
            os.replace(tmp, dest)
            yield {"status": "line", "line": f"  完成 {code}（{got // 1024} KB）"}
        except Exception as ex:
            yield {"status": "error",
                   "error": f"{code} 下载失败：{ex}。网络不通时可手动下载 "
                            f"{_TESSDATA_URL.format(code=code)} 放到 {d}。"}
            return
    yield {"status": "done"}
