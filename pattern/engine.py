"""Stage 2「套用範本修改」的套用邏輯：純程式、確定性，不呼叫任何 LLM。

處理順序固定，每一行（以 \\n 分隔）各自處理：

1. 刪除半形空格。夾在兩個英文字母／數字中間的不刪（例如英文片語），
   避免把英文字黏在一起。
2. 半形標點轉全形（template.PUNCT_MAP 那七個）。逗號、冒號夾在兩個數字
   中間的不轉（1,000、12:30 這種）。這一步原本是 Stage 3 校對的 LLM 前置
   處理在做，2.0 起改成在這裡用程式做掉，LLM 少一件事。
3. 套用範本規則（template.compile_rules 編好的 regex，單趟、長的優先）。

「除了規則改掉的東西，一個字都不能少」是這個功能的底線：除了上面三種
修改，其他字元（包括換行、空行、全形空格、定位字元）原樣保留。run() 最後
會自己驗證一次，對不上就直接拋例外，不讓有問題的結果流出去。

段落編號：輸出文字裡「非空白的行」依序編號，跟下載成 docx 之後的段落
順序一致（docx 匯出會略過空行，見 output_utils/docx_export.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pattern.template import PUNCT_MAP, CompiledTemplate

_SPACE_RUN = re.compile(r" +")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")
_DIGIT = re.compile(r"[0-9]")
_PUNCT_CHARS = re.compile("[" + re.escape("".join(PUNCT_MAP)) + "]")


@dataclass
class RuleHit:
    source: str
    target: str
    start: int  # 在處理完的這一行裡的位置（替換後）
    end: int


@dataclass
class LineChange:
    line_index: int  # 在整份文字 split("\n") 裡的位置
    para_no: int  # 輸出文字的段落編號（非空行從 1 起算）；處理後變空行的是 0
    original: str
    final: str
    spaces_removed: int = 0
    punct_converted: int = 0
    punct_positions: list[int] = field(default_factory=list)  # 在處理完的這一行裡
    rule_hits: list[RuleHit] = field(default_factory=list)


@dataclass
class PatternResult:
    text: str
    changes: list[LineChange]
    paragraphs: list[str]  # 輸出文字的段落（非空行），index 0 = 第 1 段
    rule_counts: dict[str, int]
    spaces_removed: int
    punct_converted: int
    punct_counts: dict[str, int]
    skipped_spaces: int  # 夾在英數之間而保留的半形空格
    skipped_punct: int  # 夾在數字之間而保留的逗號／冒號

    @property
    def rule_hits_total(self) -> int:
        return sum(self.rule_counts.values())


def _remove_spaces(line: str) -> tuple[str, int, int]:
    removed = 0
    kept = 0

    def repl(match: re.Match) -> str:
        nonlocal removed, kept
        start, end = match.span()
        before = line[start - 1:start]
        after = line[end:end + 1]
        if _ASCII_ALNUM.match(before) and _ASCII_ALNUM.match(after):
            kept += end - start
            return match.group(0)
        removed += end - start
        return ""

    return _SPACE_RUN.sub(repl, line), removed, kept


def _convert_punct(line: str, counts: dict[str, int]) -> tuple[str, list[int], int]:
    out = []
    positions = []
    skipped = 0
    for i, ch in enumerate(line):
        if ch in PUNCT_MAP:
            if ch in ",:" and _DIGIT.match(line[i - 1:i]) and _DIGIT.match(line[i + 1:i + 2]):
                skipped += 1
                out.append(ch)
                continue
            out.append(PUNCT_MAP[ch])
            positions.append(i)
            counts[ch] = counts.get(ch, 0) + 1
        else:
            out.append(ch)
    return "".join(out), positions, skipped


def _apply_rules(line: str, compiled: CompiledTemplate) -> tuple[str, list[RuleHit]]:
    if compiled.regex is None:
        return line, []
    out = []
    hits = []
    cursor = 0
    out_len = 0
    for match in compiled.regex.finditer(line):
        gap = line[cursor:match.start()]
        out.append(gap)
        out_len += len(gap)
        source = match.group(0)
        target = compiled.targets[source]
        out.append(target)
        hits.append(RuleHit(source, target, out_len, out_len + len(target)))
        out_len += len(target)
        cursor = match.end()
    out.append(line[cursor:])
    return "".join(out), hits


def run(text: str, compiled: CompiledTemplate) -> PatternResult:
    rule_counts = {rule.source: 0 for rule in compiled.rules}
    punct_counts: dict[str, int] = {}
    lines = text.split("\n")
    finals = []
    changes = []
    total_spaces = total_punct = skipped_spaces = skipped_punct = 0

    for index, original in enumerate(lines):
        line, spaces, kept = _remove_spaces(original)
        line, punct_positions, skipped = _convert_punct(line, punct_counts)
        converted = len(punct_positions)
        before_rules = line
        line, hits = _apply_rules(line, compiled)
        # 規則替換會改變長度，標點位置要換算成替換後的位置，報告標記才會對
        if hits and punct_positions:
            punct_positions = _shift_positions(punct_positions, before_rules, hits)
        finals.append(line)
        total_spaces += spaces
        total_punct += converted
        skipped_spaces += kept
        skipped_punct += skipped
        for hit in hits:
            rule_counts[hit.source] += 1
        if line != original:
            changes.append(
                LineChange(
                    line_index=index,
                    para_no=0,
                    original=original,
                    final=line,
                    spaces_removed=spaces,
                    punct_converted=converted,
                    punct_positions=punct_positions,
                    rule_hits=hits,
                )
            )

    result_text = "\n".join(finals)
    para_no_of_line = {}
    paragraphs = []
    for index, line in enumerate(finals):
        if line.strip():
            paragraphs.append(line)
            para_no_of_line[index] = len(paragraphs)
    for change in changes:
        change.para_no = para_no_of_line.get(change.line_index, 0)

    _verify(text, result_text, compiled)
    return PatternResult(
        text=result_text,
        changes=changes,
        paragraphs=paragraphs,
        rule_counts=rule_counts,
        spaces_removed=total_spaces,
        punct_converted=total_punct,
        punct_counts=punct_counts,
        skipped_spaces=skipped_spaces,
        skipped_punct=skipped_punct,
    )


def _shift_positions(positions: list[int], line: str, hits: list[RuleHit]) -> list[int]:
    """把替換前的位置換算成替換後的位置。標點字元本身不會被規則改到
    （規則不含半形標點以外的東西時才可能重疊，重疊的就略過不標）。"""
    shifted = []
    for pos in positions:
        delta = 0
        inside = False
        for hit in hits:
            # hit.start/end 是替換後的位置，先換回替換前的位置來比較
            src_start = hit.start - delta
            src_end = src_start + len(hit.source)
            if pos < src_start:
                break
            if pos < src_end:
                inside = True
                break
            delta += len(hit.target) - len(hit.source)
        if not inside:
            shifted.append(pos + delta)
    return shifted


def _verify(original: str, result: str, compiled: CompiledTemplate) -> None:
    """獨立重算一次，確認結果只包含預期的三種修改。

    做法跟 run() 刻意不同：直接對整份文字用最單純的方式重做一次三個步驟，
    再跟 run() 逐行處理的結果比對。兩種做法對不上，代表逐行處理、位置
    換算之類的地方有 bug，寧可整個失敗也不要回傳有問題的結果。
    另外確認：把所有半形空格拿掉、再把標點與規則換回去比較之前，行數
    必須完全一樣（換行一個都不能少）。"""
    if original.count("\n") != result.count("\n"):
        raise RuntimeError("套用範本後行數改變了，結果不予採用")
    expected_lines = []
    for line in original.split("\n"):
        line = _remove_spaces(line)[0]
        line = _convert_punct(line, {})[0]
        if compiled.regex is not None:
            line = compiled.regex.sub(lambda m: compiled.targets[m.group(0)], line)
        expected_lines.append(line)
    if "\n".join(expected_lines) != result:
        raise RuntimeError("套用範本的結果跟獨立重算的結果不一致，結果不予採用")
