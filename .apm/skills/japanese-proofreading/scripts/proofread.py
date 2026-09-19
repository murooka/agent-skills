#!/usr/bin/env python3
"""日本語テキストの添削を Gemini (agy CLI) に投げ、結果を id で対応づけて返す。

添削する原文は、agy のプロンプトに直接載せる。原文の周りの文脈(コメントが指す関数、
カラムの定義など)は、--context-dir で指定したディレクトリの中に限って Gemini に読ませられる。
agy は -p ではカレントディレクトリをワークスペースとして扱わず、--add-dir で加えた
ディレクトリの中だけ読み取りを自動で許可する。書き込みとディレクトリの外の読み取りは拒否される。

usage:
  python3 proofread.py --input items.json [--output result.json]
                       [--context-dir DIR ...] [--origin ai|human|mixed]
                       [--preserve sentences|headings|conclusion|facts]
                       [--model gemini-3.8-flash-medium] [--timeout 180s]
                       [--batch-size 5] [--max-chars 12000] [--attempts 3] [--workers 3]

items.json は項目の配列:
  [{"id": "任意の識別子", "kind": "コードコメント(Go, 1 行)", "text": "原文",
    "preserve": "任意。この項目だけ別の不変条件にするとき",
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

単語の選び方と文章の構成におかしいところがあります。読みやすくなるように直してください。どこまで書き換えてよいかは、項目ごとに「不変条件」で指定します。

どの項目でも守ること:
- 事実と違うことを書かない。確かめていないことを断定しない。
- 数値・単位・日付・URL・識別子・型名・関数名・カラム名は、原文にあるものをそのまま使い、読んで確かめていないものを新しく持ち込まない。半角文字を全角に変えない。
- 文体（である調 / ですます調）は原文に合わせる。「種別」に指定があれば、そちらに従う。
- 「種別」が示す形式（行数、長さ、コメント記号、Markdown の書式）に収める。
- revised には、その項目の全文を返す。長いからといって一部だけを返したり、省略記号で省いたりしない。長い項目ほど、後半まで同じ密度で見る。
- 原文に無いことを足したときは、note の最後に「補った: <足したもの>（根拠: 読んだファイルの名前、または一般に知られている事実）」の行を入れる。
- 原文の意味が読み取れず推敲できない項目は、revised に原文をそのまま入れ、note の先頭に「判断できない:」と書いて理由を続ける。推測で補わない。

出力は項目ごとに id / revised / note。id は与えられたものを一字一句そのまま返す。note は「何をなぜ変えたか」の日本語一文（変えていないなら「変更なし」）。
"""

# 不変条件。どこまで書き換えてよいかの目盛りで、strict から loose へ並ぶ。
# 凍結だけを並べると、書かれていない範囲まで保守的に扱われるので、解放も必ず対で書く。
PRESERVE = {
    "sentences": """
不変条件（この実行の全項目に適用）:
- 保つもの: 文の並びと切り方、段落の構成、見出し、主張、事実。
- 変えてよいもの: 語の選び方、接続詞、助詞、読点の位置。
- 文を分けたり、つないだり、順序を入れ替えたりしない。語と接続の選び直しだけで読みやすくする。
""",
    "headings": """
不変条件（この実行の全項目に適用）:
- 保つもの: 見出しの文言とその並び、各節が扱っている主張、事実。見出しの無い項目では、段落ごとの主題とその並びを保つ。
- 変えてよいもの: 節の中の段落構成、文の切り方と順序、語の選び方、接続詞、言い足しと言い換え。
- 上に挙げた以外に制限はない。読みやすくなるなら、節の中は自由に組み替えてよい。
""",
    "conclusion": """
不変条件（この実行の全項目に適用）:
- 保つもの: 最終的な主張（この文章が読み手に納得させたい結論）と、事実。
- 変えてよいもの: 見出し、節の並び、論点の順序と取捨、段落構成、文の切り方、語の選び方。
- 結論に至る道筋は、読み手が納得しやすい順に組み替えてよい。上に挙げた以外に制限はない。
""",
    "facts": """
不変条件（この実行の全項目に適用）:
- 保つもの: 事実だけ。
- 変えてよいもの: 何を書くか、どこまで書くか、順序、構成、文、語。書く必要のない内容は削ってよく、足りない説明は足してよい。
- 上に挙げた以外に制限はない。
""",
}
PRESERVE_LEVELS = list(PRESERVE)

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


