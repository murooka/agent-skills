#!/usr/bin/env python3
"""日本語テキストの添削を Gemini (agy CLI) に投げ、結果を id で対応づけて返す。

添削対象は agy のプロンプトに直接載せる。agy はヘッドレスではファイル読み取りが
自動拒否されるため、Gemini にパスを渡しても読めない。ファイルの読み書きは呼び出し側の
仕事で、このスクリプトは「テキストを渡して添削文を受け取る」だけを行う。

usage:
  python3 proofread.py --input items.json [--output result.json]
                       [--model gemini-3.8-flash-medium] [--timeout 180s]
                       [--batch-size 5] [--max-chars 12000] [--attempts 3] [--workers 3]

items.json は項目の配列:
  [{"id": "任意の識別子", "kind": "コードコメント(Go, 1 行)", "text": "原文",
    "hint": "任意。周辺の文脈や守ってほしい制約"}]
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

INSTRUCTION = """あなたは日本語の技術文書の校正者です。以下の各項目を、人間が読んで一度で理解できる自然な日本語へ添削してください。

原文はエンジニアが手早く書いた日本語で、情報を詰め込むあまり圧縮されている場合が多い。助詞が落ちて名詞が数珠つなぎになっている、一文に条件と動作と例外を詰め込んでいる、読点がなく係り受けが読み取れない、といった箇所を、意味を変えずにほどくのが仕事です。

守ること:
- 意味を変えない。原文に無い情報を足さず、原文にある情報を落とさない。
- 識別子・型名・関数名・カラム名・数値・単位・製品名・コード断片は原文の表記のまま残す。半角文字を全角に変えない。
- 各項目の「種別」が示す形式と長さに収める。コメント記号や Markdown 記法など、原文の書式は保つ。
- 文体（である調 / ですます調、体言止めの有無）は原文に合わせる。原文が混在していないかぎり、混ぜない。
- revised には、その項目の全文を返す。長いからといって一部だけを返したり、省略記号で省いたりしない。
- 直す必要が本当にない項目だけ、revised に原文をそのまま入れる。一方で、読みにくい箇所があるのに「変更なし」で済ませない。
- 原文の意味が読み取れず添削できない項目は、revised に原文をそのまま入れ、note の先頭に「判断できない:」と書いて理由を続ける。推測で補わない。

出力は項目ごとに id / revised / note。id は与えられたものを一字一句そのまま返す。note は「何をなぜ変えたか」の日本語一文（変えていないなら「変更なし」）。
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

BEGIN = "----- 原文ここから -----"
END = "----- 原文ここまで -----"


def build_prompt(items):
    blocks = [INSTRUCTION]
    for item in items:
        block = [f"\n### id: {item['id']}", f"種別: {item.get('kind', '指定なし')}"]
        if item.get("hint"):
            block.append(f"補足: {item['hint']}")
        block += [BEGIN, item["text"], END]
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def chunk(items, batch_size, max_chars):
    """項目を、件数と文字数の両方の上限に収まる塊へ分ける。

    1 回の呼び出しに全部を載せると、構造化出力が落ちたときに全件が巻き添えになる。
    塊を小さく保つと、失敗の影響範囲と再試行のコストがその塊だけに閉じる。
    """
    chunks, current = [], []
    for item in items:
        trial = current + [item]
        too_many = len(trial) > batch_size
        too_long = len(build_prompt(trial)) > max_chars
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


def call_once(prompt, model, timeout):
    cmd = [
        "agy", "--model", model, "--print-timeout", timeout,
        "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "-p", prompt,
    ]
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
        raise RuntimeError("添削結果の JSON が応答に含まれていない。応答冒頭: "
                           f"{str(payload.get('response'))[:200]!r}")
    return items, payload.get("duration_seconds", 0.0)


def call_agy(prompt, model, timeout, attempts, label):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return call_once(prompt, model, timeout)
        except RuntimeError as error:
            last_error = error
            if attempt < attempts:
                print(f"({label}: 試行 {attempt} 失敗 — {error} — 再試行する)", file=sys.stderr)
    raise last_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="項目の配列を収めた JSON ファイル")
    parser.add_argument("--output", help="結果 JSON の書き出し先")
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
    for item in items:
        item["id"] = str(item["id"])
        if not str(item.get("text", "")).strip():
            sys.exit(f"id={item['id']} の text が空")

    started = time.time()
    groups = chunk(items, args.batch_size, args.max_chars)

    def run(indexed):
        index, group = indexed
        label = f"塊 {index + 1}/{len(groups)}"
        try:
            returned, seconds = call_agy(build_prompt(group), args.model, args.timeout,
                                         args.attempts, label)
            return group, returned, seconds, None
        except RuntimeError as error:
            return group, [], 0.0, str(error)

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(groups)))) as pool:
        outcomes = list(pool.map(run, enumerate(groups)))

    revisions, failures, gemini_seconds = {}, [], 0.0
    for group, returned, seconds, error in outcomes:
        gemini_seconds += seconds
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
        results.append({
            "id": item["id"],
            "kind": item.get("kind", ""),
            "original": item["text"],
            "revised": revised,
            "note": note,
            "changed": revised != item["text"],
            "undecidable": note.startswith("判断できない"),
        })

    report = {
        "model": args.model,
        "elapsed_seconds": round(time.time() - started, 1),
        "gemini_seconds": round(gemini_seconds, 1),
        "batches": len(groups),
        "results": results,
        "missing_ids": missing,
        "unexpected_ids": sorted(revisions),
        "failures": failures,
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
        print(f"原文: {r['original']}")
        if r["changed"]:
            print(f"添削: {r['revised']}")
        print(f"理由: {r['note']}")
    for failure in failures:
        print(f"\n!! 添削できなかった id: {', '.join(failure['ids'])}\n   {failure['error']}")
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
