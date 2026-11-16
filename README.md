# normfix

> Auto-fixer and checker for the **42 Norm (v4.1)**: it rewrites your C files to follow the Norm, adds/updates the 42 header, manages `#include`s across files, audits your Makefile, and compiles / runs your code with sanitizers to catch compile errors and segfaults. Single Python file, no dependencies.

Suggested GitHub topics: `42school` `norminette` `c` `formatter` `linter` `makefile` `cli`

---

## What it does

| Area | Behaviour |
|---|---|
| **Norm fixes** | Tabs/indentation, spacing around operators/keywords/commas, `return (x);`, braces on their own line, one declaration per line, declaration/assignment split, declarations moved to the top, aligned variable names, blank lines, `#` directive indentation, header guard name, blank line between functions, struct member alignment |
| **25-line safety** | A fix that needs extra lines (`int i = 0;` split, `if (x) {` brace split, `if (x) return (1);` split, blank line after declarations...) is **only applied if the function stays ≤ 25 lines**. Otherwise the code is left untouched and the error is printed with its line, flagged *"not auto-fixed: it would make the function exceed 25 lines"* |
| **Reported, never changed** | `for` / `switch` / `case` / `goto` / `do`, ternaries, VLAs, comments inside functions, > 25 lines, > 4 params, > 5 variables, > 5 functions per file, lines > 80 columns, naming prefixes (`s_` `t_` `u_` `e_`), uppercase identifiers/macros, structs in `.c`, multiline macros, `#include "*.c"` |
| **42 header** | Added if missing (your login/mail, file name, launch time). Updated if present (see below) |
| **Includes** | `--add-include`, `--rename-include`, `--remove-include` on every processed file |
| **Makefile** | Generic checks for any project (not only libft), including a real build/relink/clean/fclean/re test in a temp copy |
| **Compile check** | Every `.c` compiled with `-Wall -Wextra -Werror` (configurable) |
| **Runtime check** | `--run`: build with AddressSanitizer + UBSan, run, report segfaults, memory errors, leaks, undefined behaviour |

Errors are printed like norminette, with line numbers matching the **fixed** file:

```
ft_split.c: Error!
Error: TOO_MANY_LINES       (line:  18, col:   1):	Function has more than 25 lines (27)
Error: DECL_ASSIGN_LINE     (line:  12, col:   1):	Declaration and assignation on a single line (not auto-fixed: it would make the function exceed 25 lines)
```

---

## Setup

Requirements: Python ≥ 3.8. Optional: `cc` (compile check), `make` (Makefile checks), a compiler with ASan/UBSan (`--run`).

### Option A: alias (works from any directory)

```sh
mkdir -p ~/tools && cp normfix.py ~/tools/normfix.py
echo "alias normfix='python3 ~/tools/normfix.py'" >> ~/.zshrc     # or ~/.bashrc
source ~/.zshrc
```

### Option B: command on your PATH

```sh
mkdir -p ~/.local/bin
cp normfix.py ~/.local/bin/normfix && chmod +x ~/.local/bin/normfix
# make sure ~/.local/bin is in your PATH:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

Then, from any project:

```sh
cd ~/my_project
normfix            # no argument = current directory (recursive over .c / .h)
```

Nothing is tied to a folder name: the Makefile is looked up next to your files (or one level up), headers/include dirs are discovered from the project, and every path is relative to where you run the command.

### Set your identity once

```sh
normfix --login fgirault --mail fgirault@student.42.fr --save
normfix --show-config
```

Saved in `~/.config/normfix/config.json`.

**Changing login/mail (e.g. someone else uses the script):**

```sh
normfix --login other --mail other@student.42.fr --save    # replace the saved identity
normfix --login other --mail other@student.42.fr           # one-off, not saved
NORMFIX_LOGIN=other NORMFIX_MAIL=other@student.42.fr normfix
rm ~/.config/normfix/config.json                           # forget it
```

Precedence: command line > `NORMFIX_LOGIN`/`NORMFIX_MAIL` > saved config > login/mail found in an existing 42 header of the processed files > `$USER` (mail falls back to `<login>@student.42.fr`).

---

## Usage

```sh
normfix                         # fix the current directory (asks for confirmation)
normfix file.c other.c          # specific files
normfix -d                      # show the diff, change nothing
normfix -n                      # only check and report, change nothing
normfix -y                      # apply without asking
```

### Confirmation

Before writing anything, normfix lists the files it would modify and what changes:

```
3 file(s) would be modified:
  ./ft_atoi.c   header updated; +include libft.h; -include header.h; norm fixes
  ./libft.h     42 header added; norm fixes
  ./noh.c       42 header added; +include libft.h; norm fixes