def headings_of(text):
    return [l.strip() for l in text.split("\n") if re.match(r"^#{1,6} ", l)]


def blocks_of(text):
    """空行で区切られた塊を数える。コードブロックは中身を畳んで 1 つと数える。"""
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


def check_invariant(original, revised, level):
    """不変条件のうち、機械で決まるものだけを照合する。

    緩い段階(conclusion, facts)は、保つものが主張と事実なので機械では決まらない。
    書き手が読んで確かめる(SKILL.md の手順 4)。
    """
    violations = []
    if level not in ("sentences", "headings"):
        return violations
    before, after = headings_of(original), headings_of(revised)
    if before != after:
        violations.append(f"見出しが変わった: {before} → {after}")
    if level == "sentences":
        if len(blocks_of(original)) != len(blocks_of(revised)):
            violations.append(f"段落の数が変わった: {len(blocks_of(original))} → {len(blocks_of(revised))}")
        if original.count("。") != revised.count("。"):
            violations.append(f"文の数が変わった: {original.count('。')} → {revised.count('。')}")
    return violations


BEGIN = "----- 原文ここから -----"
END = "----- 原文ここまで -----"


def build_prompt(items, origin="mixed", preserve="headings"):
    blocks = [INSTRUCTION + ORIGIN[origin] + PRESERVE[preserve]]
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


def chunk(items, batch_size, max_chars, origin="mixed", default_preserve="headings"):
    """項目を、件数と文字数の両方の上限に収まる塊へ分ける。

    1 回の呼び出しに全部を載せると、構造化出力が落ちたときに全件が巻き添えになる。
    塊を小さく保つと、失敗の影響範囲と再試行のコストがその塊だけに閉じる。
    """
    chunks, current, level = [], [], None
    for item in items:
        item_level = item.get("preserve") or default_preserve
        trial = current + [item]
        too_many = len(trial) > batch_size
        too_long = len(build_prompt(trial, origin, item_level)) > max_chars
        if current and (too_many or too_long or item_level != level):
            chunks.append(current)
            current = [item]
        else:
            current = trial
        level = item_level
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
    parser.add_argument("--preserve", choices=PRESERVE_LEVELS, default="headings",
                        help="どこまで書き換えてよいか。sentences=語と接続詞だけ / headings=節の中は自由(既定) / "
                             "conclusion=論の組み立ても自由 / facts=内容の取捨も自由。項目ごとの preserve が優先する")
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
        if item.get("preserve") and item["preserve"] not in PRESERVE:
            sys.exit(f"id={item['id']} の preserve は {'/'.join(PRESERVE_LEVELS)} のいずれかにする: {item['preserve']}")
        resolve_item_paths(item, context_dirs)

    started = time.time()
    groups = chunk(items, args.batch_size, args.max_chars, args.origin, args.preserve)

    def run(indexed):
        index, group = indexed
        label = f"塊 {index + 1}/{len(groups)}"
        try:
            level = group[0].get("preserve") or args.preserve
            returned, seconds, denied = call_agy(build_prompt(group, args.origin, level), args.model,
                                                 args.timeout, context_dirs, args.attempts, label)
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
        level = item.get("preserve") or args.preserve
        results.append({
            "id": item["id"],
            "preserve": level,
            "invariant_violations": check_invariant(item["text"], revised, level),
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
        "origin": args.origin,
        "preserve": args.preserve,
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
        print(f"\n## id: {r['id']}  [{r['preserve']}]" + ("" if r["changed"] else "  [変更なし]"))
        if r["undecidable"]:
            print("!! Gemini が意味を読み取れなかった項目")
        if r["mismatch"]:
            print("!! Gemini が、原文の内容と実装や定義の食い違いを指摘した項目")
        for v in r["invariant_violations"]:
            print(f"!! 不変条件({r['preserve']})に反した: {v}")
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
