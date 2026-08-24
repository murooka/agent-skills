---
name: git-wt
description: git wt (k1LoW/git-wt) による worktree の作成・切り替え・マージ・削除の手順と、Claude Code のハーネスによる作業ディレクトリ固定との兼ね合い。トリガー — 「git wt」「worktree を作って / 切り替えて / 消して」「worktree でマージして」、worktree 内での作業を指示されたとき、`git wt` が exit 0 なのに cwd が変わらないとき、worktree 作成が "already exists" で失敗したとき、新しい worktree で依存が無くてテスト・ビルドが動かないとき、worktree で直した `.env` が本チェックアウトに反映されないとき。
---

# git-wt

[git-wt](https://github.com/k1LoW/git-wt) は `git worktree` を扱いやすくする Git サブコマンド。基本操作は `git wt -h` に全部書いてあるので **まずそれを読む**。ここには `-h` に載っていない、Claude Code から使うときだけ問題になることを書く。

`wt.basedir` は Claude Code との相互運用のため **`.claude/worktrees`** にする。作業前に `git config wt.basedir` を確認し、未設定・別値なら `.claude/worktrees` に設定する。EnterWorktree ツールが `path` 指定で入れる対象を `.claude/worktrees/` 配下に限っているため、この一致が下記の「セッションごと移動する」手段を成立させている。

## 最重要: `git wt <name>` はセッションの cwd を動かさない

**Bash ツール内の `cd` は呼び出しを跨いで残らない。** ハーネスが毎回セッションの作業ディレクトリへ戻す（戻したときは `Shell cwd was reset to <path>` が出る。この `<path>` が現在のセッション作業ディレクトリ）。worktree に限った話ではなく `cd` 全般がそう。

`git wt` のシェル統合は「`git-wt` が最終行に出力したパスへ `cd` する」`git()` ラッパー関数として動くので、この影響をまともに受ける:

- `git wt <name>` は **exit 0・出力なし・cwd も変わらない**ように見える。ラッパーは `cd` を発行し（ラッパーはパスの行を出力から削るので無出力になる）、直後にハーネスが戻す。失敗ではないのでエラーを探しても見つからない。
- 1 回の Bash 呼び出しの **中** では `cd` は効いている（`cd <path> && pwd` は移動先を表示する）。効かないのは呼び出しを跨いだときだけ。

つまり `git wt` は **worktree の作成・削除には使えるが、セッションの移動には使えない**。

### 移動する手段

| やりたいこと | 手段 |
|---|---|
| 別 worktree で数コマンド叩くだけ | `git -C <path> ...`（最も軽い。移動しない） |
| セッションごと別 worktree へ入る | **EnterWorktree** に `path` を渡す（`.claude/worktrees/` 配下なら、起動時に worktree へ固定されたセッションからでも入れる） |
| worktree から出て元の起動ディレクトリへ戻る | **ExitWorktree** に `action: "keep"`（worktree は disk に残る） |

```bash
# ダメ: cwd は動かないので merge は元の場所で走る
git wt main
git merge --ff-only feature-x

# よい: 移動せずに済ませる
git -C <repo-root> merge --ff-only feature-x
```

EnterWorktree が入れるのは `.claude/worktrees/` 配下の worktree だけ。**本チェックアウト（リポジトリルート）へは `path` 指定では入れない**ので、そこへ戻りたいなら ExitWorktree を使う。

**報告のしかた**: `git wt` が exit 0 でも「切り替えました」と書かない。`pwd` と `git branch --show-current` で確認してから書く。`git -C` で代替したならそう伝える。結果は同じでも、次に同じ操作をするときの前提が変わるため。

## worktree 作成が "already exists" で落ちるとき

```
fatal: '.../.claude/worktrees/foo' already exists
Error: failed to create worktree: exit status 128
```

`.claude/worktrees/foo` にディレクトリはあるが **worktree として登録されていない** 状態。過去のセッションが「worktree のつもりのパス」に直接ファイルを書くと起きる（セッションの作業ディレクトリがそのパスに設定されていて、実際には worktree が無かったケース）。

```bash
git worktree list       # そのパスが出てこない → 未登録
find <path> -type f     # 何が入っているか
```

**消す前に必ず中身を見る。** 本来の場所と突き合わせて、重複だと確認できてから動かす:

```bash
diff -q <path>/<relative> <repo-root>/<relative>
```

削除ではなく scratchpad へ退避する。判断を誤っても戻せるし、ユーザーが中身を確認できる。何をどこへ退避したかは必ず報告する。

## 何がコピーされ、何がされないかを最初に確認する

`wt.basedir` 以外の設定はリポジトリごとに違うので、worktree で作業を始める前に読む:

```bash
git config --get-regexp '^wt\.'
```

見るべきキーと、そこから来る落とし穴:

- **`wt.copyignored true`** — `.env` 等の gitignore 対象ファイルが **コピー** される。symlink ではなく別 inode なので、**worktree 側で編集しても本チェックアウトには反映されない**。秘密情報や gitignore 対象ファイルの修正が必要という結論になったら、「本チェックアウト側で直してください」と明示して伝える。worktree で直して満足しないこと。
- **`wt.nocopy` に `node_modules/` 等がある** — 依存はコピーされない。新しい worktree でテスト・型チェック・dev サーバを回す前に install が要る。逆に、YAML やドキュメントしか触っていないなら install は不要（無駄に数分かかる）。
- **`wt.symlink`** — 上の 2 つと逆に、対象ディレクトリは **共有** される。ここへの書き込みは全 worktree に及ぶので、隔離されているつもりで壊さない。
- **`wt.hook`** — worktree 新規作成時にだけ走る。既存 worktree への切り替えでは走らないので、hook が install を担っている場合でも切り替え時は自分で確認する。

## 典型的な流れ

```bash
git wt <name> --nocd                       # 作成のみ。--nocd で「動かない」ことを明示する
```
→ **EnterWorktree** に `path: <basedir>/<name>` を渡してセッションごと入る
→ `pwd && git branch --show-current` で入れたことを確認
→ 作業してコミット
```bash
git -C <repo-root> status --short --branch # 本体がクリーンか
git -C <repo-root> merge --ff-only <name>  # 分岐していれば止まる
```
→ 戻るなら **ExitWorktree** の `action: "keep"`
```bash
git wt -d <name>                           # マージ済みのみ安全削除 (-D で強制)
```

`--ff-only` を付けて、意図しないマージコミットを防ぐ。ff できずに止まったら本体側が進んでいるということなので、rebase するか merge commit を作るかをユーザーに確認する。

`main` / `master` は削除・リネームから保護されている（`--allow-delete-default` で解除）。