Apply? [y/N/d=show diff]
```

`y` applies, `d` shows the diff, anything else cancels. In a non-interactive shell (pipe, cron) it writes nothing unless you pass `-y`. Cancelling still reports the errors found in the *fixed* text. The Makefile test always runs in a temporary copy and never touches your folder.

### Includes

```sh
normfix --rename-include header.h=libft.h              # header was renamed by the subject
normfix --add-include libft.h --add-include '<stdlib.h>'
normfix --remove-include header.h
normfix --add-include libft.h --include-headers        # also add to .h files
```

### Makefile

```sh
normfix                                # auto-detected
normfix --makefile path/to/Makefile
normfix --no-makefile
normfix --no-make-run                  # static checks only, do not run make
```

Checks: `$(NAME)`, `all`, `clean`, `fclean`, `re`, rules for each binary (`NAME_1`, `NAME_2`...) in multi-binary projects, `all` is the default rule, no `wildcard` / `*.c` / `*.o`, `-Wall -Wextra -Werror` in `CFLAGS`, listed sources exist, `.PHONY`, sub-library with its own Makefile must be built with `make -C`. Then, in a temp copy: `make` builds, a second `make` must not relink or rebuild anything, `clean` leaves no `.o`, `fclean` removes the target, `re` works. Sources present in the folder but absent from the Makefile are shown as notices (test files, for instance).

### Compile and crash checks

```sh
normfix --cflags "-Wall -Wextra -Werror" --cc gcc    # defaults shown; add --save to keep them
normfix --no-compile
normfix --run --test test_main.c                     # ASan + UBSan, runs your main
normfix --run --test test_main.c --args "42 abc" --timeout 10 --ldflags "-lm" --show-output
```

`--run` compiles all given `.c` files (ignoring any `main` they contain when `--test` is given) with `-fsanitize=address,undefined`, runs the binary, and reports: killed by signal, AddressSanitizer errors (with function and `file:line`), leaks, undefined behaviour, timeouts. Without `--test` it only runs if exactly one file contains `main`.

---

## What happens when the header / include is already there?

**42 header**

| Situation | Behaviour |
|---|---|
| No header | Added (login, mail, file name, created = updated = launch time) |
| Valid header, file unchanged by normfix | Left exactly as is |
| Valid header, file content changed | `By:` and `Created:` kept. `Updated:` set to the launch time and your login |
| Valid header, file was renamed | File name inside the header corrected (and `Updated:` bumped) |
| Broken / hand-edited header (starts with `/* ***` but invalid) | **Not** replaced or duplicated; reported as `INVALID_HEADER` for you to fix |
| `--no-header` | Never adds a header (existing ones are still maintained) |

**Includes**

| Situation | Behaviour |
|---|---|
| `--add-include X`, `X` already included | Skipped (no duplicates) |
| `--add-include X`, `X` missing | Inserted after the last `#include` of the top block (or at the top after the header). In a `.h` file with a guard, after the `# define` line |
| `--add-include X` into `X` itself | Skipped |
| `--rename-include A=B` | Replaces `A` by `B` in existing include lines; files without `A` are untouched |
| `--add-include` on `.h` files | Skipped unless `--include-headers` |
| `--rename-include A=B` and `--add-include B` together | Rename first, then add only if still missing: no duplicate |

Because of the confirmation step you always see these changes before they are written.

---

## Exit code

`0` when every checked file, the Makefile and the optional run are clean, `1` otherwise, so it can be used in a pre-commit hook or a script.

## Limitations

- It is a line-based fixer, not a full C parser. Use `-d` the first time and keep your work in git.
- `*` and `&` spacing is only fixed when clearly binary; unusual layouts (function pointers, multi-line signatures, nested unbraced `if`/`else` chains) are left alone.
- Mid-function declarations are moved to the top only at function level, not inside nested blocks.
- Not a replacement for the official `norminette`: run it before pushing. Rules that need judgement (naming, useful comments, "carryall" functions) cannot be checked.
- The default mail is `<login>@student.42.fr`; set yours with `--mail ... --save` if your campus domain differs.
