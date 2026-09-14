#!/usr/bin/env python3
"""設問をブラウザのフォームで人に聞き、回答を Markdown で受け取る。

ローカル(127.0.0.1)にフォームの画面を出すサーバを立て、AI はフォーム定義(JSON)を post し、
wait で人の送信を受け取る。セッションの状態は <ホーム>/<セッション id>/state.json に置き、
サーバと各コマンドはこのファイルをロックして読み書きする。サーバが落ちても状態は残り、
次の post / wait がサーバを起動し直す。

usage:
  python3 ask.py post FILE [--session ID] [--no-open]   # FILE に - を渡すと標準入力から読む
  python3 ask.py wait --session ID [--timeout SEC]
  python3 ask.py stop --session ID [--force]
  python3 ask.py sessions
  python3 ask.py history --session ID

wait の終了コード: 0 = 送信を受け取った / 3 = タイムアウト(同じ wait をもう一度) / 1 = エラー
ホーム: $ASK_VIA_FORM_HOME、なければ ${XDG_CACHE_HOME:-~/.cache}/ask-via-form
"""
import argparse
import contextlib
import fcntl
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

EXIT_ERROR = 1
EXIT_TIMEOUT = 3
IDLE_LIMIT_SEC = 24 * 3600
RETENTION_SEC = 7 * 24 * 3600
STOP_GRACE_SEC = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
SESSION_RE = re.compile(r"[a-z0-9]{6}")
SESSION_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
FORM_HTML = Path(__file__).resolve().parent.parent / "assets" / "form.html"

FORM_KEYS = {"id", "title", "intro", "questions"}
QUESTION_KEYS = {"id", "title", "body", "options", "multiple", "text", "recommended", "reason"}
OPTION_KEYS = {"label", "description"}
TEXT_MODES = ("none", "optional", "required")
ANSWER_MODES = ("answer", "hold", "return")
STATE_LABELS = {"answered": "回答", "hold": "保留", "return": "差し戻し", "unanswered": "未回答"}
STOP_REASONS = {"stop": "AI が終了しました", "idle": "24 時間操作がなかったため自動で終了しました"}


class UserError(Exception):
    """AI に読ませて直してもらうエラー。トレースバックは出さない。"""


# ---------------------------------------------------------------- 状態ファイル


def home():
    env = os.environ.get("ASK_VIA_FORM_HOME")
    if env:
        return Path(env).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "ask-via-form"


def session_dir(sid):
    if not SESSION_RE.fullmatch(sid or ""):
        raise UserError(f"セッション id の形式が違います: {sid}")
    d = home() / sid
    if not (d / "state.json").is_file():
        raise UserError(f"セッション {sid} が見つかりません。sessions で一覧を確認してください")
    return d


def read_state(d):
    with open(d / "state.json", encoding="utf-8") as f:
        return json.load(f)


def write_state(d, state):
    tmp = d / "state.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, d / "state.json")


