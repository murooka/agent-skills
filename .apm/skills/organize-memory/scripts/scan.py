#!/usr/bin/env python3
"""Claude Code auto-memory の棚卸しスキャナ。

機械検出できる問題(フォーマット世代・インデックス不整合・予算消費・行数超過・
プロジェクト間の同名ファイル・古い mtime)を報告する。内容の重複・陳腐化・
昇格判定など中身の解釈が要るものは扱わない(エージェントの仕事)。

usage:
  python3 scan.py                            # ~/.claude/projects 全体
  python3 scan.py --project <slug>           # 単一プロジェクト
  python3 scan.py --root <path>              # ルート差し替え(テスト用)
  python3 scan.py --json out.json            # 機械可読出力も保存
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

INDEX_LINE_BUDGET = 200
INDEX_BYTE_BUDGET = 25 * 1024
ONE_FACT_SUSPECT_LINES = 40   # これを超えたら 1 ファイル 1 事実違反の疑い
STALE_DAYS = 90               # これより古い mtime は鮮度確認の候補

KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.md$")


def parse_frontmatter(text):
    """frontmatter を素朴にパースする。(fields, has_frontmatter) を返す。

    fields: name, description, type_top, type_meta, modified
    YAML パーサに頼らず、実地の 2 世代 + 逸脱形をカバーする最小限の解析。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    fields = {}
    in_metadata = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if re.match(r"^metadata:\s*$", line):
            in_metadata = True
            continue
        m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if m:  # トップレベルキー
            in_metadata = False
            key, val = m.group(1), m.group(2).strip().strip('"')
            if key in ("name", "description", "type", "modified"):
                fields["type_top" if key == "type" else key] = val
            continue
        m = re.match(r"^\s+(\w[\w-]*):\s*(.*)$", line)
        if m and in_metadata:
            key, val = m.group(1), m.group(2).strip().strip('"')
            if key == "type":
                fields["type_meta"] = val
            elif key == "modified":
                fields["modified"] = val
    return fields, True


def scan_file(path: Path, indexed: bool, now: float):
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, has_fm = parse_frontmatter(text)
    lines = text.count("\n") + 1
    age_days = int((now - path.stat().st_mtime) / 86400)
    flags = []
    if not has_fm:
        flags.append("no-frontmatter")
    else:
        if "type_top" in fields and "type_meta" not in fields:
            flags.append("legacy-top-level-type")
        if "type_top" not in fields and "type_meta" not in fields:
            flags.append("missing-type")
    if not KEBAB_RE.match(path.name):
        flags.append("legacy-filename")
    if lines > ONE_FACT_SUSPECT_LINES:
        flags.append(f"oversized({lines}L)")
    if not indexed:
        flags.append("not-indexed")
    if age_days > STALE_DAYS:
        flags.append(f"stale-mtime({age_days}d)")
    return {
        "file": path.name,
        "type": fields.get("type_meta") or fields.get("type_top"),
        "lines": lines,
        "bytes": path.stat().st_size,
        "mtime": time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)),
        "flags": flags,
    }


def scan_project(mem_dir: Path, now: float):
    index_path = mem_dir / "MEMORY.md"
    index_text = index_path.read_text(encoding="utf-8", errors="replace") if index_path.exists() else ""
    index_refs = set(re.findall(r"\((?:\./)?([^)/]+\.md)\)", index_text))
    files = sorted(p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md")
    file_reports = [scan_file(p, p.name in index_refs, now) for p in files]
    existing = {p.name for p in files}
    index_lines = index_text.count("\n") + 1 if index_text else 0
    index_bytes = len(index_text.encode("utf-8"))
    report = {
        "slug": mem_dir.parent.name,
        "files": file_reports,
        "index": {
            "exists": index_path.exists(),
            "lines": index_lines,
            "bytes": index_bytes,
            "line_budget_pct": round(100 * index_lines / INDEX_LINE_BUDGET),
            "byte_budget_pct": round(100 * index_bytes / INDEX_BYTE_BUDGET),
            "missing_refs": sorted(index_refs - existing),
        },
    }
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--project", help="単一プロジェクトのスラグ(ディレクトリ名)")
    ap.add_argument("--json", help="機械可読出力の保存先")
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root))
    if not root.is_dir():
        sys.exit(f"root が存在しない: {root}")
    now = time.time()

    mem_dirs = sorted(root.glob("*/memory"))
    if args.project:
        mem_dirs = [d for d in mem_dirs if d.parent.name == args.project]
        if not mem_dirs:
            sys.exit(f"memory ディレクトリが見つからない: {root}/{args.project}/memory")

    reports = [scan_project(d, now) for d in mem_dirs]

    # プロジェクト間の同名ファイル(横断モードの重複候補)
    by_name = {}
    for r in reports:
        for f in r["files"]:
            by_name.setdefault(f["file"], []).append(r["slug"])
    cross_dupes = {n: slugs for n, slugs in by_name.items() if len(slugs) > 1}

    # ---- 人間可読レポート ----
    for r in reports:
        idx = r["index"]
        print(f"## {r['slug']}")
        if not idx["exists"]:
            print("  !! MEMORY.md がない")
        else:
            print(f"  index: {idx['lines']} 行 ({idx['line_budget_pct']}%) / "
                  f"{idx['bytes']}B ({idx['byte_budget_pct']}%)  予算 {INDEX_LINE_BUDGET} 行 / 25KB")
        for ref in idx["missing_refs"]:
            print(f"  !! 索引にあるが実体がない: {ref}")
        for f in r["files"]:
            flag_str = " ".join(f["flags"]) if f["flags"] else "ok"
            print(f"  {f['file']:<40} type={f['type'] or 'NONE':<10} "
                  f"{f['lines']:>4}L  mtime={f['mtime']}  {flag_str}")
        print()

    if cross_dupes:
        print("## プロジェクト間の同名ファイル(重複・昇格の候補)")
        for name, slugs in sorted(cross_dupes.items()):
            print(f"  {name}: {', '.join(slugs)}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"projects": reports, "cross_project_same_name": cross_dupes},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")


if __name__ == "__main__":
    main()
