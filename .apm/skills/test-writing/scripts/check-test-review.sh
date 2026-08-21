#!/bin/bash
# Stop hook: テストファイルに未レビューの変更があるとき、ターン終了を block して
# test-writing スキルのレビューゲート(Step 5)の実施を要求する。
# レビューゲート完了は stamp-review.sh が記録するスタンプで判定する。
# 連続 block は Claude Code 側の上限(既定 8 回)で自動解除されるため、無限ループにはならない。
set -u

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" 2>/dev/null || exit 0
git_dir=$(git rev-parse --git-dir 2>/dev/null) || exit 0

# テストファイルとみなすパスのパターン(拡張正規表現)。消費側の慣習に応じて調整可。
test_pattern='((^|/)(tests?|__tests__|spec)/|_test\.[A-Za-z0-9]+$|\.test\.[A-Za-z0-9]+$|\.spec\.[A-Za-z0-9]+$|(^|/)test_[^/]+\.py$|_spec\.rb$)'

changed=$(
  {
    git diff --name-only
    git diff --name-only --cached
    git ls-files --others --exclude-standard
  } 2>/dev/null | sort -u | grep -E "$test_pattern"
) || true
[ -z "$changed" ] && exit 0

stamp="$git_dir/test-review-stamp"
if [ -f "$stamp" ]; then
  stale=0
  while IFS= read -r f; do
    if [ -e "$f" ] && [ "$f" -nt "$stamp" ]; then
      stale=1
      break
    fi
  done <<<"$changed"
  [ "$stale" -eq 0 ] && exit 0
fi

{
  echo "テストファイルに変更がありますが、test-writing スキルのレビューゲートが未実施です。"
  echo "対象:"
  echo "$changed" | head -10 | sed 's/^/  - /'
  echo "終了する前に次を実施してください:"
  echo "1. test-writing スキルの Step 5(レビューゲート)を実施する — references/review-checklist.md の全文と、テスト変更の diff、テスト対象の契約(シグネチャ・doc・仕様)をサブエージェントに渡して審査させ、指摘に対応する。会話の経緯と実装本体は渡さないこと。"
  echo "2. 完了後、test-writing スキルの scripts/stamp-review.sh を実行してスタンプを記録する。"
} >&2
exit 2