@contextlib.contextmanager
def transaction(d):
    """state.json を排他ロックして読み、ブロックを抜けたとき変わっていれば書き戻す。"""
    with open(d / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(d)
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            write_state(d, state)


def create_session():
    root = home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cleanup(root)
    for _ in range(50):
        sid = "".join(secrets.choice(SESSION_ALPHABET) for _ in range(6))
        d = root / sid
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        now = time.time()
        write_state(d, {
            "version": 1,
            "id": sid,
            "token": secrets.token_urlsafe(18),
            "created_at": now,
            "last_activity": now,
            "status": "active",
            "stop_reason": None,
            "stopped_at": None,
            "rev": 0,
            "port": None,
            "forms": [],
            "submissions": [],
        })
        return d
    raise UserError("セッション id を割り当てられませんでした")


def mark_stopped(state, reason):
    state["status"] = "stopped"
    state["stop_reason"] = reason
    state["stopped_at"] = time.time()
    state["rev"] += 1


def cleanup(root):
    """終わって 7 日たったセッションを消す。サーバごと放置されたセッションは終了扱いにする。"""
    now = time.time()
    for d in root.iterdir():
        if not (d / "state.json").is_file():
            continue
        try:
            st = read_state(d)
        except (OSError, ValueError):
            continue
        if st.get("status") == "stopped":
            if now - (st.get("stopped_at") or st.get("last_activity") or 0) > RETENTION_SEC:
                shutil.rmtree(d, ignore_errors=True)
        elif now - st.get("last_activity", 0) > IDLE_LIMIT_SEC and not server_alive(d, st):
            with transaction(d) as s:
                if s["status"] == "active":
                    mark_stopped(s, "idle")


def open_form(state):
    return next((f for f in state["forms"] if f["submitted"] is None), None)


def find_form(state, form_id):
    return next((f for f in state["forms"] if f["definition"]["id"] == form_id), None)


def undelivered(state):
    return [s for s in state["submissions"] if s["delivered_at"] is None]


def ago(ts):
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分前"
    if sec < 86400:
        return f"{sec // 3600} 時間前"
    return f"{sec // 86400} 日前"


def delivered_hint(state):
    done = [s for s in state["submissions"] if s["delivered_at"] is not None]
    if not done:
        return ""
    last = done[-1]
    return (f"送信 #{last['seq']} は {ago(last['delivered_at'])}に受け取り済みです。"
            "出力を見失った場合は history で確認してください。")


# ---------------------------------------------------------------- フォーム定義


def parse_form(text):
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise UserError(f"フォーム定義を JSON として読めません({e.lineno} 行 {e.colno} 列): {e.msg}")
    errors = []
    form = normalize_form(raw, errors)
    if errors:
        raise UserError("フォーム定義に誤りがあります:\n" + "\n".join("- " + e for e in errors))
    return form


def check_keys(obj, allowed, path, errors):
    for key in obj:
        if key not in allowed:
            errors.append(f"{path or '最上位'}: 知らない項目 {key} があります(使えるのは {', '.join(sorted(allowed))})")


def optional_str(obj, key, path, errors):
    value = obj.get(key, "")
    if not isinstance(value, str):
        errors.append(f"{path}{key}: 文字列にしてください")
        return ""
    return value


def normalize_form(raw, errors):
    if not isinstance(raw, dict):
        errors.append("最上位はオブジェクトにしてください")
        return None
    check_keys(raw, FORM_KEYS, "", errors)
    form_id = raw.get("id")
    if not isinstance(form_id, str) or not ID_RE.fullmatch(form_id):
        errors.append("id: 必須です。英数字・_・- の 1〜40 文字で書いてください")
    questions = []
    raw_questions = raw.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        errors.append("questions: 設問を 1 つ以上、配列で書いてください")
    else:
        seen = set()
        for i, rq in enumerate(raw_questions):
            q = normalize_question(rq, i, errors)
            if q is None:
                continue
            if q["id"] in seen:
                errors.append(f"questions[{i}].id: {q['id']} が重複しています")
            seen.add(q["id"])
            questions.append(q)
    return {
        "id": form_id,
        "title": optional_str(raw, "title", "", errors),
        "intro": optional_str(raw, "intro", "", errors),
        "questions": questions,
    }


def normalize_question(rq, i, errors):
    path = f"questions[{i}]"
    if not isinstance(rq, dict):
        errors.append(f"{path}: オブジェクトにしてください")
        return None
    check_keys(rq, QUESTION_KEYS, path, errors)
    qid = rq.get("id", f"q{i + 1}")
    if not isinstance(qid, str) or not ID_RE.fullmatch(qid):
        errors.append(f"{path}.id: 英数字・_・- の 1〜40 文字で書いてください")
    title = rq.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{path}.title: 必須です")

    options = []
    raw_options = rq.get("options", [])
    if not isinstance(raw_options, list):
        errors.append(f"{path}.options: 配列にしてください")
        raw_options = []
    for j, ro in enumerate(raw_options):
        opath = f"{path}.options[{j}]"
        if isinstance(ro, str):
            ro = {"label": ro}
        if not isinstance(ro, dict):
            errors.append(f"{opath}: 文字列か {{\"label\", \"description\"}} にしてください")
            continue
        check_keys(ro, OPTION_KEYS, opath, errors)
        label = ro.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{opath}.label: 必須です")
            continue
        if any(o["label"] == label for o in options):
            errors.append(f"{opath}.label: {label} が重複しています")
            continue
        options.append({"label": label, "description": optional_str(ro, "description", opath + ".", errors)})

    multiple = rq.get("multiple", False)
    if not isinstance(multiple, bool):
        errors.append(f"{path}.multiple: true か false にしてください")
        multiple = False
    if multiple and not options:
        errors.append(f"{path}.multiple: options がない設問には指定できません")
    text = rq.get("text", "optional")
    if text not in TEXT_MODES:
        errors.append(f"{path}.text: none / optional / required のいずれかにしてください")
        text = "optional"
    if not options and text == "none":
        errors.append(f"{path}: options がなく text も none なので、答えようがありません")

    recommended = []
    raw_rec = rq.get("recommended")
    if raw_rec is not None:
        recs = raw_rec if isinstance(raw_rec, list) else [raw_rec]
        if not recs or not all(isinstance(r, str) and r.strip() for r in recs):
            errors.append(f"{path}.recommended: 文字列(複数選択なら文字列の配列)で書いてください")
        elif options:
            labels = [o["label"] for o in options]
            for r in recs:
                if r not in labels:
                    errors.append(f"{path}.recommended: 「{r}」は options の label にありません")
            if not multiple and len(recs) > 1:
                errors.append(f"{path}.recommended: 単一選択の設問には 1 つだけ指定してください")
            recommended = [r for r in recs if r in labels]
        else:
            if len(recs) > 1:
                errors.append(f"{path}.recommended: options がない設問では、推す文章を 1 つだけ書いてください")
            recommended = recs[:1]
    reason = optional_str(rq, "reason", path + ".", errors)
    if reason and raw_rec is None:
        errors.append(f"{path}.reason: recommended と一緒に書いてください")

    return {
        "id": qid,
        "title": title if isinstance(title, str) else "",
        "body": optional_str(rq, "body", path + ".", errors),
        "options": options,
        "multiple": multiple,
        "text": text,
        "recommended": recommended,
        "reason": reason,
    }


# ---------------------------------------------------------------- 回答


def clean_answer(q, raw):
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in ANSWER_MODES else "answer"
    chosen = raw.get("selected") if isinstance(raw.get("selected"), list) else []
    selected = [o["label"] for o in q["options"] if o["label"] in chosen]
    if not q["multiple"]:
        selected = selected[:1]
    text = raw.get("text") if isinstance(raw.get("text"), str) and q["text"] != "none" else ""
    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    return {"mode": mode, "selected": selected, "text": text, "note": note}


def clean_answers(form, raw):
    raw = raw if isinstance(raw, dict) else {}
    return {q["id"]: clean_answer(q, raw.get(q["id"])) for q in form["questions"]}


def answer_state(q, a):
    if a["mode"] in ("hold", "return"):
        return a["mode"]
    text = a["text"].strip()
    if not a["selected"] and not text:
        return "unanswered"
    if q["text"] == "required" and not text:
        return "unanswered"
    return "answered"


def effective(q, a):
    """回答状態に効く中身だけを取り出す。変更の有無はこれで比べる。"""
    st = answer_state(q, a)
    if st == "answered":
        return [st, a["selected"], a["text"].strip()]
    if st in ("hold", "return"):
        return [st, a["note"].strip()]
    return [st]


def follows_recommendation(q, a):
    if answer_state(q, a) != "answered" or not q["recommended"]:
        return False
    if q["options"]:
        return set(a["selected"]) == set(q["recommended"]) and not a["text"].strip()
    return a["text"].strip() == q["recommended"][0].strip()


def state_label(q, a):
    if follows_recommendation(q, a):
        return "回答(推奨どおり)"
    return STATE_LABELS[answer_state(q, a)]


def quote(text):
    return ["> " + line if line else ">" for line in text.strip().split("\n")]


def shorten(text, limit=40):
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def answer_lines(q, a):
    st = answer_state(q, a)
    lines = []
    if st == "answered":
        if a["selected"]:
            if q["multiple"]:
                lines.append("選択:")
                lines += ["- " + s for s in a["selected"]]
            else:
                lines.append("選択: " + a["selected"][0])
        if a["text"].strip():
            lines.append("自由記述:")
            lines += quote(a["text"])
    elif st in ("hold", "return") and a["note"].strip():
        lines += quote(a["note"])
    return lines


def one_line(q, a):
    st = answer_state(q, a)
    parts = [state_label(q, a)]
    if st == "answered":
        if a["selected"]:
            parts.append("選択: " + "、".join(a["selected"]))
        if a["text"].strip():
            parts.append("自由記述: " + shorten(a["text"]))
    elif st in ("hold", "return") and a["note"].strip():
        parts.append(shorten(a["note"]))
    return " / ".join(parts)


def missing_return_notes(form, answers, only=None):
    return [f"{form['id']}/{q['id']}" for q in form["questions"]
            if (only is None or q["id"] in only)
            and answers[q["id"]]["mode"] == "return" and not answers[q["id"]]["note"].strip()]


def render_submission(seq, form, answers, memo, changes):
    out = []
    if form is not None:
        head = f"# 送信 #{seq}: フォーム {form['id']}"
        if form["title"]:
            head += f"「{form['title']}」"
        out += [head, ""]
        for n, q in enumerate(form["questions"], 1):
            a = answers[q["id"]]
            out.append(f"## Q{n} {q['title']} — {state_label(q, a)}")
            out += answer_lines(q, a)
            out.append("")
        if memo.strip():
            out += ["## 全体メモ"] + quote(memo) + [""]
    else:
        out += [f"# 送信 #{seq}: 前のフォームでの変更のみ", ""]
    if changes:
        out += ["## 前のフォームでの変更", ""]
        for lines in changes:
            out += lines + [""]
    return "\n".join(out).rstrip() + "\n"


def collect_changes(form_entry, new_answers, new_memo):
    """送信済みフォームの編集内容を、返す回答の行と、差し戻しの記入漏れに分けて返す。"""
    form = form_entry["definition"]
    old_answers = form_entry["answers"]
    changes, changed_ids = [], set()
    for n, q in enumerate(form["questions"], 1):
        old, new = old_answers[q["id"]], new_answers[q["id"]]
        if effective(q, old) == effective(q, new):
            continue
        changed_ids.add(q["id"])
        changes.append([f"### フォーム {form['id']} Q{n} {q['title']} — {state_label(q, new)}"]
                       + answer_lines(q, new) + ["変更前: " + one_line(q, old)])
    old_memo = form_entry.get("memo", "")
    if new_memo.strip() != old_memo.strip():
        changes.append([f"### フォーム {form['id']} 全体メモ"]
                       + (quote(new_memo) if new_memo.strip() else ["(削除)"])
                       + ["変更前: " + (shorten(old_memo) if old_memo.strip() else "(なし)")])
    return changes, missing_return_notes(form, new_answers, only=changed_ids)


# ---------------------------------------------------------------- サーバ


def server_url(state):
    return f"http://127.0.0.1:{state['port']}/{state['token']}/"


def read_server_info(d):
    try:
        with open(d / "server.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def server_alive(d, state):
    info = read_server_info(d)
    if not info:
        return False
    url = f"http://127.0.0.1:{info['port']}/{state['token']}/api/ping"
    # HTTP_PROXY などの環境変数があっても 127.0.0.1 へ直接つなぐ
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1) as res:
            return res.status == 200
    except OSError:
        return False


def ensure_server(d):
    """サーバが動いていなければ起動する。(URL, 起動したか) を返す。"""
    with open(d / "spawn.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(d)
        if server_alive(d, state):
            return server_url(state), False
        with open(d / "server.log", "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_serve", state["id"]],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                raise UserError(f"フォームのサーバを起動できませんでした。{d / 'server.log'} を確認してください")
            state = read_state(d)
            if server_alive(d, state):
                return server_url(state), True
            time.sleep(0.1)
        raise UserError(f"フォームのサーバが 10 秒以内に起動しませんでした。{d / 'server.log'} を確認してください")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ask-via-form"

    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def resolve(self):
        """トークンと Host を確かめ、トークンより後ろのパスを返す。応答済みなら None。"""
        ctx = self.server.ctx
        if self.headers.get("Host", "") not in (f"127.0.0.1:{ctx['port']}", f"localhost:{ctx['port']}"):
            self.send_json(403, {"error": "forbidden"})
            return None
        path = self.path.split("?", 1)[0]
        prefix = "/" + ctx["token"]
        if path == prefix:
            self.send_response(302)
            self.send_header("Location", prefix + "/")
            self.end_headers()
            return None
        if not path.startswith(prefix + "/"):
            self.send_json(404, {"error": "not found"})
            return None
        return path[len(prefix):]

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        route = self.resolve()
        if route is None:
            return
        d = self.server.ctx["dir"]
        if route == "/":
            body = FORM_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif route == "/api/ping":
            self.send_json(200, {"ok": True})
        elif route == "/api/state":
            self.send_json(200, client_view(read_state(d)))
        else:
            self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        route = self.resolve()
        if route is None:
            return
        if route != "/api/draft":
            return self.send_json(404, {"error": "not found"})
        try:
            body = self.read_body()
        except ValueError:
            return self.send_json(400, {"error": "リクエストを読めません"})
        with transaction(self.server.ctx["dir"]) as st:
            if st["status"] != "active":
                return self.send_json(409, {"error": "セッションは終了しています"})
            entry = find_form(st, body.get("form_id"))
            if entry is None:
                return self.send_json(404, {"error": "フォームが見つかりません"})
            memo = body.get("memo") if isinstance(body.get("memo"), str) else ""
            entry["draft"] = {"answers": clean_answers(entry["definition"], body.get("answers")), "memo": memo}
            st["last_activity"] = time.time()
        self.send_json(200, {"ok": True})

    def do_POST(self):
        route = self.resolve()
        if route is None:
            return
        if route != "/api/submit":
            return self.send_json(404, {"error": "not found"})
        try:
            body = self.read_body()
        except ValueError:
            return self.send_json(400, {"error": "リクエストを読めません"})
        with transaction(self.server.ctx["dir"]) as st:
            status, payload = submit(st, body)
        self.send_json(status, payload)


def submit(st, body):
    if st["status"] != "active":
        return 409, {"error": "セッションは終了しています"}
    current = open_form(st)
    form_id = body.get("form_id")
    if form_id is not None and (current is None or current["definition"]["id"] != form_id):
        return 409, {"error": "フォームの状態が変わりました。画面を読み込み直してください"}

    missing = []
    answers, memo = None, ""
    if form_id is not None:
        answers = clean_answers(current["definition"], body.get("answers"))
        memo = body.get("memo") if isinstance(body.get("memo"), str) else ""
        missing += missing_return_notes(current["definition"], answers)

    edits = body.get("edits") if isinstance(body.get("edits"), dict) else {}
    changes, applied = [], []
    for entry in st["forms"]:
        edit = edits.get(entry["definition"]["id"])
        if entry["submitted"] is None or not isinstance(edit, dict):
            continue
        new_answers = clean_answers(entry["definition"], edit.get("answers"))
        new_memo = edit.get("memo") if isinstance(edit.get("memo"), str) else ""
        lines, missing_notes = collect_changes(entry, new_answers, new_memo)
        missing += missing_notes
        changes += lines
        applied.append((entry, new_answers, new_memo))

    if missing:
        return 400, {"error": "差し戻しの内容が書かれていない設問があります", "questions": missing}
    if form_id is None and not changes:
        return 400, {"error": "送る変更がありません"}

    now = time.time()
    seq = len(st["submissions"]) + 1
    text = render_submission(seq, current["definition"] if form_id is not None else None, answers, memo, changes)
    if form_id is not None:
        current.update({"submitted": {"seq": seq, "at": now}, "answers": answers, "memo": memo, "draft": None, "notice": ""})
    for entry, new_answers, new_memo in applied:
        entry.update({"answers": new_answers, "memo": new_memo, "draft": None})
    st["submissions"].append({"seq": seq, "at": now, "form_id": form_id, "text": text, "delivered_at": None})
    st["last_activity"] = now
    st["rev"] += 1
    return 200, {"seq": seq}


def client_view(st):
    last = st["submissions"][-1] if st["submissions"] else None
    forms = []
    for entry in st["forms"]:
        view = dict(entry["definition"])
        view.update({
            "submitted": entry["submitted"] is not None,
            "seq": entry["submitted"]["seq"] if entry["submitted"] else None,
            "answers": entry.get("answers"),
            "memo": entry.get("memo", ""),
            "draft": entry.get("draft"),
            "notice": entry.get("notice", ""),
        })
        forms.append(view)
    return {
        "session": st["id"],
        "status": st["status"],
        "stop_reason": STOP_REASONS.get(st.get("stop_reason"), ""),
        "rev": st["rev"],
        "forms": forms,
        "last_submission": {"seq": last["seq"], "delivered": last["delivered_at"] is not None} if last else None,
    }


def serve(sid):
    d = home() / sid
    st = read_state(d)
    httpd = None
    for port in ([st["port"], 0] if st.get("port") else [0]):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("127.0.0.1 で待ち受けられませんでした", file=sys.stderr)
        return EXIT_ERROR
    port = httpd.server_address[1]
    httpd.ctx = {"dir": d, "token": st["token"], "port": port}
    with transaction(d) as s:
        s["port"] = port
    with open(d / "server.json", "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port}, f)

    terminated = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: terminated.set())
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    stopped_seen = None
    try:
        while not terminated.wait(1):
            try:
                s = read_state(d)
            except (OSError, ValueError):
                continue
            if s["status"] == "active":
                if time.time() - s["last_activity"] > IDLE_LIMIT_SEC:
                    with transaction(d) as s2:
                        if s2["status"] == "active" and time.time() - s2["last_activity"] > IDLE_LIMIT_SEC:
                            mark_stopped(s2, "idle")
                continue
            # 開いている画面が「セッション終了」を読み取れるよう、少し待ってから止まる
            stopped_seen = stopped_seen or time.time()
            if time.time() - stopped_seen >= STOP_GRACE_SEC:
                break
    finally:
        httpd.shutdown()
        httpd.server_close()
        info = read_server_info(d)
        if info and info.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                (d / "server.json").unlink()
    return 0


# ---------------------------------------------------------------- コマンド


def apply_post(st, form):
    count = len(form["questions"])
    entry = find_form(st, form["id"])
    if entry is not None:
        old = entry["definition"]
        if entry["submitted"] is not None:
            if old == form:
                return f"フォーム {form['id']} は送信済みで、定義も同じなので何もしませんでした"
            raise UserError(f"フォーム {form['id']} は送信済みです。新しい設問は別の id で post してください")
        if old == form:
            return f"フォーム {form['id']} は同じ定義ですでに出ています({count} 問)"
        new_ids = {q["id"] for q in form["questions"]}
        removed = [q["id"] for q in old["questions"] if q["id"] not in new_ids]
        if removed:
            raise UserError(
                f"差し替えで設問を消すことはできません(消えた設問 id: {', '.join(removed)})。"
                "取り下げたい設問は、intro に「答えなくてよい」と書いてください")
        entry["definition"] = form
        if entry.get("draft"):
            entry["draft"]["answers"] = clean_answers(form, entry["draft"]["answers"])
        added = count - len(old["questions"])
        entry["notice"] = f"設問が {added} 問追加されました" if added > 0 else "設問の内容が更新されました"
        st["rev"] += 1
        return f"フォーム {form['id']} を差し替えました({len(old['questions'])} 問 → {count} 問)"
    current = open_form(st)
    if current is not None:
        cid = current["definition"]["id"]
        raise UserError(f"送信前のフォーム {cid} があります。設問を足すなら、同じ id({cid})で post し直してください")
    st["forms"].append({
        "definition": form,
        "posted_at": time.time(),
        "submitted": None,
        "answers": None,
        "memo": "",
        "draft": None,
        "notice": "",
    })
    st["rev"] += 1
    return f"フォーム {form['id']} を出しました({count} 問)"


def cmd_post(args):
    if args.file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            raise UserError(f"フォーム定義を読めません: {e}")
    form = parse_form(text)
    is_new = args.session is None
    d = create_session() if is_new else session_dir(args.session)
    with transaction(d) as st:
        if st["status"] != "active":
            raise UserError(f"セッション {st['id']} は終了しています。--session を付けずに post すると新しいセッションになります")
        message = apply_post(st, form)
        st["last_activity"] = time.time()
        pending = [s["seq"] for s in undelivered(st)]
        sid = st["id"]
    url, started = ensure_server(d)
    if is_new and not args.no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    print(message)
    print(f"session: {sid}")
    print(f"url: {url}")
    if started and not is_new:
        print("(止まっていたサーバを起動し直しました)")
    if pending:
        print(f"注意: 受け取っていない送信があります(#{', #'.join(map(str, pending))})。wait で受け取ってください")
    return 0


def cmd_wait(args):
    d = session_dir(args.session)
    deadline = time.time() + args.timeout if args.timeout else None
    last_check = 0.0
    while True:
        with transaction(d) as st:
            pending = undelivered(st)
            if pending:
                pending[0]["delivered_at"] = time.time()
                text = pending[0]["text"]
                break
            if st["status"] != "active":
                raise UserError(f"セッション {st['id']} は終了しています({STOP_REASONS.get(st['stop_reason'], '')})。" + delivered_hint(st))
            current = open_form(st)
            if current is None:
                raise UserError("開いているフォームも、受け取っていない送信もありません。" + delivered_hint(st))
            form_id = current["definition"]["id"]
            hint = delivered_hint(st)
        if time.time() - last_check > 5:
            ensure_server(d)
            last_check = time.time()
        if deadline is not None and time.time() >= deadline:
            print(f"まだ送信されていません(フォーム {form_id} の回答待ち)。同じ wait をもう一度実行してください。" + hint)
            return EXIT_TIMEOUT
        time.sleep(0.5)
    sys.stdout.write(text)
    return 0


def cmd_stop(args):
    d = session_dir(args.session)
    with transaction(d) as st:
        if st["status"] != "active":
            print(f"セッション {st['id']} はすでに終了しています")
            return 0
        pending = [s["seq"] for s in undelivered(st)]
        if pending and not args.force:
            raise UserError(
                f"受け取っていない送信があります(#{', #'.join(map(str, pending))})。"
                "wait で受け取ってから stop してください(捨ててよければ --force)")
        mark_stopped(st, "stop")
    info = read_server_info(d)
    if info:
        deadline = time.time() + STOP_GRACE_SEC + 5
        while time.time() < deadline:
            try:
                os.kill(info["pid"], 0)
            except OSError:
                break
            time.sleep(0.2)
        else:
            with contextlib.suppress(OSError):
                os.kill(info["pid"], signal.SIGTERM)
    print(f"セッション {args.session} を終了しました")
    return 0


def cmd_sessions(args):
    root = home()
    if not root.is_dir():
        print("セッションはありません")
        return 0
    cleanup(root)
    rows = []
    for d in root.iterdir():
        if not (d / "state.json").is_file():
            continue
        try:
            st = read_state(d)
        except (OSError, ValueError):
            continue
        rows.append((st["last_activity"], d, st))
    if not rows:
        print("セッションはありません")
        return 0
    for _, d, st in sorted(rows, key=lambda r: r[0], reverse=True):
        current = open_form(st)
        parts = [st["id"]]
        if st["status"] != "active":
            parts.append("終了")
        elif server_alive(d, st):
            parts.append("動作中")
        else:
            parts.append("サーバ停止中(post / wait で起動し直す)")
        parts.append(f"最終操作 {ago(st['last_activity'])}")
        parts.append(f"回答待ち: フォーム {current['definition']['id']}" if current else "開いているフォームなし")
        parts.append(f"未受け取りの送信 {len(undelivered(st))}")
        if st["status"] == "active" and st.get("port"):
            parts.append(server_url(st))
        print("  ".join(parts))
    return 0


def cmd_history(args):
    d = session_dir(args.session)
    st = read_state(d)
    blocks = []
    for s in st["submissions"]:
        mark = "[受け取り済み]" if s["delivered_at"] is not None else "[未受け取り: wait で受け取れます]"
        blocks.append(mark + "\n" + s["text"])
    print("\n---\n\n".join(blocks) if blocks else "まだ送信はありません")
    current = open_form(st)
    if current:
        print(f"\n(回答待ち: フォーム {current['definition']['id']}、{len(current['definition']['questions'])} 問)")
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("post", help="フォームを出す・差し替える")
    p.add_argument("file", help="フォーム定義の JSON ファイル。- なら標準入力")
    p.add_argument("--session", help="既存のセッション id。省略すると新しいセッションを作る")
    p.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("wait", help="送信を 1 回分受け取る")
    p.add_argument("--session", required=True)
    p.add_argument("--timeout", type=float, help="待つ秒数。省略すると送信まで待ち続ける")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("stop", help="セッションを終える")
    p.add_argument("--session", required=True)
    p.add_argument("--force", action="store_true", help="受け取っていない送信があっても終える")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("sessions", help="セッションの一覧")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("history", help="これまでの送信をすべて出し直す")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("_serve", help=argparse.SUPPRESS)
    p.add_argument("session")
    p.set_defaults(func=lambda a: serve(a.session))

    args = parser.parse_args()
    try:
        return args.func(args)
    except UserError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
