#!/bin/bash
# test-writing のレビューゲート(Step 5)完了を記録する。
# Stop hook (check-test-review.sh) がこのスタンプとテストファイルの mtime を比較する。
set -u

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" 2>/dev/null || { echo "プロジェクトディレクトリに移動できません" >&2; exit 1; }
git_dir=$(git rev-parse --git-dir 2>/dev/null) || { echo "git リポジトリではありません" >&2; exit 1; }

touch "$git_dir/test-review-stamp"
echo "レビューゲートのスタンプを記録しました: $git_dir/test-review-stamp"
