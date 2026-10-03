"""Tokenising SQL splitter.

Splits a script into individual statements while correctly handling
semicolons inside string literals, quoted identifiers, line/block
comments and CREATE TRIGGER ... BEGIN ... END; bodies (including
nested CASE ... END).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Token:
    kind: str  # "word", "string", "qident", "comment", "semi", "other"
    text: str


def tokenize(script: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if script.startswith("--", i):
            j = script.find("\n", i)
            j = n if j == -1 else j
            tokens.append(Token("comment", script[i:j]))
            i = j
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i + 2)
            j = n if j == -1 else j + 2
            tokens.append(Token("comment", script[i:j]))
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if script[j] == "'":
                    if j + 1 < n and script[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            tokens.append(Token("string", script[i:j]))
            i = j
            continue
        if ch in ('"', "`"):
            j = script.find(ch, i + 1)
            j = n if j == -1 else j + 1
            tokens.append(Token("qident", script[i:j]))
            i = j
            continue
        if ch == "[":
            j = script.find("]", i + 1)
            j = n if j == -1 else j + 1
            tokens.append(Token("qident", script[i:j]))
            i = j
            continue
        if ch == ";":
            tokens.append(Token("semi", ";"))
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (script[j].isalnum() or script[j] == "_"):
                j += 1
            tokens.append(Token("word", script[i:j]))
            i = j
            continue
        tokens.append(Token("other", ch))
        i += 1
    return tokens


def split_statements(script: str) -> list[str]:
    """Split a script into statements, keeping trigger bodies intact."""
    tokens = tokenize(script)
    statements: list[str] = []
    current: list[Token] = []
    depth = 0          # BEGIN/CASE ... END nesting inside a trigger body
    in_trigger = False
    seen_words: list[str] = []

    def flush():
        nonlocal current, in_trigger, seen_words, depth
        stmt = " ".join(
            t.text + chr(10) if t.kind == "comment" else t.text
            for t in current).strip()
        if stmt and any(t.kind != "comment" for t in current):
            statements.append(stmt)
        current = []
        in_trigger = False
        seen_words = []
        depth = 0

    for t in tokens:
        if t.kind == "semi":
            if in_trigger and depth > 0:
                current.append(t)
            else:
                flush()
            continue
        current.append(t)
        if t.kind == "word":
            upper = t.text.upper()
            seen_words.append(upper)
            if len(seen_words) <= 3 and "TRIGGER" in seen_words[:3]:
                in_trigger = True
            if in_trigger:
                if upper in ("BEGIN", "CASE"):
                    depth += 1
                elif upper == "END":
                    depth = max(0, depth - 1)
    flush()
    return statements


def first_keyword(statement: str) -> str | None:
    for t in tokenize(statement):
        if t.kind == "comment":
            continue
        if t.kind == "word":
            return t.text.upper()
        return None
    return None
