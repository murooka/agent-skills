---
name: task
description: "Task (taskfile.dev) タスクランナーの設定・管理。Taskfile.yml の作成、タスクの追加・変更、タスク実行を支援する。トリガー: (1) Taskfile.yml の作成・編集, (2) タスク定義の追加・変更, (3) タスクの実行・一覧表示, (4) ビルド自動化の設定。キーワード: task, taskfile, Taskfile.yml, タスク定義, タスクランナー"
---

# Task

Task (taskfile.dev) はMakeに代わるクロスプラットフォームのタスクランナー。YAMLベースの `Taskfile.yml` でタスクを定義する。

## Workflow

### 1. 既存Taskfileの確認

Taskfile.yml を変更する前に、必ず既存ファイルを Read で確認する。ファイルが存在しない場合のみ新規作成する。

### 2. Taskfile.yml の初期化

プロジェクトに Taskfile.yml がない場合:

```yaml
version: '3'

tasks:
  default:
    desc: Show available tasks
    cmds:
      - task --list
```

### 3. タスクの追加

タスク追加時は以下を守る:
- `desc` を必ず付ける（`task --list` で表示される）
- 依存関係は `deps` で定義（並列実行される）
- 順序が必要な場合は `cmds` 内で `task:` を使う
- ソースファイルの変更検知が有効な場合は `sources` と `generates` を設定

### 4. タスクの実行

```bash
task <task-name>           # タスク実行
task --list                # タスク一覧
task <name> -- <args>      # 引数付き実行（.CLI_ARGS で参照）
task --watch <name>        # ファイル変更監視モード
task --dry <name>          # ドライラン
```

## Key Patterns

### 依存タスク（並列実行）

```yaml
tasks:
  build:
    deps: [lint, test]
    cmds:
      - go build ./...
```

### 変更検知（fingerprinting）

```yaml
tasks:
  build:
    sources:
      - src/**/*.go
    generates:
      - bin/app
    cmds:
      - go build -o bin/app ./...
```

### CLI引数の受け渡し

```yaml
tasks:
  run:
    cmds:
      - go run ./cmd/app {{.CLI_ARGS}}
```

### 環境変数と.env

```yaml
version: '3'
dotenv: ['.env']

tasks:
  deploy:
    env:
      DEPLOY_ENV: production
    cmds:
      - ./deploy.sh
```

### クリーンアップ（defer）

```yaml
tasks:
  integration-test:
    cmds:
      - defer: docker compose down
      - docker compose up -d
      - go test ./integration/...
```

### 動的変数

```yaml
tasks:
  release:
    vars:
      VERSION:
        sh: git describe --tags --abbrev=0
    cmds:
      - echo "Releasing {{.VERSION}}"
```

## Reference

Taskfile.yml の完全な構文は [references/taskfile-syntax.md](references/taskfile-syntax.md) を参照。
