# auto-memory 仕様の詳細と旧世代フォーマットのカタログ

scan 結果を解釈するための背景資料。**書式の正は実行中セッションの system prompt の Memory 節**であり、
この文書と食い違ったら system prompt に従う(ここは 2026-08 時点のスナップショット)。

## 公式仕様(出典: https://code.claude.com/docs/en/memory.md)

### 保存構造とロード

```
~/.claude/projects/<cwd のスラグ>/memory/
├── MEMORY.md          # インデックス。セッション開始時に先頭 200 行 or 25KB だけロードされる
└── <topic>.md         # 個別メモリ。ロードされず、必要時に Read されるオンデマンド方式
```

- スラグは cwd の `/` と `.` を `-` に置換したもの(非可逆。元パスのハイフンと区別できない)
- MEMORY.md の超過分は読まれない。個別ファイルにはサイズ制限なし
- ユーザーグローバルなメモリ置き場は存在しない。全てプロジェクト単位
- subagent には親セッションのメモリは渡らない
- `/compact` 後も MEMORY.md は再ロードされる(会話サマリーには含まれない)

### frontmatter の契約(現行)

```markdown
---
name: <kebab-case スラグ>
description: <1 行要約。想起時の関連判定に使われる>
metadata:
  type: user | feedback | project | reference
---

<事実。feedback / project は **Why:** と **How to apply:** を続ける。関連メモリは [[name]] でリンク>
```

- `type` の意味 — `user`: ユーザー自身(役割・専門性・好み)、`feedback`: ユーザーからの修正・確認済みのやり方、
  `project`: コードや git 履歴から導出できない進行中の作業・決定、`reference`: 外部リソースへのポインタ
- v2.1.214 以降、書き込み時に `metadata.modified`(ISO 8601)が自動付与される。鮮度判定に使える
- MEMORY.md は 1 行 1 メモリ: `- [タイトル](file.md) — 想起の手がかり`

### 記録しないことになっているもの(= 見つけたら削除・昇格候補)

- アーキテクチャやファイルレイアウトなど、コードから導出できるもの
- 過去のデバッグ修正(git 履歴に残る)
- CLAUDE.md が既に説明している内容(重複したら CLAUDE.md に一本化してメモリから消すのが公式推奨)

### 設定

| 設定 | 効果 |
|---|---|
| `autoMemoryEnabled: false` | auto-memory を無効化(user / project / local 各スコープ) |
| `autoMemoryDirectory: <path>` | 保存先の差し替え(絶対パスか `~/` 始まり) |
| 環境変数 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | 単一セッションで無効化 |

## 旧世代フォーマットのカタログ(実地で観測されたもの)

ハーネスの更新に伴い書式が変遷しており、古いメモリは以下の形で残っていることがある。
scan.py はこれらを `legacy-*` フラグで報告する。

### 第 1 世代(〜2026 年前半)

```markdown
---
name: User profile              # 人間可読タイトル(kebab でない)
description: ...
type: user                      # metadata: の下ではなくトップレベル
originSessionId: <uuid>         # トップレベルに置かれることがある
---
```

- ファイル名が `feedback_*.md` / `user_*.md` / `project_*.md` のアンダースコア接頭辞式
- MEMORY.md が `## User` `## Feedback` `## Project` のセクション分け式

### 第 2 世代(現行に近い)

- kebab-case ファイル名 + `metadata.type`。`metadata.node_type: memory` や
  `metadata.originSessionId` が付くことがある(害はないので正規化時に保持してよい)

### 逸脱形(世代ではなく違反)

- frontmatter なしの素の markdown
- 時系列の作業ログ・依頼ログ(「1 ファイル 1 事実」違反。整理時は今も真である結論だけを抽出する)
- 1 ファイルに独立した規範を 10 個以上列挙したもの(想起単位として粗すぎるので分割)

## 正規化の指針

- リネーム(`feedback_pnpm.md` → `prefers-pnpm.md` 等)したら MEMORY.md 再生成で索引も追従させる
- 旧 `type:` は `metadata.type` へ移す。`originSessionId` は `metadata:` 配下へ移すか、そのまま保持
- `name:` はファイル名(拡張子抜き)と一致する kebab-case スラグに揃える
- `modified` が無いファイルに手で付ける必要はない(次回ハーネスが書き込むときに付く)。
  鮮度判定にはファイルの mtime を使えばよい
