"""Stage 2「套用範本修改」的報告（純文字）與分析報告要用的命中清單。

報告是給人逐筆核對用的：每一筆規則修改都附上前後各一段，讓使用者不用
打開原檔就能判斷改得對不對（使用者明確要求前後文要給得比只有一行寬）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pattern.engine import LineChange, PatternResult

_TAIPEI = timezone(timedelta(hours=8))

# 分析報告送給 LLM 的前後文長度（字）。報告本身給完整段落，LLM 只需要
# 足夠判斷語意的範圍，截短可以讓一次呼叫塞進更多筆。
_LLM_CONTEXT_CHARS = 60


def _mark(text: str, spans: list[tuple[int, int]]) -> str:
    out = []
    cursor = 0
    for start, end in sorted(spans):
        out.append(text[cursor:start])
        out.append("【" + text[start:end] + "】")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _before_rules(change: LineChange) -> tuple[str, list[tuple[int, int]]]:
    """把規則替換倒回去，得到「去空格、轉標點之後、套規則之前」的那一行，
    以及每個被改片段在那一行裡的位置。"""
    out = []
    spans = []
    cursor = 0
    out_len = 0
    for hit in change.rule_hits:
        gap = change.final[cursor:hit.start]
        out.append(gap)
        out_len += len(gap)
        out.append(hit.source)
        spans.append((out_len, out_len + len(hit.source)))
        out_len += len(hit.source)
        cursor = hit.end
    out.append(change.final[cursor:])
    return "".join(out), spans


def _neighbors(result: PatternResult, para_no: int) -> tuple[str, str]:
    prev_text = result.paragraphs[para_no - 2] if para_no >= 2 else "（沒有前一段）"
    next_text = result.paragraphs[para_no] if 0 < para_no < len(result.paragraphs) else "（沒有後一段）"
    return prev_text, next_text


def _para_label(change: LineChange) -> str:
    return f"第 {change.para_no} 段" if change.para_no else "（處理後變成空行）"


def build_report(
    result: PatternResult,
    *,
    file_name: str | None,
    template_label: str,
    rule_order: list[str],
    targets: dict[str, str],
) -> str:
    lines: list[str] = []
    w = lines.append
    rule_changes = [c for c in result.changes if c.rule_hits]
    punct_changes = [c for c in result.changes if c.punct_converted]
    space_changes = [c for c in result.changes if c.spaces_removed]

    w("套用範本修改報告")
    w("=" * 40)
    w(f"檔案：{file_name or '（未命名）'}")
    w(f"範本：{template_label}")
    w(f"產生時間：{datetime.now(_TAIPEI):%Y-%m-%d %H:%M}（台灣時間）")
    w("")
    w("說明")
    w("- 段落編號＝下載成 docx 之後的段落順序（空行不算）。")
    w("- 【】標出被改的位置，只是報告裡的標記，文件裡沒有。")
    w("- 處理順序：先刪半形空格，再把半形標點轉全形，最後套用範本規則。")
    w("- 除了這三種修改，其他任何字元都沒有動。")
    w("")
    w(
        f"總計：範本規則修改 {result.rule_hits_total} 處（{len(rule_changes)} 段）；"
        f"半形標點轉全形 {result.punct_converted} 個（{len(punct_changes)} 段）；"
        f"刪除半形空格 {result.spaces_removed} 個（{len(space_changes)} 段）；"
        f"全文共 {len(result.paragraphs)} 段。"
    )
    if result.skipped_spaces or result.skipped_punct:
        w(
            f"刻意保留沒動的：夾在英文字母／數字之間的半形空格 {result.skipped_spaces} 個、"
            f"夾在數字之間的逗號或冒號 {result.skipped_punct} 個（例如 1,000、12:30）。"
        )
    w("")

    w("一、各規則命中次數")
    w("-" * 40)
    zero = []
    for source in rule_order:
        count = result.rule_counts.get(source, 0)
        if count:
            w(f"{count:>5} 次　{source} → {targets[source]}")
        else:
            zero.append(source)
    if not result.rule_hits_total:
        w("（沒有任何規則命中）")
    w("")
    w(f"命中 0 次的規則共 {len(zero)} 條：" + ("、".join(zero) if zero else "無"))
    w("")

    w("二、範本規則修改逐筆（依段落）")
    w("-" * 40)
    if not rule_changes:
        w("（無）")
    for change in rule_changes:
        before_text, before_spans = _before_rules(change)
        after_spans = [(h.start, h.end) for h in change.rule_hits]
        prev_text, next_text = _neighbors(result, change.para_no)
        summary = "；".join(f"{h.source} → {h.target}" for h in change.rule_hits)
        w(f"{_para_label(change)}　{summary}")
        w(f"  前一段：{prev_text}")
        w(f"  修改前：{_mark(before_text, before_spans)}")
        w(f"  修改後：{_mark(change.final, after_spans)}")
        w(f"  後一段：{next_text}")
        w("")

    w("三、半形標點轉全形（依段落）")
    w("-" * 40)
    if not punct_changes:
        w("（無）")
    else:
        counts = "、".join(f"「{ch}」{n} 個" for ch, n in sorted(result.punct_counts.items()))
        w(f"各標點：{counts}")
        w("")
        for change in punct_changes:
            spans = [(p, p + 1) for p in change.punct_positions]
            w(f"{_para_label(change)}（{change.punct_converted} 個）：{_mark(change.final, spans)}")
    w("")

    w("四、刪除的半形空格（依段落，␣ 是被刪掉的空格原本的位置）")
    w("-" * 40)
    if not space_changes:
        w("（無）")
    for change in space_changes:
        shown = _show_removed_spaces(change.original)
        w(f"{_para_label(change)}（{change.spaces_removed} 個）：{shown}")
    w("")
    return "\n".join(lines)


def _show_removed_spaces(original: str) -> str:
    """用跟 engine._remove_spaces 一樣的判斷，把會被刪掉的空格畫成 ␣。"""
    from pattern.engine import _ASCII_ALNUM, _SPACE_RUN

    def repl(match):
        start, end = match.span()
        before = original[start - 1:start]
        after = original[end:end + 1]
        if _ASCII_ALNUM.match(before) and _ASCII_ALNUM.match(after):
            return match.group(0)
        return "␣" * (end - start)

    return _SPACE_RUN.sub(repl, original)


def _clip(text: str, center_start: int, center_end: int, width: int) -> str:
    start = max(0, center_start - width)
    end = min(len(text), center_end + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def word_hits_for_analysis(result: PatternResult) -> list[dict]:
    """分析報告（LLM）要看的清單：只有範本規則造成的修改，不含標點與空格
    （那兩種是機械性的轉換，交給 LLM 只是浪費額度，使用者也明確說不要）。
    每一筆是一個「規則命中」，同一段有兩筆就是兩項。"""
    items = []
    for change in result.changes:
        if not change.rule_hits:
            continue
        prev_text, next_text = _neighbors(result, change.para_no)
        for hit in change.rule_hits:
            marked = change.final[:hit.start] + "【" + hit.target + "】" + change.final[hit.end:]
            items.append(
                {
                    "no": len(items) + 1,
                    "para_no": change.para_no,
                    "source": hit.source,
                    "target": hit.target,
                    "excerpt": _clip(marked, hit.start, hit.end + 2, _LLM_CONTEXT_CHARS),
                    "prev": prev_text[-_LLM_CONTEXT_CHARS:],
                    "next": next_text[:_LLM_CONTEXT_CHARS],
                }
            )
    return items
