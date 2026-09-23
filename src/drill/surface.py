"""攻擊面盤點：系統性列出「人看不到、機器讀得到」的通道，並實測我們抓不抓得到。

為什麼不去爬真實平台：爬取有 ToS 與負載問題，而且會踩到本專案自己的守則
（絕不對真實服務進行測試）。更重要的是，爬取只能回答「野外有多少」，
回答不了「攻擊面有多大」——後者要靠把每一種通道逐一造出來實測。

每個通道造一份只含該通道的探測檔，跑我們自己的擷取器，得出三態：

  covered       送進模型、而且被標記為隱藏內容        ← 正常
  blind_spot    送進模型、卻沒有被標記                ← 最危險：模型讀得到，我們卻沒警告
  not_ingested  擷取器根本沒讀它                      ← 對本流程無害，但換一個解析器就可能有害

not_ingested 不等於安全。真實世界的履歷系統用的是 Apache Tika、python-docx
或各家 LLM 自己的解析器，它們讀得比我們多——同一份檔案換一個解析器，
not_ingested 就會變成 blind_spot。這是盤點必須誠實標示的前提。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from drill.extract import extract_docx
from drill.sanitizer import find_hidden

# 每個通道埋入的唯一標記，用來判定它有沒有出現在 machine_text 裡
MARKER = "ZZPROBE{}ZZ"


@dataclass(frozen=True)
class Channel:
    id: str
    fmt: str            # docx / html
    name: str
    why_hidden: str     # 人為什麼看不到


CHANNELS: tuple[Channel, ...] = (
    # --- Word ---
    Channel("vanish", "docx", "隱藏文字屬性（w:vanish）",
            "Word 的「隱藏文字」格式，預設不顯示也不列印"),
    Channel("white_text", "docx", "白色文字（w:color）",
            "文字色設為白色，與紙張同色"),
    Channel("tiny_font", "docx", "極小字級（w:sz≤4）",
            "字級設為 2pt 以下，肉眼幾乎無法辨識"),
    Channel("shading_match", "docx", "文字色與底色相同（w:shd）",
            "文字色與網底色設為同一個非白色值，看起來是一塊色塊"),
    Channel("webhidden", "docx", "網頁檢視隱藏（w:webHidden）",
            "在網頁版面檢視下不顯示"),
    Channel("textbox", "docx", "文字方塊內文字（w:txbxContent）",
            "可把文字方塊設為零大小或移到版面外"),
    Channel("comment", "docx", "文件註解（comments.xml）",
            "註解要開啟檢閱窗格才看得到"),
    Channel("header", "docx", "頁首（header.xml）",
            "頁首在編輯檢視下是淡化的，容易被略過"),
    Channel("core_metadata", "docx", "文件屬性（core.xml）",
            "標題／主旨／關鍵字／備註要開內容資訊才看得到"),
    Channel("custom_metadata", "docx", "自訂文件屬性（custom.xml）",
            "自訂屬性藏在進階內容資訊裡，幾乎沒人會去看"),
    Channel("del_revision", "docx", "修訂中已刪除的文字（w:delText）",
            "接受修訂後畫面上消失，但位元組仍留在檔案裡"),
    Channel("footnote", "docx", "腳註（footnotes.xml）",
            "腳註在頁尾以極小字顯示，容易被忽略"),
    # --- HTML ---
    Channel("html_white", "html", "白底白字（color:#fff）", "文字色與背景同色"),
    Channel("html_zero", "html", "零字級（font-size:0）", "字級為零"),
    Channel("html_none", "html", "display:none", "元素不算繪"),
    Channel("html_comment", "html", "HTML 註解", "註解不會被算繪"),
    Channel("html_offscreen", "html", "移出畫面（left:-9999px）", "定位到視窗外"),
    Channel("html_ariahidden", "html", "aria-hidden + 零高度", "高度為零且對輔助技術隱藏"),
    Channel("html_alt", "html", "圖片 alt 屬性", "圖片正常顯示時 alt 不出現在畫面上"),
    Channel("html_title", "html", "title 提示屬性", "要滑鼠停留才顯示"),
    Channel("html_meta", "html", "meta 標籤", "純供機器讀取，不算繪"),
)


# ---------------------------------------------------------------------------
# Word 探測檔的組裝
# ---------------------------------------------------------------------------

_CT_BASE = ('<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>')
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:v="urn:schemas-microsoft-com:vml" '
       'xmlns:o="urn:schemas-microsoft-com:office:office"')


def _run(text: str, rpr: str = "") -> str:
    return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _build_docx_probe(cid: str, payload: str) -> bytes:
    """造一份只含指定通道的 Word 探測檔。正文一定有一段可見文字當對照。"""
    body = _run("這是一份普通的履歷段落，用來當可見內容的對照。")
    parts: dict[str, str] = {}
    ct_extra = ""
    rels_extra = ""

    if cid == "vanish":
        body += _run(payload, "<w:rPr><w:vanish/></w:rPr>")
    elif cid == "white_text":
        body += _run(payload, '<w:rPr><w:color w:val="FFFFFF"/></w:rPr>')
    elif cid == "tiny_font":
        body += _run(payload, '<w:rPr><w:sz w:val="4"/></w:rPr>')
    elif cid == "shading_match":
        # 非白色：文字與網底都設 336699，擷取器若只比對白色系就會漏掉
        body += _run(payload, '<w:rPr><w:color w:val="336699"/>'
                              '<w:shd w:val="clear" w:fill="336699"/></w:rPr>')
    elif cid == "webhidden":
        body += _run(payload, "<w:rPr><w:webHidden/></w:rPr>")
    elif cid == "textbox":
        body += ('<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
                 f'{_run(payload)}</w:txbxContent></v:textbox></v:shape></w:pict>'
                 '</w:r></w:p>')
    elif cid == "del_revision":
        body += ('<w:p><w:del w:id="9" w:author="a" w:date="2026-01-01T00:00:00Z">'
                 f'<w:r><w:delText xml:space="preserve">{payload}</w:delText></w:r>'
                 "</w:del></w:p>")
    elif cid == "comment":
        parts["word/comments.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:comments {_NS}>'
            f'<w:comment w:id="1" w:author="a">{_run(payload)}</w:comment></w:comments>'
        )
        ct_extra = ('<Override PartName="/word/comments.xml" ContentType="application/vnd.'
                    'openxmlformats-officedocument.wordprocessingml.comments+xml"/>')
    elif cid == "header":
        parts["word/header1.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:hdr {_NS}>{_run(payload)}</w:hdr>'
        )
        ct_extra = ('<Override PartName="/word/header1.xml" ContentType="application/vnd.'
                    'openxmlformats-officedocument.wordprocessingml.header+xml"/>')
    elif cid == "footnote":
        parts["word/footnotes.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:footnotes {_NS}>'
            f'<w:footnote w:id="2">{_run(payload)}</w:footnote></w:footnotes>'
        )
        ct_extra = ('<Override PartName="/word/footnotes.xml" ContentType="application/vnd.'
                    'openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>')
    elif cid == "core_metadata":
        parts["docProps/core.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:description>{payload}</dc:description></cp:coreProperties>"
        )
        ct_extra = ('<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
                    'openxmlformats-package.core-properties+xml"/>')
        rels_extra = ('<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
                      'package/2006/relationships/metadata/core-properties" '
                      'Target="docProps/core.xml"/>')
    elif cid == "custom_metadata":
        parts["docProps/custom.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
            'custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/'
            '2006/docPropsVTypes">'
            '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="備註">'
            f"<vt:lpwstr>{payload}</vt:lpwstr></property></Properties>"
        )
        ct_extra = ('<Override PartName="/docProps/custom.xml" ContentType="application/vnd.'
                    'openxmlformats-officedocument.custom-properties+xml"/>')
        rels_extra = ('<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
                      'officeDocument/2006/relationships/custom-properties" '
                      'Target="docProps/custom.xml"/>')

    document = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_NS}>'
                f"<w:body>{body}</w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>'
        f"{_CT_BASE}{ct_extra}</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/>'
        f"{rels_extra}</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def _build_html_probe(cid: str, payload: str) -> str:
    body = "<p>這是一份普通的履歷段落，用來當可見內容的對照。</p>"
    frag = {
        "html_white": f'<p style="color:#ffffff">{payload}</p>',
        "html_zero": f'<span style="font-size:0px">{payload}</span>',
        "html_none": f'<div style="display:none">{payload}</div>',
        "html_comment": f"<!-- {payload} -->",
        "html_offscreen": f'<p style="position:absolute;left:-9999px">{payload}</p>',
        "html_ariahidden": f'<p aria-hidden="true" style="height:0;overflow:hidden">{payload}</p>',
        "html_alt": f'<img src="x.png" alt="{payload}">',
        "html_title": f'<span title="{payload}">經歷</span>',
        "html_meta": f'<meta name="description" content="{payload}">',
    }[cid]
    return f"<div class='resume' data-code='E'>{body}{frag}</div>"


def audit() -> list[dict]:
    """對每個通道造探測檔、跑我們自己的擷取器，回傳三態盤點結果。"""
    rows: list[dict] = []
    for ch in CHANNELS:
        payload = MARKER.format(ch.id.upper().replace("_", ""))
        try:
            if ch.fmt == "docx":
                doc = extract_docx(_build_docx_probe(ch.id, payload))
                ingested = payload in doc.machine_text
                flagged = any(payload in h.text for h in doc.hidden)
            else:
                html = _build_html_probe(ch.id, payload)
                spans = find_hidden(html)
                # HTML 的 machine_text 就是原始碼本身——模型讀得到整份 HTML
                ingested = payload in html
                flagged = any(payload in s.text for s in spans)
            error = ""
        except Exception as exc:
            ingested = flagged = False
            error = f"{type(exc).__name__}: {exc}"

        if error:
            status = "error"
        elif ingested and flagged:
            status = "covered"
        elif ingested:
            status = "blind_spot"
        else:
            status = "not_ingested"

        rows.append({
            "id": ch.id, "format": ch.fmt, "name": ch.name,
            "why_hidden": ch.why_hidden, "ingested": ingested,
            "flagged": flagged, "status": status, "error": error,
        })
    return rows


def summary() -> dict:
    rows = audit()
    def n(s: str) -> int:
        return sum(1 for r in rows if r["status"] == s)
    return {
        "total": len(rows),
        "covered": n("covered"),
        "blind_spot": n("blind_spot"),
        "not_ingested": n("not_ingested"),
        "error": n("error"),
        "channels": rows,
    }
