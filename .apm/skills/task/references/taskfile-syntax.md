# Taskfile.yml Syntax Reference

## Minimal Taskfile

```yaml
version: '3'

tasks:
  hello:
    cmds:
      - echo "Hello, World!"
```

## Full Structure

```yaml
version: '3'

# Global settings
output: interleaved  # interleaved | group | prefixed
method: checksum     # checksum | timestamp | none
run: always          # always | once | when_changed
interval: 5s         # watch mode polling interval
set: [errexit, pipefail]
shopt: [globstar]
silent: false
dotenv: ['.env', '.env.local']

# Global variables
vars:
  GREETING: Hello
  GIT_COMMIT:
    sh: git log -n 1 --format=%h

# Global environment variables
env:
  ENV_VAR: value

tasks:
  task-name:
    desc: Short description (shown in --list)
    summary: |
      Multi-line detailed summary
      shown with --summary flag
    aliases: [alias1, alias2]
    cmds:
      - command1
      - command2
    deps:
      - dep-task1
      - task: dep-task2
        vars:
          VAR: value
    dir: '{{.USER_WORKING_DIR}}'
    vars:
      LOCAL_VAR: value
      DYNAMIC_VAR:
        sh: echo "computed"
    env:
      MY_ENV: '{{.LOCAL_VAR}}'
    sources:
      - src/**/*.go
    generates:
      - bin/app
    method: checksum
    status:
      - test -f output.txt
    preconditions:
      - sh: test -f required.txt
        msg: "required.txt must exist"
    platforms: [linux, darwin, windows/amd64]
    internal: false
    silent: false
    interactive: false
    run: always
    ignore_error: false
    requires:
      vars: [REQUIRED_VAR]
```

## Command Variations

```yaml
tasks:
  examples:
    cmds:
      # Simple command
      - echo "hello"

      # Command with options
      - cmd: echo "hello"
        silent: true
        ignore_error: true
        platforms: [linux, darwin]

      # Call another task
      - task: other-task
        vars:
          KEY: value

      # Defer (runs on task exit, even on failure)
      - defer: rm -f temp.txt
      - defer:
          cmd: echo "cleanup"

      # For loop - static list
      - for: ['a', 'b', 'c']
        cmd: echo "{{.ITEM}}"

      # For loop - variable (string with split)
      - for:
          var: MY_LIST
          split: ','
        cmd: echo "{{.ITEM}}"

      # For loop - sources glob
      - for:
          sources:
            - src/**/*.go
        cmd: echo "{{.ITEM}}"

      # For loop - matrix
      - for:
          matrix:
            OS: [linux, darwin]
            ARCH: [amd64, arm64]
        cmd: echo "{{.ITEM.OS}}/{{.ITEM.ARCH}}"
```

## Variables

Resolution order (highest to lowest priority):
1. Task-level vars
2. Call parameters (vars passed when calling a task)
3. Global Taskfile vars
4. Environment variables

Special variables:
- `{{.TASK}}` - current task name
- `{{.USER_WORKING_DIR}}` - directory where `task` was invoked
- `{{.TASKFILE_DIR}}` - directory containing the Taskfile
- `{{.CLI_ARGS}}` - arguments after `--` (e.g., `task run -- --verbose`)
- `{{.ITEM}}` - current item in for loops
- `{{.MATCH}}` - captured wildcard segments in task names

Variable types: string, bool (sh syntax), int, float, array, map.

Pass non-string types by reference:
```yaml
vars:
  MY_LIST: [a, b, c]
tasks:
  example:
    cmds:
      - task: sub-task
        vars:
          LIST:
            ref: .MY_LIST
```

## Wildcards

```yaml
tasks:
  run-*:
    cmds:
      - echo "Running {{index .MATCH 0}}"
# task run-tests -> "Running tests"
```

## Output Grouping

```yaml
output: group

tasks:
  example:
    cmds:
      - command
    group:
      begin: '::group::{{.TASK}}'
      end: '::endgroup::'
```

## CLI Usage

```
task [task-name] [flags]

task                    # run default task
task name               # run specific task
task name -- args       # pass CLI args
task -l / --list        # list tasks with descriptions
task -a / --list-all    # list all tasks
task --summary name     # show task summary
task -w / --watch       # watch mode
task -n / --dry         # dry run
task -f / --force       # force run (ignore fingerprint)
task -g / --global      # use global Taskfile (~/)
task -d dir             # run in specific directory
task -o mode            # set output mode
task --status name      # check task status without running
```
