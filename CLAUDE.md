# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## このリポジトリの位置づけ

自作スキルを一箇所に集約し、[microsoft/apm](https://github.com/microsoft/apm)（Agent Package Manager）経由で複数プロジェクトから利用可能にするための **skill repository**。リポジトリ全体が 1 つの apm パッケージ（`apm.yml` + `.apm/`）として配布される。

ここはスキルの「配布元」であって、アプリケーションコードは持たない。ビルド・テスト・実行のパイプラインは存在せず、成果物は `.apm/skills/<skill-name>/SKILL.md` というテキストそのもの。したがって作業の中心は「コードが動くか」ではなく **「エージェントが正しく起動し、正しく振る舞うか」** の検証になる。

## レイアウト

```
apm.yml                 # パッケージマニフェスト
.apm/
  skills/
    <skill-name>/
      SKILL.md          # 必須。frontmatter + 本文
      references/       # 任意。オンデマンドで読ませる詳細規範
      scripts/          # 任意。実行可能ヘルパー
      assets/           # 任意。テンプレート・雛形
```

1 スキル = 1 ディレクトリ。ディレクトリ名と frontmatter の `name` は**必ず一致**させる（apm 側の検証対象）。`.apm/` 配下には `agents/` `instructions/` `prompts/` `hooks/` も置けるが、本リポジトリは skills に限定する（`apm.yml` の `type: skill` がこれを宣言している）。

## SKILL.md の契約

frontmatter の必須フィールドは `name` と `description` の 2 つのみ。

- `name` — 小文字英数字とハイフンのみ（`a-z`, `0-9`, `-`）、1〜64 文字。ディレクトリ名と一致。
- `description` — 1024 文字未満。**いつ起動すべきか**を意図ベースで書く。これはドキュメントの要約欄ではなく、エージェントが起動判定に使う唯一の手がかりなので、機能説明（「〜を支援します」）ではなくトリガー列挙（「次のときに使う — (1)…、(2)…」「〜と言われたときも使う」）で書く。既存スキル（`~/.claude/skills/` 配下）がこの書き方の実例になる。

本文が長くなる場合は `references/` に切り出し、SKILL.md からはパスで参照させる。SKILL.md 本体は「何を読むべきか」「どの順で動くか」の骨格に留め、詳細規範を丸ごと抱え込まない。

## apm.yml

`includes: auto` はローカルコンテンツを配布する明示的な同意にあたる。これを外すとレガシーな暗黙同意扱いになり `apm audit` が advisory を出すので、消さない。

スキルを追加しても `apm.yml` に列挙する必要はない（`.apm/skills/` 配下が自動的に対象になる）。更新が要るのは `version` — 公開時は `version` を上げ、同じ値の `v` 付きタグ（`v0.2.0` など）を切る。消費側はこのタグで固定する。

## 消費側からの参照

```bash
apm install murooka/agent-skills                      # 全スキル
apm install murooka/agent-skills#v0.1.0               # タグ固定
apm install murooka/agent-skills --skill dig          # 個別スキルのみ（--skill は繰り返し可）
```

`--skill` の選択は**インストールをまたいで加算される**（union）。一度入れたスキルは、消費側の `apm.yml` を直接編集しない限り外れない。全体に戻すには `--skill '*'`。

消費側 `apm.yml` に書く場合:

```yaml
dependencies:
  apm:
    - murooka/agent-skills#v0.1.0          # 全スキル
    - git: https://github.com/murooka/agent-skills.git
      ref: v0.1.0
      skills: [dig, task]                  # 個別選択
```

## 検証

apm CLI はローカル未インストール（2026-08 時点）。検証コマンドを使うには導入が必要:

```bash
apm compile --validate   # 全 primitive の frontmatter と構造を解析、出力せずエラー報告
apm compile --dry-run    # 配置先の決定内容だけを表示
apm view agent-skills    # 消費側から見えるメタデータと primitive 数を確認
apm audit                # 隠しUnicode検査 + 配置済みコンテキストとの差分検出
apm pack                 # build/agent-skills-<version>/ にプラグイン形式で出力
```

推奨順序は validate → dry-run → view → audit → pack。CLI に頼らない場合でも、frontmatter の `name` とディレクトリ名の一致は最低限手で確認する。

## スキルを書く・直すとき

`skill-creator` スキルが利用可能なので、新規作成・改訂時はまずそれを起動する。作成・大幅改訂の直後は `empirical-prompt-tuning` で、description の起動精度と本文の指示の曖昧さを実測して詰める（このリポジトリの成果物は指示テキストそのものなので、この工程が実質的な「テスト」にあたる）。
