"""雙視角擷取與輸入淨化。

同一套偵測邏輯有兩個方向的用途：
  - 正著用：找出「人看不到但機器讀得到」的內容 —— 這就是注入的藏身處
  - 反著用：把那些內容剝掉 —— 這就是配置 C/D 的輸入淨化

刻意只用標準庫的 html.parser。Playwright 的 computed style 更準
（能處理外部 CSS 與繼承），留待需要時再加；目前的注入素材都是 inline style。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# 判定「視覺上不可見」的 inline style 規則
_HIDDEN_STYLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("display_none", re.compile(r"display\s*:\s*none", re.I)),
    ("visibility_hidden", re.compile(r"visibility\s*:\s*hidden", re.I)),
    ("zero_font", re.compile(r"font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem)?\b", re.I)),
    ("transparent", re.compile(r"opacity\s*:\s*0(?:\.0+)?\b", re.I)),
    ("white_text", re.compile(r"color\s*:\s*(?:#fff(?:fff)?\b|white\b|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\))", re.I)),
    ("offscreen", re.compile(r"(?:left|top)\s*:\s*-\d{3,}(?:px|em)", re.I)),
    # 高度壓成零再裁切。常與 aria-hidden 併用，視覺上完全不佔位。
    # 單看 height:0 不算隱藏（可能只是還沒撐開），要搭配 overflow:hidden 才成立。
    ("clipped", re.compile(
        r"(?=[^\"']*height\s*:\s*0(?:px|pt|em|rem)?\b)"
        r"(?=[^\"']*overflow\s*:\s*hidden)", re.I)),
)

_INVISIBLE_ATTRS = ("hidden",)


@dataclass
class HiddenSpan:
    """一段人看不到、但送進模型的文字會包含的內容。"""

    kind: str          # 判定依據，如 white_text / html_comment / meta
    text: str
    context: str = ""  # 出現在哪個標籤

    def __str__(self) -> str:
        return f"[{self.kind}] {self.text[:80]}"


# 會被視為一筆獨立資料的區塊標籤。只有帶 data-* 屬性者才算，
# 否則整份文件的每個 div 都會被加上邊界，反而製造雜訊。
_BLOCK_TAGS = ("div", "section", "article", "li", "tr", "td")

BLOCK_OPEN = "【以下為應徵者提供的資料：{label}】"
BLOCK_CLOSE = "【{label} 的資料結束】"


@dataclass
class _Extractor(HTMLParser):
    """一次走訪同時產出「人看到的」與「機器讀到的」兩份文字。"""

    visible: list[str] = field(default_factory=list)
    machine: list[str] = field(default_factory=list)
    hidden: list[HiddenSpan] = field(default_factory=list)
    mark_blocks: bool = False  # 是否在可見文字裡保留資料區塊的邊界標記
    _hidden_depth: int = 0
    _hidden_kind: str = ""
    _tag_stack: list[str] = field(default_factory=list)
    _skip_depth: int = 0  # script/style 內容兩邊都不算文字
    _blocks: list[tuple[int, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    # -- HTMLParser 介面 --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._tag_stack.append(tag)

        if tag in ("script", "style"):
            self._skip_depth += 1
            return

        # meta 的 content 人看不到，但常被模型讀入
        if tag == "meta" and attr_map.get("content"):
            name = attr_map.get("name") or attr_map.get("property") or "meta"
            self.hidden.append(
                HiddenSpan(kind="meta", text=attr_map["content"], context=name)
            )
            self.machine.append(attr_map["content"])
            return

        # img 的 alt 同理
        if tag == "img" and attr_map.get("alt"):
            self.hidden.append(HiddenSpan(kind="alt", text=attr_map["alt"], context="img"))
            self.machine.append(attr_map["alt"])
            return

        # title 提示文字：畫面上不出現，要滑鼠停留才顯示，但擷取工具照讀。
        # 放在 return 之前，因為帶 title 的標籤本身可能還有正常的內文。
        if attr_map.get("title"):
            self.hidden.append(
                HiddenSpan(kind="title_attr", text=attr_map["title"], context=tag)
            )
            self.machine.append(attr_map["title"])

        if self.mark_blocks and tag in _BLOCK_TAGS:
            label = next(
                (v for k, v in attr_map.items() if k.startswith("data-") and v), ""
            )
            if label:
                # len(_tag_stack) 此時已含自己，記下來供 endtag 配對
                self._blocks.append((len(self._tag_stack), label))
                self.visible.append(BLOCK_OPEN.format(label=label))

        kind = self._hidden_kind_of(attr_map)
        if kind and self._hidden_depth == 0:
            self._hidden_depth = 1
            self._hidden_kind = kind
        elif self._hidden_depth:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
            # pop 之後長度少 1，故與記錄時的深度相差 1
            if self._blocks and self._blocks[-1][0] == len(self._tag_stack) + 1:
                _depth, label = self._blocks.pop()
                self.visible.append(BLOCK_CLOSE.format(label=label))
        if self._hidden_depth:
            self._hidden_depth -= 1
            if self._hidden_depth == 0:
                self._hidden_kind = ""

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or self._skip_depth:
            return
        self.machine.append(text)
        if self._hidden_depth:
            self.hidden.append(
                HiddenSpan(
                    kind=self._hidden_kind,
                    text=text,
                    context=self._tag_stack[-1] if self._tag_stack else "",
                )
            )
        else:
            self.visible.append(text)

    def handle_comment(self, data: str) -> None:
        text = data.strip()
        if text:
            self.hidden.append(HiddenSpan(kind="html_comment", text=text))
            self.machine.append(text)

    # -- 內部 --

    @staticmethod
    def _hidden_kind_of(attr_map: dict[str, str]) -> str:
        for attr in _INVISIBLE_ATTRS:
            if attr in attr_map:
                return "hidden_attr"
        style = attr_map.get("style", "")
        if not style:
            return ""
        for kind, pattern in _HIDDEN_STYLE_RULES:
            if pattern.search(style):
                return kind
        return ""


def _extract(html: str, mark_blocks: bool = False) -> _Extractor:
    parser = _Extractor(mark_blocks=mark_blocks)
    parser.feed(html)
    parser.close()
    return parser


def visible_text(html: str) -> str:
    """人在畫面上看得到的文字。"""
    return "\n".join(_extract(html).visible)


def machine_text(html: str) -> str:
    """模型讀進去的文字（含所有隱藏內容）。"""
    return "\n".join(_extract(html).machine)


def find_hidden(html: str) -> list[HiddenSpan]:
    """雙視角差集：人看不到、機器讀得到的內容。"""
    return _extract(html).hidden


def sanitize(html: str) -> tuple[str, list[HiddenSpan]]:
    """輸入淨化：回傳 (只含可見文字的版本, 被剝除的內容)。

    注意能力邊界：只擋得住「隱藏型」注入。寫在正文裡、人也看得到的
    「明文型」注入剝不掉 —— 這是設計上的限制，不是 bug。

    ⚠️ 實測（run #2，480 案例）發現這個做法有反效果：隱藏型的操控率
    全數歸零，明文型卻全數上升（S2 從 40% 升到 73%，S1 從 0% 升到 20%）。
    原因是剝掉標籤的同時也剝掉了「這段文字屬於某份履歷」的結構邊界，
    注入句變成文件末尾一則孤立的註記，讀起來更像給模型的指示。
    要保留邊界請改用 sanitize_with_boundaries。
    """
    parsed = _extract(html)
    return "\n".join(parsed.visible), parsed.hidden


def sanitize_with_boundaries(html: str) -> tuple[str, list[HiddenSpan]]:
    """淨化並保留資料區塊的邊界標記。

    與 sanitize 只差一個變因：同樣剝除隱藏內容，但在每個帶 data-* 屬性的
    區塊前後插入文字標記，讓「這段是誰提供的資料」在純文字裡仍然成立。
    """
    parsed = _extract(html, mark_blocks=True)
    return "\n".join(parsed.visible), parsed.hidden
