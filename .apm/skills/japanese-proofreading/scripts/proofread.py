#!/usr/bin/env python3
"""日本語の推敲を Gemini (agy CLI) に投げ、ファイルを直接書き換えさせる。

agy は --mode accept-edits で編集を自動承認し、--add-dir で加えたディレクトリの中だけを
読み書きできる。外は読み取りも拒否される。そこで対象ファイルを含むなるべく狭いディレクトリ
だけを渡し、書き換えの前後でディレクトリ全体のハッシュを比べて、対象以外が変わっていないかを
確かめる。

usage:
  python3 proofread.py --file PATH [--file PATH ...] [--range 12-48]
                       [--preserve sentences|headings|conclusion|facts]
                       [--context-dir DIR] [--hint TEXT] [--output report.json]
                       [--model gemini-3.8-flash-medium] [--timeout 300s] [--attempts 2]
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

INSTRUCTION = """次のファイルを推敲してください。

原文は AI が生成した日本語です。次の癖が残っていることが多いので、読み手が一度で理解できる文章に直してください。

- その分野の技術者が使わない語に言い換えられている（「バージョン」が「版」、「ツール」が「道具」など）
- 構成が独特である。否定や前置きから入る、不要な比較をする、結論が後ろに回る
- どの段落も同じ重さで書かれていて、読み手がどこを重点的に読めばよいか分からない
- 文脈をすべて書き残そうとして、量が多い

どこをどう直すかは、あなたの判断に任せます。

どの場合でも守ること:
- 事実と違うことを書かない。確かめていないことを断定しない。
- 数値・単位・日付・URL・識別子・型名・関数名・カラム名は、原文にあるものをそのまま使う。読んで確かめていないものを新しく持ち込まない。半角文字を全角に変えない。
- コード、設定値、コードブロックの中身は変えない。直すのは日本語の文章だけにする。
- 文体（である調 / ですます調）は原文に合わせる。
"""

# 不変条件。どこまで書き換えてよいかの目盛りで、厳しい順に並ぶ。
# 凍結だけを並べると、書かれていない範囲まで保守的に扱われるので、解放も必ず対で書く。
PRESERVE = {
    "sentences": """
不変条件:
- 保つもの: 文の並びと切り方、段落の構成、見出し、主張、事実。
- 変えてよいもの: 語の選び方、接続詞、助詞、読点の位置。
- 文を分けたり、つないだり、順序を入れ替えたりしない。語と接続の選び直しだけで読みやすくする。
""",
    "headings": """
不変条件:
- 保つもの: 見出しの文言とその並び、各節が扱っている主張、事実。
- 変えてよいもの: 節の中の段落構成、文の切り方と順序、語の選び方、接続詞、言い足しと削り。
- 上に挙げた以外に制限はない。読みやすくなるなら、節の中は自由に組み替えてよい。冗長なところは削ってよい。
""",
    "conclusion": """
不変条件:
- 保つもの: 最終的な主張（この文章が読み手に納得させたい結論）と、事実。
- 変えてよいもの: 見出し、節の並び、論点の順序と取捨、段落構成、文の切り方、語の選び方。
- 結論に至る道筋は、読み手が納得しやすい順に組み替えてよい。上に挙げた以外に制限はない。
""",
    "facts": """
