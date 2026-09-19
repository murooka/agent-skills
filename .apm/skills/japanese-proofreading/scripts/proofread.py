#!/usr/bin/env python3
"""日本語テキストの添削を Gemini (agy CLI) に投げ、結果を id で対応づけて返す。

添削する原文は、agy のプロンプトに直接載せる。原文の周りの文脈(コメントが指す関数、
カラムの定義など)は、--context-dir で指定したディレクトリの中に限って Gemini に読ませられる。
agy は -p ではカレントディレクトリをワークスペースとして扱わず、--add-dir で加えた
ディレクトリの中だけ読み取りを自動で許可する。書き込みとディレクトリの外の読み取りは拒否される。

usage:
  python3 proofread.py --input items.json [--output result.json]
                       [--context-dir DIR ...] [--origin ai|human|mixed]
                       [--model gemini-3.8-flash-medium] [--timeout 180s]
                       [--batch-size 5] [--max-chars 12000] [--attempts 3] [--workers 3]

items.json は項目の配列:
  [{"id": "任意の識別子", "kind": "コードコメント(Go, 1 行)", "text": "原文",
    "hint": "任意。周辺の文脈や守ってほしい制約",
    "path": "任意。原文のあるファイルの絶対パス(--context-dir の中)",
    "line": "任意。原文のある行(例: 42、42-45)",
    "refs": ["任意。原文が指すコードやスキーマのファイルの絶対パス(--context-dir の中)"]}]
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

INSTRUCTION = """あなたは日本語の技術文書の校正者です。以下の各項目を、その分野の読み手が一度で理解できる自然な日本語へ推敲してください。

直すのは次の 3 つです。当てはまる箇所は、意味が変わらない範囲で書き換えてください。

1. 語の選び方。その分野で定着している言葉を避けて言い換えたもの（「バージョン」を「版」、「ツール」を「道具」、「レイヤー」を「層」と書くなど）、直訳調の言い回し、硬すぎる漢語、意味の広すぎる語。読み手がその分野で普通に使う語に直す。
2. 文章の構成。否定や前置きから入って主旨が後ろに回っている、話の順序が追いにくい、同じことを繰り返している。主旨が先に来る順に並べ替える。
3. 一文の圧縮。助詞が落ちて名詞が数珠つなぎになっている、一文に条件と動作と例外を詰め込んでいる、読点がなく係り受けが読み取れない。ほどいて分ける。

守ること:
- **事実と違うことを書かない。禁止はこれだけである。** 言い方を変える、順序を組み替える、言葉を足す、重複を削る、限定を言い直す — どれもしてよい。原文と違う内容になってもかまわない。問題になるのは、その文書が扱っている対象について、事実でないことを書いた場合だけである。
- 確かめられることは確かめる。「場所」や「参考」のファイルがあるなら読み、そこに書かれていることと食い違わないようにする。
- 確かめていないことを断定しない。数値・単位・日付・URL・識別子・型名・関数名・カラム名は、原文にあるものをそのまま使い、読んで確かめていないものを新しく持ち込まない。半角文字を全角に変えない。
- 原文が伝えている事実は落とさない。重複や冗長な言い回しは削ってよい。
- 原文に無いことを足したときは、note の最後に「補った: <足したもの>（根拠: 読んだファイルの名前、または一般に知られている事実）」の行を入れる。何も足していないなら書かない。
- 各項目の「種別」が示す形式と長さに収める。コメント記号や Markdown 記法など、原文の書式は保つ。
- 文体（である調 / ですます調、体言止めの有無）は原文に合わせる。原文が混在していないかぎり、混ぜない。
- revised には、その項目の全文を返す。長いからといって一部だけを返したり、省略記号で省いたりしない。長い項目ほど、後半まで同じ密度で見る。
- 直すところが無いかは、語・文・段落の 3 つの単位で確かめる。3 つとも無い項目だけ、revised に原文をそのまま入れる。読みにくい箇所や不自然な語があるのに「変更なし」で済ませない。
- 原文の意味が読み取れず添削できない項目は、revised に原文をそのまま入れ、note の先頭に「判断できない:」と書いて理由を続ける。推測で補わない。

出力は項目ごとに id / revised / note。id は与えられたものを一字一句そのまま返す。note は「何をなぜ変えたか」の日本語一文（変えていないなら「変更なし」）。
"""

ORIGIN = {
    "ai": """
原文について: これは AI が自動生成した文章です。意味は通っていることが多い一方で、その分野で使う言葉を避けた言い換え（「バージョン」を「版」と書くなど）、否定や前置きから入る構成、同じ言い回しの繰り返しが残りやすい。語の選び方と文章の構成を重点的に見てください。
""",
    "human": """
