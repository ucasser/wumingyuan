"""入库前文字检测与标准化：语言、简繁、PDF 版式和简体检索副本。"""
from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache

from .parsers import Document


_TRADITIONAL_HINTS = set(
    "萬與專業東絲兩嚴喪個豐臨為麗舉義烏樂喬習鄉書買亂爭於虧雲亞產"
    "親億僅從倉儀價眾優會偉傳傷倫偽體餘債傾儲兒黨蘭關興養獸內冊寫"
    "軍農馮沖決況凍淨準幾鳳凱劑劍劇勞勢勳匯區醫華協單賣盧衛廳歷厲"
    "壓厭廁廂廈廚縣參雙發變敘葉號嘆嚇嗎噸聽啟員問喚喪喬圍園圓圖團"
    "執堅塊塗塵墊墜壘壞壯聲殼壺處備復夠頭誇夾奪奮獎婦媽娛嬰學寧寶"
    "實審寬將屆屬歲島嶺嶽巖幣帥師帳帶幫幹廣莊慶廬庫應廟廢開異棄張"
    "強彈彙彎錄徑從德憶憂懷態慘慣憲懸驚戀戰戲戶掃揚擔據擴擺擾攜攝"
    "敗數斷無時晉晝暫曆術樸機殺雜權條楊業極標樓樣樹橋檔檢欄歡歐歸"
    "殘毀氣漢湯溝滅滬淚淵淺渦測濟濃濤濫灣濕燈靈災爐點煉熱獲環現瑣"
    "產畫當疇療瘋瘓盡監盤睜礎禮禍種稱穀積穩窩窮竄競筆節範築簡簽籠"
    "類糧糾紀約紅紋納紙級紛紡終組結絕統經綜綠維網緊緒線練縮縱總績"
    "織繼續纏罰罷職聯聰肅腦腳脫臉臘舊艰艦藝蘇蘊虛蟲蠶補裝裡複見觀"
    "規覺覽觸計訂認討讓訓議訊記講證評詞該詳語誤說請論諸讀課調談謀"
    "謝識譜譯護貝負財責貢貨貪貧貫貴貸費賀資賓賦質賴贊贏趕趙跡踐車"
    "軌轉輪軟較載輕輛輩輝輸轄辦辭邊遼達遷過運還進遠違連遲適選遺郵"
    "鄧鄭鄰釋鑒針鈔鋼錢錯鍋鎖鏡長門閃閉間閣閱隊陽陰陣階際陸陳險隨"
    "隱難霧靜頂頃項順須頑頓頒領頗頻題額顏願風飛飯飲館馬馳駁駐駕驗"
    "髮鬥魚鳥鳴鴨鷹鹽麥黃齊齒龍龜"
)


def detect_text(text: str) -> dict:
    text = text or ""
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    latin = re.findall(r"[A-Za-z]", text)
    kana = re.findall(r"[\u3040-\u30ff]", text)
    hangul = re.findall(r"[\uac00-\ud7af]", text)
    cyrillic = re.findall(r"[\u0400-\u04ff]", text)
    arabic = re.findall(r"[\u0600-\u06ff]", text)
    devanagari = re.findall(r"[\u0900-\u097f]", text)
    meaningful = sum(map(len, (cjk, latin, kana, hangul, cyrillic, arabic, devanagari)))
    traditional = sum(1 for ch in cjk if ch in _TRADITIONAL_HINTS)
    trad_ratio = traditional / max(1, len(cjk))
    if kana and len(kana) >= max(3, len(latin) // 3):
        language = "ja"
    elif hangul and len(hangul) >= max(3, len(latin) // 3):
        language = "ko"
    elif cyrillic and len(cyrillic) >= max(6, len(latin)):
        language = "cyrillic"
    elif arabic and len(arabic) >= 6:
        language = "ar"
    elif devanagari and len(devanagari) >= 6:
        language = "devanagari"
    elif len(cjk) >= max(4, len(latin) * 0.35):
        language = "zh"
    elif len(latin) >= max(20, len(cjk) * 2):
        language = "en"
    elif meaningful:
        language = "mixed"
    else:
        language = "unknown"
    return {
        "language": language,
        "script": "traditional" if traditional >= 3 and trad_ratio >= 0.012 else "simplified",
        "cjk_chars": len(cjk),
        "latin_chars": len(latin),
        "other_script_chars": len(kana) + len(hangul) + len(cyrillic)
                              + len(arabic) + len(devanagari),
        "traditional_ratio": round(trad_ratio, 4),
    }


def to_simplified(text: str) -> tuple[str, str]:
    info = detect_text(text)
    if info["script"] != "traditional":
        return text, "unchanged"
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert(text), "opencc-t2s"
    except ImportError:
        return text, "opencc-unavailable"


@lru_cache(maxsize=1)
def _search_converter():
    """检索层始终加载繁转简转换器，不依赖单个短句的简繁检测阈值。"""
    try:
        from opencc import OpenCC
        return OpenCC("t2s")
    except ImportError:
        return None


def normalize_for_search(text: str) -> str:
    """生成简繁统一的检索副本；展示、引文和原始 Markdown 均不改写。

    查询通常很短，不能复用 ``detect_text`` 的“至少三个繁体提示字”门槛；
    否则“動的邏輯”之类短语仍会漏过统一处理。OpenCC 对已经是简体的
    文字是幂等的，因此检索层可以无条件转换。
    """
    value = text or ""
    converter = _search_converter()
    return converter.convert(value) if converter else value


def normalize_document(doc: Document) -> tuple[Document, dict]:
    original = "\n".join(section.text for section in doc.sections)
    before = detect_text(original)
    converter = "unchanged"
    sections = []
    for section in doc.sections:
        normalized, used = to_simplified(section.text)
        if used != "unchanged":
            converter = used
        sections.append(replace(section, text=normalized))
    normalized_doc = replace(doc, sections=sections)
    after = detect_text("\n".join(section.text for section in sections))
    return normalized_doc, {
        "detected_language": before["language"],
        "detected_script": before["script"],
        "traditional_ratio": before["traditional_ratio"],
        "normalizer": converter,
        "normalized_script": after["script"],
    }


def inspect_pdf_page(page) -> dict:
    """判断有字不等于可用：同时检查乱码、控制字符和竖排阅读方向。"""
    text = page.get_text("text") or ""
    compact = re.sub(r"\s+", "", text)
    replacement = text.count("\ufffd")
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\r\t")
    alnum_cjk = len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text))
    useful_ratio = alnum_cjk / max(1, len(compact))
    vertical_lines = total_lines = 0
    try:
        layout = page.get_text("dict")
        for block in layout.get("blocks", []):
            for line in block.get("lines", []):
                direction = line.get("dir", (1.0, 0.0))
                total_lines += 1
                if abs(float(direction[1])) > abs(float(direction[0])):
                    vertical_lines += 1
    except Exception:
        pass
    vertical_ratio = vertical_lines / max(1, total_lines)
    reasons = []
    if len(compact) < 20:
        reasons.append("text_too_short")
    if replacement > max(2, len(text) * 0.005) or controls:
        reasons.append("encoding_garbage")
    if compact and useful_ratio < 0.55:
        reasons.append("low_useful_character_ratio")
    if total_lines >= 3 and vertical_ratio >= 0.35:
        reasons.append("vertical_layout")
    return {
        "chars": len(compact),
        "useful_ratio": round(useful_ratio, 3),
        "vertical_ratio": round(vertical_ratio, 3),
        "ocr_needed": bool(reasons),
        "reasons": reasons,
        **detect_text(text),
    }
