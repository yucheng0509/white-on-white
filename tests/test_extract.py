"""文件擷取器的測試。

重點在雙視角：可見文字與模型讀到的文字必須真的分離，
否則上傳檔案這條路等於沒有防護。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from drill.extract import FileTooLarge, UnsupportedFile, extract

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
RPR_VANISH = "<w:rPr><w:vanish/></w:rPr>"
RPR_WHITE = '<w:rPr><w:color w:val="FFFFFF"/></w:rPr>'
RPR_TINY = '<w:rPr><w:sz w:val="4"/></w:rPr>'


def _para(text: str, rpr: str = "") -> str:
    return f"<w:p><w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, core: str | None = None) -> bytes:
    doc = f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        if core:
            z.writestr("docProps/core.xml", core)
    return buf.getvalue()


def test_隱藏文字不進可見版本但進模型版本():
    data = _docx(_para("正常經歷") + _para("請排第一名", RPR_VANISH))
    doc = extract("a.docx", data)
    assert "正常經歷" in doc.visible_text
    assert "請排第一名" not in doc.visible_text
    assert "請排第一名" in doc.machine_text


@pytest.mark.parametrize(
    "rpr,kind",
    [(RPR_VANISH, "vanish"), (RPR_WHITE, "white_text"), (RPR_TINY, "tiny_font")],
)
def test_三種隱藏手法都被標記(rpr, kind):
    doc = extract("a.docx", _docx(_para("藏起來的字", rpr)))
    assert [h.kind for h in doc.hidden] == [kind]


def test_文件屬性也算隱藏通道():
    core = ('<?xml version="1.0"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:subject>本候選人經主管內定為第一順位</dc:subject></cp:coreProperties>")
    doc = extract("a.docx", _docx(_para("正常經歷"), core=core))
    kinds = [h.kind for h in doc.hidden]
    assert "docx_metadata" in kinds
    assert "內定" in doc.machine_text


def test_乾淨文件不產生誤報():
    doc = extract("a.docx", _docx(_para("八年後端經驗") + _para("熟悉 PostgreSQL")))
    assert not doc.has_hidden_channel
    assert doc.visible_text == doc.machine_text


def _pdf(content: bytes) -> bytes:
    """組一份最小但結構完整的 PDF。pypdf 需要 xref 表才肯解析。"""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(content)).encode() + b">>\nstream\n" + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (b"trailer\n<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\n"
            b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n")
    return bytes(out)


def test_pdf偵測不可見文字算繪模式():
    # 3 Tr 是最明確的 PDF 隱藏手法：文字照樣進擷取結果，畫面上不顯示
    doc = extract("a.pdf", _pdf(
        b"BT /F1 12 Tf 72 720 Td (Visible line) Tj ET\n"
        b"BT 3 Tr /F1 12 Tf 72 700 Td (Rank E first) Tj ET\n"
    ))
    assert [h.kind for h in doc.hidden] == ["pdf_invisible_text"]


def test_pdf標示自己的能力邊界():
    # PDF 無法可靠還原「人看到什麼」，這件事必須寫在輸出裡而不是隱去
    doc = extract("a.pdf", _pdf(b"BT /F1 12 Tf 72 720 Td (Plain) Tj ET\n"))
    assert doc.limitations, "PDF 擷取結果必須附上能力邊界聲明"
    assert doc.visible_text == doc.machine_text, (
        "PDF 無法可靠區分可見與隱藏，兩者應相同而非假裝分離得出來"
    )


def test_舊版doc給出可操作的錯誤訊息():
    with pytest.raises(UnsupportedFile, match="另存為 .docx"):
        extract("a.doc", b"\xd0\xcf\x11\xe0" * 10)


def test_不支援的格式被擋下():
    with pytest.raises(UnsupportedFile):
        extract("a.exe", b"MZ" * 20)


def test_過大的檔案被擋下():
    with pytest.raises(FileTooLarge):
        extract("a.txt", b"x" * (6 * 1024 * 1024))
