# normfix

A zero-dependency Python auto-formatter and linter for the **42 School C Norm (v4.1)**.

`normfix` mechanically fixes formatting, spacing, alignment, declarations, and 42 headers in `.c` and `.h` files—then runs a built-in linter and reports any remaining structural errors (such as functions exceeding 25 lines or forbidden control structures) in standard `norminette` format.

## Features

- **Safe 25-Line Guard:** Fixes that add extra lines (splitting `int i = 0;`, moving `{` to its own line, breaking up one-line `if` statements) are **only applied if the function stays within 25 lines**. Otherwise, the code is left untouched and the exact error location is reported.
- **Whitespace & Indentation:** Converts leading spaces to 4-column tabs, strips trailing whitespace, removes consecutive blank lines, and properly indents nested blocks, multi-line conditions, and preprocessor directives (`#ifdef`, `# define`, etc.).
- **Operator & Return Spacing:** Normalizes spacing around binary/unary operators, casts, keywords (`if (`, `while (`, `sizeof(`), commas, and wraps `return` expressions in parentheses (`return (val);`).
- **Declarations & Alignment:**
  - Moves mid-function declarations to the top of the function.
  - Splits multiple declarations (`int a, b;`) onto separate lines.
  - Separates variable declarations from assignments (`int i = 0;` $\rightarrow$ `int i;` + `i = 0;`) while preserving `static`, `const`, and array/struct initializers.
  - Inserts the mandatory blank line between declarations and statements.
  - Tab-aligns variable names, struct/union members, `typedef` names, and function prototypes.
- **Headers & Guards:**
  - Generates or updates the standard 42 ASCII header (`--login` / `--mail`).
  - Auto-generates and normalizes `#ifndef FILENAME_H` / `# define FILENAME_H` / `#endif` include guards in `.h` files.
- **Built-in Linter:** Checks for remaining Norm v4.1 violations (`TOO_MANY_LINES`, `TOO_MANY_VARS_FUNC`, `TOO_MANY_ARGS`, `TOO_MANY_FUNCS`, `FORBIDDEN_CS`, `TERNARY_FBIDDEN`, `VLA_FBIDDEN`, `LINE_TOO_LONG`, `WRONG_SCOPE_COMMENT`, etc.).

---

## Installation

No external packages or dependencies are required (built entirely using the Python 3 standard library).

```bash
clone the repo
cd normfix
chmod +x normfix.py

Optional: Symlink it into your local binary directory so you can run normfix from anywhere:
mkdir -p ~/.local/bin
ln -s "$(pwd)/normfix.py" ~/.local/bin/normfix

Usage
normfix can target individual .c / .h files or recursively scan entire directories.
1. Basic In-Place Formatting
Fix a single file or multiple directories in place:
normfix main.c
normfix src/ includes/

2. Check Only (Dry Run)
Run the linter without modifying files on disk (useful for CI/CD pipelines or pre-commit hooks):
normfix -n src/

3. Preview Changes (Diff View)
Inspect exact line-by-line changes via a unified diff before writing to files:
normfix -d main.c

4. Managing 42 Headers
Automatically insert or update the official 42 ASCII header using your login and student email:
normfix --login jdoe --mail jdoe@student.42nice.fr main.c

> Pro Tip: Set your default login as an environment variable in your .bashrc or .zshrc so you never have to pass --login manually:
> export USER42="jdoe"
> 
> Once exported, normfix picks it up automatically. If you want to leave a file without a header or skip header generation, use --no-header.
> 
5. Allowing Global Variables
By default, global variables trigger a GLOBAL_VAR_DETECTED warning. If your project instructions explicitly permit globals, you can bypass this check with:
normfix --allow-globals src/

CLI Options Reference
| Option / Flag | Description |
|---|---|
| paths | One or more .c/.h files or directories to process. |
| -n, --check | Do not write changes; check files and report remaining errors only. |
| -d, --diff | Print a unified diff of proposed changes to standard output without writing. |
| --login LOGIN | Specify your 42 login for the ASCII header (defaults to $USER42 or $USER). |
| --mail MAIL | Specify your student email for the header (defaults to <login>@student.42.fr). |
| --no-header | Do not auto-insert a missing 42 header. |
| --allow-globals | Suppress GLOBAL_VAR_DETECTED errors. |
Makefile Integration
You can easily integrate normfix directly into your project's Makefile to keep your codebase clean on every build:
NORM	= normfix
SRC_DIR = src
INC_DIR = includes

all:
	@$(NORM) $(SRC_DIR) $(INC_DIR)
	@echo "Normfix check completed."

check:
	@$(NORM) -n $(SRC_DIR) $(INC_DIR)

diff:
	@$(NORM) -d $(SRC_DIR)

Example Output
When running normfix, errors are reported in clean norminette style with exact file paths, error codes, line numbers, and column numbers:
src/main.c: OK!
src/parser.c: Error!
Error: TOO_MANY_LINES       (line:  14, col:   1):  Function has more than 25 lines (28)
Error: FORBIDDEN_CS         (line:  47, col:   5):  Forbidden control structure (for)
