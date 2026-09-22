"""從 Word 與 PDF 擷取內容，並找出人看不到、模型讀得到的部分。

## 為什麼這一層重要

人資實際收到的履歷是 .docx 和 .pdf，不是 HTML。這兩種格式各自有比 HTML
更多的隱藏通道，而且更難被人工察覺——沒有人會去看一份 Word 履歷的 XML。

## 各格式的能力邊界（必須誠實標示）

**.docx 能做到精確的雙視角。** OOXML 把樣式寫在標記裡，因此可以逐段判斷
某段文字是否被設為隱藏（w:vanish）、白色（w:color）、極小字級（w:sz），
另外註解、頁首頁尾與 metadata 也都是模型讀得到而人不一定會看的地方。

**.pdf 只能做到部分。** PDF 的文字可見性取決於算繪指令與繪製順序，
單靠文字擷取無法完整還原「人到底看到了什麼」。這裡只處理兩種明確可判的情形：
文字算繪模式 3（不可見文字，OCR 圖層常用，也是最常見的隱藏手法）與文件 metadata。
其餘情形一律標為「無法判定」，不假裝測得出來。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from drill.sanitizer import HiddenSpan

MAX_FILE_BYTES = 5 * 1024 * 1024        # 單檔上限
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024  # docx 解壓後上限，防 zip bomb


class UnsupportedFile(ValueError):
    """不支援的檔案格式。"""


class FileTooLarge(ValueError):
    """檔案超過大小上限。"""


@dataclass
class ExtractedDoc:
    """一份文件的雙視角擷取結果。"""

    visible_text: str                   # 人在畫面上看得到的
    machine_text: str                   # 模型實際讀進去的（含隱藏內容）
    hidden: list[HiddenSpan] = field(default_factory=list)
    file_type: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def has_hidden_channel(self) -> bool:
        return bool(self.hidden)


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_LIGHT_COLORS = {"FFFFFF", "FEFEFE", "FDFDFD", "auto"}


def _docx_runs(xml: bytes):
    """逐個 w:r（文字 run）產出 (文字, 是否隱藏, 隱藏原因)。"""
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml)
    for run in root.iter(f"{_W}r"):
        text = "".join(t.text or "" for t in run.iter(f"{_W}t"))
        if not text.strip():
            continue

        props = run.find(f"{_W}rPr")
        reason = ""
        if props is not None:
            if props.find(f"{_W}vanish") is not None:
                reason = "vanish"                      # Word 的「隱藏文字」屬性
            else:
                color = props.find(f"{_W}color")
                val = (color.get(f"{_W}val") if color is not None else "") or ""
                if val.upper() in _LIGHT_COLORS and val != "auto":
                    reason = "white_text"
                else:
                    size = props.find(f"{_W}sz")
                    half_pt = size.get(f"{_W}val") if size is not None else None
                    # w:sz 的單位是半點，4 半點 = 2pt，肉眼幾乎看不見
                    if half_pt and half_pt.isdigit() and int(half_pt) <= 4:
                        reason = "tiny_font"
        yield text, bool(reason), reason


def extract_docx(data: bytes) -> ExtractedDoc:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_UNCOMPRESSED_BYTES:
            raise FileTooLarge(f"解壓後 {total // 1024 // 1024}MB，超過上限")
        names = set(z.namelist())

        visible: list[str] = []
        machine: list[str] = []
        hidden: list[HiddenSpan] = []

        if "word/document.xml" in names:
            for text, is_hidden, reason in _docx_runs(z.read("word/document.xml")):
                machine.append(text)
                if is_hidden:
                    hidden.append(HiddenSpan(kind=reason, text=text, context="內文"))
                else:
                    visible.append(text)

        # 註解、頁首頁尾：模型讀得到，人多半不會特地去看
        for name in sorted(names):
            if not re.fullmatch(r"word/(comments|header\d*|footer\d*)\.xml", name):
                continue
            label = {"c": "註解", "h": "頁首", "f": "頁尾"}[name.split("/")[1][0]]
            for text, _is_hidden, _reason in _docx_runs(z.read(name)):
                machine.append(text)
                hidden.append(HiddenSpan(kind=f"docx_{label}", text=text, context=label))

        # 文件屬性：標題、主旨、關鍵字、備註都會被多數擷取工具讀進去
        for name in ("docProps/core.xml", "docProps/app.xml"):
            if name not in names:
                continue
            for match in re.finditer(rb">([^<>]{8,})<", z.read(name)):
                text = match.group(1).decode("utf-8", "ignore").strip()
                if len(text) >= 8 and not text.replace("-", "").replace(":", "").isdigit():
                    machine.append(text)
                    hidden.append(HiddenSpan(kind="docx_metadata", text=text, context=name))

    return ExtractedDoc(
        visible_text="\n".join(visible),
        machine_text="\n".join(machine),
        hidden=hidden,
        file_type="docx",
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

# 文字算繪模式 3 = 不可見。OCR 圖層用它疊在掃描影像上，
# 也是把文字藏給機器讀的最直接手法。
_INVISIBLE_MODE_RE = re.compile(rb"\b3\s+Tr\b")


def extract_pdf(data: bytes) -> ExtractedDoc:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages_text: list[str] = []
    hidden: list[HiddenSpan] = []
    invisible_pages: list[int] = []

    for index, page in enumerate(reader.pages, start=1):
        pages_text.append(page.extract_text() or "")
        try:
            content = page.get_contents()
            raw = content.get_data() if content is not None else b""
        except Exception:
            raw = b""
        if _INVISIBLE_MODE_RE.search(raw):
            invisible_pages.append(index)

    machine = "\n".join(pages_text)

    for page_no in invisible_pages:
        hidden.append(HiddenSpan(
            kind="pdf_invisible_text",
            text=f"第 {page_no} 頁使用了不可見文字算繪模式（3 Tr）",
            context=f"page {page_no}",
        ))

    meta = reader.metadata or {}
    for key, value in meta.items():
        text = str(value).strip()
        if len(text) >= 8:
            hidden.append(HiddenSpan(kind="pdf_metadata", text=text, context=str(key)))
            machine += f"\n{text}"

    limitations = [
        "PDF 的文字可見性取決於算繪指令與繪製順序，單靠文字擷取無法完整還原"
        "「人實際看到什麼」。本擷取器只判定不可見算繪模式與 metadata，"
        "白底白字、被圖形覆蓋、超出頁面範圍等情形無法偵測。"
    ]
    if invisible_pages:
        limitations.append(
            f"第 {'、'.join(map(str, invisible_pages))} 頁含不可見文字，"
            "但擷取器無法分辨其中哪幾段屬於隱藏內容，因此可見版本未將其剔除。"
        )

    return ExtractedDoc(
        visible_text=machine,      # 無法可靠區分，誠實地兩者相同
        machine_text=machine,
        hidden=hidden,
        file_type="pdf",
        limitations=limitations,
    )


# ---------------------------------------------------------------------------

def extract(filename: str, data: bytes) -> ExtractedDoc:
    """依副檔名分派擷取器。"""
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge(f"檔案 {len(data) // 1024}KB，超過 {MAX_FILE_BYTES // 1024 // 1024}MB 上限")

    name = filename.lower()
    if name.endswith(".docx"):
        return extract_docx(data)
    if name.endswith(".pdf"):
        return extract_pdf(data)
    if name.endswith((".txt", ".md", ".html", ".htm")):
        text = data.decode("utf-8", "replace")
        return ExtractedDoc(visible_text=text, machine_text=text, file_type=name.rsplit(".", 1)[-1])
    if name.endswith(".doc"):
        raise UnsupportedFile(
            "舊版 .doc 是二進位格式，本擷取器不支援。請在 Word 另存為 .docx 後再上傳。"
        )
    raise UnsupportedFile(f"不支援的格式：{filename}。目前支援 .docx、.pdf、.txt、.html")


# ---------------------------------------------------------------------------
# 產生示範用的 Word 檔
# ---------------------------------------------------------------------------

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_RPR_BY_CARRIER = {
    "vanish": "<w:rPr><w:vanish/></w:rPr>",
    "white_text": '<w:rPr><w:color w:val="FFFFFF"/></w:rPr>',
    "tiny_font": '<w:rPr><w:sz w:val="4"/></w:rPr>',
    "body": "",
}


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def build_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """把 (文字, 載體) 的清單組成一份 .docx。

    載體為 vanish／white_text／tiny_font 時，那一段在 Word 裡看不見，
    但任何文字擷取工具都讀得到——這正是要示範的東西。

    用途限定於產生本平台的示範素材。內容全為虛構資料，
    使用範圍見 ETHICS.md。
    """
    body = "".join(
        f'<w:p><w:r>{_RPR_BY_CARRIER.get(carrier, "")}'
        f'<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'
        for text, carrier in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        z.writestr("_rels/.rels", _DOCX_RELS)
        z.writestr("word/document.xml", document)
    return buf.getvalue()