原文について: これはエンジニアが手早く書いた文章です。情報を詰め込むあまり圧縮されている場合が多く、助詞の落ちた名詞の数珠つなぎや、条件と動作と例外を詰め込んだ一文が残りやすい。一文の圧縮をほどくことを重点的に見てください。
""",
    "mixed": "",
}


CONTEXT_INSTRUCTION = """
周辺の文脈について:
- 「場所」や「参考」が書かれた項目は、添削の前に、そのファイルを読んで原文の周り（コメントが指す関数や型、カラムの定義、同じ文書の前後の節）を確かめる。ファイルを読むツールには、書かれている絶対パスをそのまま渡す。
- 周りを読んで確かめた事実は、読み手の理解を助けるなら revised に補ってよい（「利用停止中」に `status: suspended` を添えるなど）。補ったときは note の最後に「補った:」の行を入れる。読んでも確かめられないことは、断定して書かない。
- 書き直すのは原文だけ。周りの文章やコードは直さない。ファイルの変更やコマンドの実行はしない。
- 周りを読んで、原文の内容そのものが実装や定義と食い違っていると気づいたら、revised では意味を変えずに添削し、note の 1 行目に「食い違い:」と書いて何が食い違っているかを続け、改行してから、いつもどおり何をなぜ変えたかを 1 文で書く。1 行目に変更理由を混ぜない。
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "revised": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "revised", "note"],
            },
        }
    },
    "required": ["items"],
}

# インラインコードと数値。添削で増えた分は補足、減った分は事実の欠落の候補になる。
TOKEN = re.compile(r"`[^`\n]+`|(?<![\w`])\d+(?:[.,]\d+)*")


def token_diff(original, revised):
    """原文と添削文で、インラインコードと数値の集合を比べる。

    補足を許しているので、足されたものを書き手が 1 件ずつ確かめられるように拾う。
    Gemini の「補った:」の申告は漏れることがあるため、申告とは別に数える。
    """
    before = collections.Counter(TOKEN.findall(original))
    after = collections.Counter(TOKEN.findall(revised))
    return sorted((after - before).elements()), sorted((before - after).elements())


BEGIN = "----- 原文ここから -----"
END = "----- 原文ここまで -----"


def build_prompt(items, origin="mixed"):
    blocks = [INSTRUCTION + ORIGIN[origin]]
    if any(item.get("path") or item.get("refs") for item in items):
        blocks.append(CONTEXT_INSTRUCTION)
    for item in items:
        block = [f"\n### id: {item['id']}", f"種別: {item.get('kind', '指定なし')}"]
        if item.get("hint"):
            block.append(f"補足: {item['hint']}")
        if item.get("path"):
            line = f"（{item['line']} 行目）" if item.get("line") else ""
            block.append(f"場所: {item['path']}{line}")
        for ref in item.get("refs") or []:
            block.append(f"参考: {ref}")
        block += [BEGIN, item["text"], END]
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def chunk(items, batch_size, max_chars, origin="mixed"):
    """項目を、件数と文字数の両方の上限に収まる塊へ分ける。

    1 回の呼び出しに全部を載せると、構造化出力が落ちたときに全件が巻き添えになる。
    塊を小さく保つと、失敗の影響範囲と再試行のコストがその塊だけに閉じる。
    """
    chunks, current = [], []
    for item in items:
        trial = current + [item]
        too_many = len(trial) > batch_size
        too_long = len(build_prompt(trial, origin)) > max_chars
        if current and (too_many or too_long):
            chunks.append(current)
            current = [item]
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks


def extract_items(payload):
    """項目の配列を取り出す。structured_output が空で返ることが間欠的にあるため、
    応答本文に同じ JSON が載っていればそちらから拾う。"""
    structured = payload.get("structured_output")
    if isinstance(structured, dict) and isinstance(structured.get("items"), list):
        return structured["items"]
    text = payload.get("response", "")
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        for end in range(len(text), start, -1):
            if text[end - 1] != "}":
                continue
            try:
                candidate = json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("items"), list):
                return candidate["items"]
    return None


