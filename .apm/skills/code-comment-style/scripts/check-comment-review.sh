#!/bin/bash
# Stop hook: コードファイルに未レビューの変更があるとき、ターン終了を block して
# code-comment-style スキルの評価(変更完了モード)または非該当報告の実施を要求する。
# 完了は stamp-review.sh が記録するスタンプで判定する。
# 連続 block は Claude Code 側の上限(既定 8 回)で自動解除されるため、無限ループにはならない。
set -u

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" 2>/dev/null || exit 0
git_dir=$(git rev-parse --git-dir 2>/dev/null) || exit 0

# コメント規範の対象とみなすファイル(拡張正規表現)。消費側の言語構成に応じて調整可。
code_pattern='\.(go|py|rb|rs|ts|tsx|js|jsx|mjs|cjs|java|kt|kts|swift|c|h|cc|cpp|hpp|cs|php|scala|ex|exs|sql|sh|bash|zsh|lua|dart|vue|svelte|tf|proto|yaml|yml|toml|ini)$|(^|/)(Dockerfile|Makefile|Taskfile\.ya?ml)$'
# 生成物・ロックファイルは対象外
exclude_pattern='(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|composer\.lock)$|\.min\.(js|css)$|(^|/)(node_modules|vendor|dist|build)/'

changed=$(
  {
    git diff --name-only
    git diff --name-only --cached
    git ls-files --others --exclude-standard
  } 2>/dev/null | sort -u | grep -E "$code_pattern" | grep -vE "$exclude_pattern"
) || true
[ -z "$changed" ] && exit 0

stamp="$git_dir/comment-review-stamp"
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
  echo "コードファイルに変更がありますが、code-comment-style スキルのコメント評価が未実施です。"
  echo "対象:"
  echo "$changed" | head -10 | sed 's/^/  - /'
  echo "終了する前に次を実施してください:"
  echo "1. code-comment-style スキルの「変更完了モード」の起動条件を判定する。該当するなら、規範(references/norms.md)のパスを渡したサブエージェントに評価を委譲し、指摘に対応する。非該当なら、どの条件にも該当しなかったことを報告する。"
  echo "2. いずれの場合も、完了後に code-comment-style スキルの scripts/stamp-review.sh を対象リポジトリで実行してスタンプを記録する。"
} >&2
exit 2