不変条件:
- 保つもの: 事実だけ。
- 変えてよいもの: 何を書くか、どこまで書くか、順序、構成、文、語。書く必要のない内容は削ってよく、足りない説明は足してよい。
- 上に挙げた以外に制限はない。
""",
}
PRESERVE_LEVELS = list(PRESERVE)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".terraform", ".venv", "dist", "build"}
BACKUP_LIMIT_BYTES = 50 * 1024 * 1024
BACKUP_LIMIT_FILES = 3000

# インラインコードと数値。増えた分は補足、減った分は事実の欠落の候補になる。
TOKEN = re.compile(r"`[^`\n]+`|(?<![\w`])\d+(?:[.,]\d+)*")


def walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                yield path


def snapshot(root, backup_dir=None):
    """ディレクトリの中身のハッシュを取り、backup_dir があれば控えも取る。

    対象以外のファイルが書き換わったときに気づけるようにするため。控えがあれば戻せる。
    """
    paths = list(walk_files(root))
    total = sum(os.path.getsize(p) for p in paths)
    copy = backup_dir is not None and len(paths) <= BACKUP_LIMIT_FILES and total <= BACKUP_LIMIT_BYTES
    hashes = {}
    for path in paths:
        with open(path, "rb") as f:
            data = f.read()
        hashes[path] = hashlib.sha256(data).hexdigest()
        if copy:
            dest = os.path.join(backup_dir, os.path.relpath(path, root))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
    return hashes, copy


def build_prompt(targets, preserve, hint):
    blocks = [INSTRUCTION, PRESERVE[preserve], "対象:"]
    for path, rng in targets:
        blocks.append(f"- {path}" + (f"（{rng} 行目）" if rng else "（ファイル全体）"))
    if hint:
        blocks.append(f"\n補足: {hint}")
    blocks.append(
        "\nファイルを直接書き換えてください。対象に挙げたファイルの、対象に挙げた範囲だけを書き換え、"
        "ほかのファイルには触れないでください。\n"
        "書き換えたら、何をなぜ変えたかを 3 行以内で答えてください。")
    return "\n".join(blocks)


def run_agy(prompt, model, timeout, context_dir):
    cmd = ["agy", "--model", model, "--print-timeout", timeout, "--output-format", "json",
           "--mode", "accept-edits", "--add-dir", context_dir, "-p", prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"agy が異常終了した (exit {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout).strip()[:300]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"agy の出力を JSON として読めない: {proc.stdout[:300]}")
    if payload.get("status") != "SUCCESS":
        raise RuntimeError(f"agy が失敗を返した (status={payload.get('status')}): "
                           f"{str(payload.get('response'))[:300]}")
    denied = payload.get("denied_actions") or []
    if denied:
        names = ", ".join(sorted({d.get("display_name") or d.get("action", "?") for d in denied}))
        note = next((l for l in (proc.stderr or "").splitlines() if l.strip()), "")
        raise RuntimeError(f"agy が操作を拒否した: {names}"
                           f"(--context-dir の外を触ろうとした可能性がある)。{note[:200]}")
    return str(payload.get("response") or ""), payload.get("duration_seconds", 0.0)


def headings_of(text):
    return [l.strip() for l in text.split("\n") if re.match(r"^#{1,6} ", l)]


def blocks_of(text):
    out, buf, fence = [], [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
            buf.append(line)
            continue
        if not fence and not line.strip():
            if buf:
                out.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out


def measure(before, after, preserve):
    """機械で決まることだけを数える。主張と事実は、書き手が読んで確かめる。"""
    bt = collections.Counter(TOKEN.findall(before))
    at = collections.Counter(TOKEN.findall(after))
    violations = []
    if preserve in ("sentences", "headings") and headings_of(before) != headings_of(after):
        violations.append(f"見出しが変わった: {headings_of(before)} → {headings_of(after)}")
    if preserve == "sentences":
        if len(blocks_of(before)) != len(blocks_of(after)):
            violations.append(f"段落の数が変わった: {len(blocks_of(before))} → {len(blocks_of(after))}")
        if before.count("。") != after.count("。"):
            violations.append(f"文の数が変わった: {before.count('。')} → {after.count('。')}")
    return {
        "chars_before": len(before), "chars_after": len(after),
        "lines_before": before.count("\n") + 1, "lines_after": after.count("\n") + 1,
        "added_tokens": sorted((at - bt).elements()),
        "removed_tokens": sorted((bt - at).elements()),
        "invariant_violations": violations,
    }


def resolve_context_dir(raw, files):
    dirs = [os.path.dirname(f) for f in files]
    directory = os.path.realpath(raw) if raw else os.path.commonpath(dirs)
    home = os.path.realpath(os.path.expanduser("~"))
    if not os.path.isdir(directory):
        sys.exit(f"--context-dir が見つからない: {raw}")
    if directory in (home, os.path.sep):
        sys.exit(f"--context-dir が広すぎる: {directory}。対象を含む、なるべく狭いディレクトリを指定する。")
    for f in files:
        if os.path.commonpath([f, directory]) != directory:
            sys.exit(f"--context-dir の外に対象がある: {f}")
    return directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", action="append", required=True, help="推敲するファイル(繰り返し指定可)")
    parser.add_argument("--range", action="append", default=[],
                        help="対象の行範囲(例: 12-48)。--file と同じ順で対応させる。省略するとファイル全体")
    parser.add_argument("--preserve", choices=PRESERVE_LEVELS, default="headings",
                        help="どこまで書き換えてよいか。sentences=語と接続詞だけ / headings=節の中は自由(既定) / "
                             "conclusion=論の組み立ても自由 / facts=内容の取捨も自由")
    parser.add_argument("--context-dir", help="agy に読み書きを許すディレクトリ。既定は対象ファイルの共通の親")
    parser.add_argument("--hint", help="ファイルを読んでもわからない文脈(既存表記、読み手など)")
    parser.add_argument("--output", help="結果 JSON の書き出し先")
    parser.add_argument("--model", default="gemini-3.8-flash-medium")
    parser.add_argument("--timeout", default="300s", help="agy 1 回あたりの応答待ち上限")
    parser.add_argument("--attempts", type=int, default=2, help="失敗したときの試行回数")
    args = parser.parse_args()

    if shutil.which("agy") is None:
        sys.exit("agy が見つからない。Antigravity CLI がインストールされているか確認する。")

    files = []
    for raw in args.file:
        path = os.path.realpath(raw)
        if not os.path.isfile(path):
            sys.exit(f"--file が見つからない: {raw}")
        files.append(path)
    if args.range and len(args.range) != len(files):
        sys.exit("--range を渡すなら、--file と同じ数だけ渡す")
    ranges = args.range or [None] * len(files)
    context_dir = resolve_context_dir(args.context_dir, files)

    backup_dir = os.environ.get("JP_PROOFREAD_BACKUP") or tempfile.mkdtemp(prefix="jp-proofread-")
    os.makedirs(backup_dir, exist_ok=True)
    before_hashes, copied = snapshot(context_dir, backup_dir)
    before = {p: open(p, encoding="utf-8").read() for p in files}

    started = time.time()
    prompt = build_prompt(list(zip(files, ranges)), args.preserve, args.hint)
    response, gemini_seconds, error = "", 0.0, None
    for attempt in range(1, args.attempts + 1):
        try:
            response, gemini_seconds = run_agy(prompt, args.model, args.timeout, context_dir)
            error = None
            break
        except RuntimeError as e:
            error = str(e)
            if attempt < args.attempts:
                print(f"(試行 {attempt} 失敗 — {e} — 再試行する)", file=sys.stderr)

    after_hashes, _ = snapshot(context_dir)
    changed = [p for p, h in after_hashes.items() if before_hashes.get(p) != h]
    unexpected = sorted(p for p in changed if p not in files)

    report = {"model": args.model, "preserve": args.preserve, "context_dir": context_dir,
              "backup_dir": backup_dir, "backup_has_files": copied,
              "elapsed_seconds": round(time.time() - started, 1),
              "gemini_seconds": round(gemini_seconds, 1),
              "response": response, "error": error,
              "files": [], "unexpected_changes": unexpected}

    print(f"# 推敲 ({args.model}, preserve={args.preserve}, {report['elapsed_seconds']}s)")
    if error:
        print(f"\n!! 推敲できなかった: {error}")
    for path in files:
        after = open(path, encoding="utf-8").read()
        entry = {"path": path, "changed": after != before[path],
                 **measure(before[path], after, args.preserve)}
        report["files"].append(entry)
        print(f"\n## {path}" + ("" if entry["changed"] else "  [変更なし]"))
        print(f"   {entry['chars_before']} → {entry['chars_after']} 字 / "
              f"{entry['lines_before']} → {entry['lines_after']} 行")
        for v in entry["invariant_violations"]:
            print(f"!! 不変条件({args.preserve})に反した: {v}")
        if entry["added_tokens"]:
            print(f"!! 原文に無い記号・数値が増えた: {', '.join(entry['added_tokens'])}")
        if entry["removed_tokens"]:
            print(f"!! 原文にあった記号・数値が消えた: {', '.join(entry['removed_tokens'])}")

    if unexpected:
        print(f"\n!! 対象以外のファイルが変わった: {', '.join(unexpected)}")
        print("   控えから戻すこと。" + (f"控え: {backup_dir}" if copied
                                  else "控えはハッシュだけで、中身は残っていない"))
    if response:
        print(f"\n## Gemini の説明\n{response.strip()[:800]}")
    print(f"\n控え: {backup_dir}" + ("" if copied else " (大きすぎるのでハッシュだけ)"))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"結果 JSON: {args.output}")

    if error:
        sys.exit("推敲できなかった。")


if __name__ == "__main__":
    main()
