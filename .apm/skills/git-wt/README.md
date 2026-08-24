# git-wt

git wt (k1LoW/git-wt) を Claude Code から安全に使うための運用知識スキル。

## 解決する課題

Claude Code では Bash の `cd` がツール呼び出しを跨いで残らないため、`git wt <name>` が「exit 0 なのにセッションはどこにも移動していない」という分かりにくい状態になる。ほかにも worktree 作成の "already exists" 衝突、`wt.copyignored` でコピーされた `.env` を worktree 側で直しても本体に反映されない、依存がコピーされずビルドが落ちる、といった `git wt -h` には載っていない落とし穴がある。

## アプローチ

ハーネスとの兼ね合いで問題になる点だけに絞った手順集。セッション移動は EnterWorktree/ExitWorktree に任せる(そのために `wt.basedir` を `.claude/worktrees` に合わせる)、移動せず `git -C` で済ませる、`wt.*` 設定を作業前に読む、といった型を定める。
