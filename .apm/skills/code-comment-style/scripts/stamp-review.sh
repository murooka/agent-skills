#!/bin/bash
# code-comment-style のコメント評価(または非該当報告)の完了を記録する。
# Stop hook (check-comment-review.sh) がこのスタンプとコードファイルの mtime を比較する。
set -u

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" 2>/dev/null || { echo "プロジェクトディレクトリに移動できません" >&2; exit 1; }
git_dir=$(git rev-parse --git-dir 2>/dev/null) || { echo "git リポジトリではありません" >&2; exit 1; }

touch "$git_dir/comment-review-stamp"
echo "コメント評価のスタンプを記録しました: $git_dir/comment-review-stamp"
