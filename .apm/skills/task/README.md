# task

Task (taskfile.dev) の Taskfile.yml を設定・管理するためのスキル。

## 解決する課題

Taskfile の構文(依存関係の並列実行、fingerprinting による変更検知、CLI 引数の受け渡し、defer によるクリーンアップなど)は都度公式ドキュメントを引かないと正確に書けず、`desc` の欠落など一覧性を損なう定義も生まれやすい。

## アプローチ

初期化テンプレート、タスク追加時の規約、主要パターンのクイックリファレンスを SKILL.md に、完全な構文リファレンスを `references/taskfile-syntax.md` に置く。
