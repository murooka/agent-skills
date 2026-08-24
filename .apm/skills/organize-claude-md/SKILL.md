---
name: organize-claude-md
description: >
  Organize and slim down CLAUDE.md files by distributing rules to .claude/rules/ (file-specific)
  and subdirectory CLAUDE.md files (directory-specific), removing coding style rules in favor of
  linter commands, and trimming task runner commands. Use when CLAUDE.md is bloated, hard to
  maintain, or exceeds 50 lines. Triggers: /organize-claude-md, "organize claude.md",
  "slim down claude.md", "clean up claude.md"
---

# Organize CLAUDE.md

Reorganize CLAUDE.md files to be concise (<50 lines each) by distributing rules to the right locations.

## Workflow

### Step 1: Scan

1. Read the root CLAUDE.md
2. Find and read all subdirectory CLAUDE.md files: `find . -name "CLAUDE.md" -not -path "./.git/*"`
3. Find and read existing .claude/rules/ files: `ls .claude/rules/`
4. Identify project structure (monorepo packages, architecture directories, linters, task runners)

### Step 2: Categorize Each Rule

For every rule/instruction in each CLAUDE.md, assign one category:

| Category | Destination | Criteria |
|----------|-------------|----------|
| **file-specific** | `.claude/rules/<name>.md` with `paths` frontmatter | Applies to specific file types or file patterns (e.g., "*.test.ts files should...", "migrations must...") |
| **directory-specific** | `<dir>/CLAUDE.md` | Applies to a specific package, module, or architectural directory (e.g., "the API server uses...", "frontend components should...") |
| **coding-style** | **DELETE** | Formatting, naming conventions, import ordering — anything a linter/formatter enforces. Replace with a single line noting how to run the linter |
| **task-runner** | **TRIM** | Keep only 2-3 representative commands + how to list all commands (e.g., `make help`, `task --list`, `npm run`) |
| **general** | Root CLAUDE.md | Project-wide context that doesn't fit above: architecture overview, deployment notes, key decisions |

### Step 3: Present Plan

Show the user a structured plan before making changes:

```
## Proposed Changes

### Rules to move to .claude/rules/
- `test-conventions.md` (paths: "**/*.test.ts, **/*.spec.ts")
  - [list of rules being moved]

### Rules to move to subdirectory CLAUDE.md
- `packages/api/CLAUDE.md`
  - [list of rules being moved]

### Coding style rules to remove (covered by linter)
- [list of rules being removed]
- Will add: "Run `<lint command>` to check style"

### Task runner commands to trim
- Keeping: [representative commands]
- Removing: [verbose command lists]
- Will add: "Run `<list command>` to see all available commands"

### Resulting line counts
- CLAUDE.md: NN lines (was NN)
- .claude/rules/: N new files
- Subdirectory CLAUDE.md: N new/updated files
```

Ask the user to approve, modify, or reject.

### Step 4: Apply Changes

After approval:

1. Create `.claude/rules/` directory if needed
2. Write rule files with proper frontmatter:
   ```markdown
   ---
   description: Brief description of the rule
   paths: "pattern1, pattern2"
   ---

   Rule content here.
   ```
3. Create/update subdirectory CLAUDE.md files
4. Rewrite the root CLAUDE.md — aim for <50 lines
5. Show a summary of files created/modified

## Key Constraints

- Each CLAUDE.md must be under 50 lines
- Never delete information without relocating it (except coding style rules replaced by linter reference)
- Preserve the intent and specificity of every rule
- `.claude/rules/` files must have accurate `paths` globs
- If a linter/formatter config exists but no run command is documented, detect it (check package.json scripts, Makefile, Taskfile.yml, etc.)
