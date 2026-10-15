"""Stage 2「套用範本修改」的範本格式：解析、檢查、編譯成比對用的 regex。

範本是純文字，一行一條規則，寫成「修改前 → 修改後」（箭頭是 U+2192）：

    盡管 → 儘管
    # 井字號開頭的行是註解，空行會略過

這份解析邏輯在前端有一份對應的 JavaScript 版本（zh-cn-to-tw-web 的
js/features/pattern-template.js），讓使用者選個人範本檔案的當下就能看到
錯誤、不必等網路。兩邊的規則必須一致；後端這份是權威——前端檢查過的
範本送上來，這裡還是會再檢查一次。

會讓整份範本被拒絕的錯誤（errors）：
- 這一行不是剛好一個「→」
- 「→」左邊或右邊是空的
- 規則裡有空白（套用前會先刪掉半形空格，含空白的規則永遠對不上）
- 同一個「修改前」對到兩個不同的「修改後」（不知道該聽哪一條）
- 規則太多或範本太大

只提醒、不擋的狀況（warnings）：完全重複的規則（只留一條）、修改前後
一樣的規則（略過）、只有標點的規則（標點本來就會自動轉全形，這條多餘）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ARROW = "→"  # →

# Stage 2 一定會做的半形轉全形標點（不受範本影響，見 engine.py）。放在這裡
# 是因為範本檢查也要知道「哪些規則已經被自動處理掉了」。
PUNCT_MAP = {
    ",": "，",  # ，
    "!": "！",  # ！
    "?": "？",  # ？
    ":": "：",  # ：
    ";": "；",  # ；
    "(": "（",  # （
    ")": "）",  # ）
}

MAX_RULES = 5000
MAX_TEMPLATE_CHARS = 500_000

_WHITESPACE = re.compile(r"\s")


@dataclass(frozen=True)
class Rule:
    source: str
    target: str
    line_no: int


@dataclass
class ParsedTemplate:
    rules: list[Rule] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_template(text: str) -> ParsedTemplate:
    result = ParsedTemplate()
    if len(text) > MAX_TEMPLATE_CHARS:
        result.errors.append((0, f"範本太大（超過 {MAX_TEMPLATE_CHARS:,} 字）"))
        return result

    text = text.lstrip("\ufeff")
    seen: dict[str, Rule] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count(ARROW) != 1:
            result.errors.append((line_no, f"每一行要剛好有一個「{ARROW}」：{line}"))
            continue
        source, target = (part.strip() for part in line.split(ARROW))
        if not source or not target:
            result.errors.append((line_no, f"「{ARROW}」左右兩邊都要有字：{line}"))
            continue
        if _WHITESPACE.search(source) or _WHITESPACE.search(target):
            result.errors.append(
                (line_no, f"規則裡不能有空白（套用前會先刪掉半形空格，含空白的規則永遠對不上）：{line}")
            )
            continue
        if source == target:
            result.warnings.append((line_no, f"修改前後一樣，略過：{line}"))
            continue
        previous = seen.get(source)
        if previous is not None:
            if previous.target == target:
                result.warnings.append((line_no, f"跟第 {previous.line_no} 行重複，只留一條：{line}"))
            else:
                result.errors.append(
                    (
                        line_no,
                        f"「{source}」在第 {previous.line_no} 行要改成「{previous.target}」，"
                        f"這裡又要改成「{target}」，不知道該聽哪一條",
                    )
                )
            continue
        if all(ch in PUNCT_MAP for ch in source) and target == "".join(PUNCT_MAP[ch] for ch in source):
            result.warnings.append((line_no, f"標點本來就會自動轉全形，這條可以刪掉：{line}"))
        rule = Rule(source, target, line_no)
        seen[source] = rule
        result.rules.append(rule)

    if len(result.rules) > MAX_RULES:
        result.errors.append((0, f"規則太多（{len(result.rules)} 條，上限 {MAX_RULES} 條）"))
    return result


def format_issues(issues: list[tuple[int, str]]) -> list[dict]:
    return [{"line": line_no, "message": message} for line_no, message in issues]


@dataclass
class CompiledTemplate:
    rules: list[Rule]
    regex: re.Pattern | None
    targets: dict[str, str]


def compile_rules(rules: list[Rule]) -> CompiledTemplate:
    """把規則編成一個 regex，一次掃過整段文字（單趟替換）。

    - 長的規則排前面：「依依不舍」要先於「不舍」被比對到，不然長規則永遠
      輪不到。
    - 單趟：A→B 改完的結果不會再被另一條規則改一次。
    - 「修改後」包含「修改前」的規則（例如 石翁 → 石翁仲）自動加上前後
      排除條件：原文已經是「石翁仲」的地方不會再被改成「石翁仲仲」，
      同一份文字重跑也不會越改越長。
    """
    if not rules:
        return CompiledTemplate(rules=[], regex=None, targets={})
    parts = []
    for rule in sorted(rules, key=lambda r: len(r.source), reverse=True):
        piece = re.escape(rule.source)
        index = rule.target.find(rule.source)
        if index >= 0:
            before = rule.target[:index]
            after = rule.target[index + len(rule.source):]
            if before:
                piece = f"(?<!{re.escape(before)})" + piece
            if after:
                piece = piece + f"(?!{re.escape(after)})"
        parts.append(piece)
    return CompiledTemplate(
        rules=list(rules),
        regex=re.compile("|".join(parts)),
        targets={rule.source: rule.target for rule in rules},
    )