def diagnose(payload, stderr):
    """失敗の原因を切り分けるための材料を、エラー文の先頭に付ける。

    agy はツールの権限を拒否すると応答全体を空にして返すため、症状(JSON が無い)だけでは
    構造化出力の欠落と区別がつかない。拒否された操作と agy の案内を、失敗時にこそ出す。
    """
    notes = []
    denied = payload.get("denied_actions") or []
    if denied:
        names = ", ".join(sorted({d.get("display_name") or d.get("action", "?") for d in denied}))
        notes.append(f"拒否された操作: {names}(--context-dir の外を読もうとした可能性がある)")
    hint = next((line for line in (stderr or "").splitlines() if line.strip()), "")
    if hint:
        notes.append(f"agy の案内: {hint.strip()[:200]}")
    return "。".join(notes) + "。" if notes else ""


def call_once(prompt, model, timeout, context_dirs):
    cmd = [
        "agy", "--model", model, "--print-timeout", timeout,
        "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
    ]
    for directory in context_dirs:
        cmd += ["--add-dir", directory]
    cmd += ["-p", prompt]
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
    items = extract_items(payload)
    if items is None:
        raise RuntimeError(diagnose(payload, proc.stderr) + "添削結果の JSON が応答に含まれていない。応答冒頭: "
                           f"{str(payload.get('response'))[:200]!r}")
    return items, payload.get("duration_seconds", 0.0), payload.get("denied_actions") or []


def call_agy(prompt, model, timeout, context_dirs, attempts, label):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return call_once(prompt, model, timeout, context_dirs)
        except RuntimeError as error:
            last_error = error
            if attempt < attempts:
                print(f"({label}: 試行 {attempt} 失敗 — {error} — 再試行する)", file=sys.stderr)
    raise last_error


def resolve_context_dirs(raw_dirs):
    """--context-dir を実在する絶対パスに揃える。

    ディレクトリの中は .gitignore の対象も含めて Gemini に読まれうるので、
    ホームディレクトリやルートのような広すぎる指定は受け付けない。
    """
    home = os.path.realpath(os.path.expanduser("~"))
    dirs = []
    for raw in raw_dirs:
        directory = os.path.realpath(raw)
        if not os.path.isdir(directory):
            sys.exit(f"--context-dir が見つからない: {raw}")
        if directory in (home, os.path.sep):
            sys.exit(f"--context-dir が広すぎる: {raw}。原文と参考のファイルを含む、なるべく狭いディレクトリを指定する。")
        dirs.append(directory)
    return dirs


