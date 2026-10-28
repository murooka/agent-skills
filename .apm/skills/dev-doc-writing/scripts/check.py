#!/usr/bin/env python3
"""開発文書の、機械で決まるところだけを確かめる。

セクションの見出し、要求 ID、該当なし、未決定事項、上流への参照は、読まなくても決まる。
ここをコマンド 1 本で済ませ、サブエージェントには読まないと決まらないことだけを任せる。

usage:
  python3 check.py --file docs/prd/data-export.md --norm <スキル>/references/prd.md \
                   [--upstream docs/prd/initial-launch.md ...]
"""
import argparse
import os
import re
import sys

PREFIXES = r"UC|FR|TR|UF|FS|EC|AC|G|NFR|OQ"


def sections_of_norm(path):
    """セクション規範の表の 1 列目から、置くべき見出しを順に取り出す。"""
    rows = []
    for line in open(path, encoding="utf-8"):
        if not line.startswith("|") or line.startswith("|---") or "| ---" in line:
            continue
        cell = line.split("|")[1].strip()
        if cell and cell not in ("セクション", "文書", "下流の項目", "上流のセクション", "中身", "語", "判定", "並び"):
            rows.append(cell)
    # 表が複数あるファイルでも、最初の表(セクションの表)だけを使う
    return rows


def headings_of(text):
    return [l[3:].strip() for l in text.split("\n") if l.startswith("## ")]


def ids_in(text):
    """この文書が振っている要求 ID。項目の先頭の **FR-1** 形式を拾う。"""
    found = {}
    for m in re.finditer(r"\*\*(" + PREFIXES + r")-(\d+)\*\*", text):
        found.setdefault(m.group(1), []).append(int(m.group(2)))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="確かめる開発文書")
    ap.add_argument("--norm", help="対応するセクション規範(prd.md / product-spec.md / design-doc.md)")
    ap.add_argument("--upstream", action="append", default=[], help="上流文書(繰り返し指定可)")
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8").read()
    lines = text.split("\n")
    problems, notes = [], []

    # 1. 見出しの欠けと順序
    if args.norm:
        want = sections_of_norm(args.norm)
        have = headings_of(text)
        missing = [s for s in want if s not in have]
        extra = [s for s in have if s not in want]
        if missing:
            problems.append(f"見出しが足りない: {', '.join(missing)}")
        if extra:
            notes.append(f"規範に無い見出し: {', '.join(extra)}")
        order = [s for s in have if s in want]
        if order != [s for s in want if s in have]:
            problems.append(f"見出しの順序が規範と違う: {' → '.join(order)}")

    # 2. 要求 ID の重複
    for prefix, nums in sorted(ids_in(text).items()):
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            problems.append(f"{prefix} の番号が重複: {', '.join(f'{prefix}-{n}' for n in dup)}")
        gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        if gaps:
            notes.append(f"{prefix} の欠番: {', '.join(f'{prefix}-{n}' for n in gaps)}"
                         "(改訂で消したなら正しい。新規作成なら振り直す)")

    # 3. 該当なしに理由が続いているか
    for i, line in enumerate(lines):
        if "該当なし" in line:
            body = line.split("該当なし", 1)[1].lstrip("：: ")
            if not body.strip() and not (i + 1 < len(lines) and lines[i + 1].strip()):
                problems.append(f"{args.file}:{i+1} 該当なしに理由がない")

    # 4. 未決定を示す語が、未決定事項に紐づいているか
    for i, line in enumerate(lines):
        if re.search(r"未定|検討中|TBD", line) and not re.search(r"OQ-\d+", line):
            problems.append(f"{args.file}:{i+1} 未定/検討中/TBD があるのに OQ の参照がない: {line.strip()[:60]}")

    # 5. 上流への参照が実在するか
    refs = re.findall(r"`([^`]+\.md)#(?:user-content-)?([a-zA-Z]{1,3}-\d+)`", text)
    refs += [(p, i) for p, i in re.findall(r"\(([^)]+\.md)#(?:user-content-)?([a-zA-Z]{1,3}-\d+)\)", text)]
    upstream = {}
    for path in args.upstream:
        upstream[os.path.basename(path)] = open(path, encoding="utf-8").read()
    seen = set()
    for path, rid in refs:
        key = (os.path.basename(path), rid.upper())
        if key in seen:
            continue
        seen.add(key)
        body = upstream.get(key[0])
        if body is None:
            notes.append(f"参照先を渡されていないので確かめられない: {path}#{rid}")
            continue
        if not re.search(r"\*\*" + re.escape(key[1]) + r"\*\*|id=\"" + re.escape(rid.lower()) + r"\"", body):
            problems.append(f"参照先に実在しない: {path}#{rid}")

    print(f"# 機械チェック: {args.file}")
    print(f"  見出し {len(headings_of(text))} 個 / 要求 ID "
          f"{sum(len(v) for v in ids_in(text).values())} 個 / 上流参照 {len(seen)} 個")
    for p in problems:
        print(f"!! {p}")
    for n in notes:
        print(f"-- {n}")
    print(f"\n{len(problems)} 件の問題、{len(notes)} 件の注意")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