def resolve_item_paths(item, context_dirs):
    """path と refs を実在する絶対パスに揃え、--context-dir の中にあることを確かめる。"""
    for key in ("path", "refs"):
        if key not in item:
            continue
        values = item[key] if key == "refs" else [item[key]]
        if key == "refs" and not isinstance(values, list):
            sys.exit(f"id={item['id']} の refs は絶対パスの配列にする")
        resolved = []
        for value in values:
            if not isinstance(value, str) or not os.path.isabs(value):
                sys.exit(f"id={item['id']} の {key} は絶対パスにする: {value!r}")
            path = os.path.realpath(value)
            if not os.path.isfile(path):
                sys.exit(f"id={item['id']} の {key} が見つからない: {value}")
            if not context_dirs:
                sys.exit(f"id={item['id']} に {key} があるのに --context-dir がない。読ませるディレクトリを指定する。")
            if not any(os.path.commonpath([path, d]) == d for d in context_dirs):
                sys.exit(f"id={item['id']} の {key} が --context-dir の外にある: {value}")
            resolved.append(path)
        item[key] = resolved if key == "refs" else resolved[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="項目の配列を収めた JSON ファイル")
    parser.add_argument("--output", help="結果 JSON の書き出し先")
    parser.add_argument("--context-dir", action="append", default=[],
                        help="Gemini に読ませてよいディレクトリ(繰り返し指定可)。項目の path と refs はこの中に置く")
    parser.add_argument("--origin", choices=["ai", "human", "mixed"], default="mixed",
                        help="原文の出自。ai=エージェントや LLM が生成した文章、human=人が手早く書いた文章")
    parser.add_argument("--model", default="gemini-3.8-flash-medium")
    parser.add_argument("--timeout", default="180s", help="agy 1 回あたりの応答待ち上限")
    parser.add_argument("--batch-size", type=int, default=5, help="1 回の呼び出しに載せる項目数の上限")
    parser.add_argument("--max-chars", type=int, default=12000, help="1 回の呼び出しのプロンプト上限文字数")
    parser.add_argument("--attempts", type=int, default=3, help="塊ごとの試行回数")
    parser.add_argument("--workers", type=int, default=3, help="並行して投げる塊の数")
    args = parser.parse_args()

    if shutil.which("agy") is None:
        sys.exit("agy が見つからない。Antigravity CLI がインストールされているか確認する。")

    with open(args.input, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list) or not items:
        sys.exit("--input は項目の配列でなければならない（空でないこと）")
    ids = [str(item["id"]) for item in items]
    if len(set(ids)) != len(ids):
        sys.exit("id が重複している。対応づけができないため中断する。")
    context_dirs = resolve_context_dirs(args.context_dir)
    for item in items:
        item["id"] = str(item["id"])
        if not str(item.get("text", "")).strip():
            sys.exit(f"id={item['id']} の text が空")
        resolve_item_paths(item, context_dirs)

    started = time.time()
    groups = chunk(items, args.batch_size, args.max_chars, args.origin)

    def run(indexed):
        index, group = indexed
        label = f"塊 {index + 1}/{len(groups)}"
        try:
            returned, seconds, denied = call_agy(build_prompt(group, args.origin), args.model, args.timeout,
                                                 context_dirs, args.attempts, label)
            return group, returned, seconds, denied, None
        except RuntimeError as error:
            return group, [], 0.0, [], str(error)

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(groups)))) as pool:
        outcomes = list(pool.map(run, enumerate(groups)))

    revisions, failures, denied_actions, gemini_seconds = {}, [], [], 0.0
    for group, returned, seconds, denied, error in outcomes:
        gemini_seconds += seconds
        if denied:
            denied_actions.append({"ids": [i["id"] for i in group], "denied": denied})
        if error:
            failures.append({"ids": [i["id"] for i in group], "error": error})
        for entry in returned:
            revisions[str(entry.get("id"))] = entry

    results, missing = [], []
    for item in items:
        entry = revisions.pop(item["id"], None)
        if entry is None:
            missing.append(item["id"])
            continue
        revised = entry.get("revised", "")
        note = entry.get("note", "")
        added_tokens, removed_tokens = token_diff(item["text"], revised)
        results.append({
            "id": item["id"],
            "kind": item.get("kind", ""),
            "original": item["text"],
            "revised": revised,
            "note": note,
            "changed": revised != item["text"],
            "undecidable": note.startswith("判断できない"),
            "mismatch": note.startswith("食い違い"),
            "supplemented": "補った:" in note,
            "added_tokens": added_tokens,
            "removed_tokens": removed_tokens,
        })

    report = {
        "model": args.model,
        "elapsed_seconds": round(time.time() - started, 1),
        "gemini_seconds": round(gemini_seconds, 1),
        "batches": len(groups),
        "context_dirs": context_dirs,
        "results": results,
        "missing_ids": missing,
        "unexpected_ids": sorted(revisions),
        "failures": failures,
        "denied_actions": denied_actions,
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    changed = sum(1 for r in results if r["changed"])
    print(f"# 添削結果 ({args.model}, {len(groups)} 回の呼び出し, {report['elapsed_seconds']}s) — "
          f"{len(items)} 件中 {len(results)} 件が返り、うち {changed} 件に変更あり")
    for r in results:
        print(f"\n## id: {r['id']}" + ("" if r["changed"] else "  [変更なし]"))
        if r["undecidable"]:
            print("!! Gemini が意味を読み取れなかった項目")
        if r["mismatch"]:
            print("!! Gemini が、原文の内容と実装や定義の食い違いを指摘した項目")
        if r["supplemented"]:
            print("!! 原文に無い補足が足された項目（note の「補った:」の根拠を確かめる）")
        if r["added_tokens"]:
            print(f"!! 原文に無い記号・数値が増えた: {', '.join(r['added_tokens'])}")
        if r["removed_tokens"]:
            print(f"!! 原文にあった記号・数値が消えた: {', '.join(r['removed_tokens'])}")
        print(f"原文: {r['original']}")
        if r["changed"]:
            print(f"添削: {r['revised']}")
        print(f"理由: {r['note']}")
    for failure in failures:
        print(f"\n!! 添削できなかった id: {', '.join(failure['ids'])}\n   {failure['error']}")
    for entry in denied_actions:
        names = ", ".join(sorted({d.get("display_name") or d.get("action", "?") for d in entry["denied"]}))
        print(f"\n!! agy が拒否した操作があった id: {', '.join(entry['ids'])} ({names})")
    if missing:
        print(f"\n!! 返ってこなかった id: {', '.join(missing)}")
    if report["unexpected_ids"]:
        print(f"!! 依頼していない id が返った: {', '.join(report['unexpected_ids'])}")
    if args.output:
        print(f"\n結果 JSON: {args.output}")
    if not results:
        sys.exit("1 件も添削できなかった。")


if __name__ == "__main__":
    main()
