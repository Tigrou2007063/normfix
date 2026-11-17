# normfix

One command to **fix and check your 42 projects**: the full Norm v4.1 for C, plus the usual 42 conventions for C++, Python, Rust and shell. It maintains the 42 header, manages `#include`s across files, audits your Makefile, enforces the subject's forbidden / authorised functions, compiles your code and runs it under sanitizers or valgrind to catch segfaults, leaks and data races.

Single Python file, no dependencies (external tools are used when installed, see below).

## What works for which language

The official **Norm / norminette only exists for C**. For the other languages 42 has no Norm, so normfix applies what the subjects and evaluations actually ask for.

| | C (`.c` `.h`) | C++ (`.cpp` `.hpp` `.cc` `.tpp`...) | Python (`.py`) | Rust (`.rs`) | Shell (`.sh`) |
|---|---|---|---|---|---|
| Auto-fix | **Full Norm v4.1** | trailing whitespace, includes | autopep8 (flake8 / pycodestyle, 79 cols) + ruff (unused imports); `--py-format` adds ruff format / black | `rustfmt` | trailing whitespace |
| Lint | Norm errors (norminette-style) | include guards | syntax, **flake8** (or ruff), `--mypy` | `cargo clippy -D warnings` (or `cargo check`) | `bash -n`, `shellcheck` |
| Compile | `cc -Wall -Wextra -Werror` | `c++ -Wall -Wextra -Werror -std=c++98` | `compile()` | cargo | n/a |
| `--run` | ASan+UBSan / TSan / valgrind | same | runs the script, reports the exception | `cargo run`, reports panics | n/a |
| 42 header | added by default | with `--header` | with `--header` | with `--header` (`//` style) | with `--header` |
| `--forbid`, includes | yes | yes | `--forbid` | `--forbid` | n/a |
| `--func-prefix` naming | yes | no | no | no | n/a |
| Makefile audit | yes | yes (+ `-std=c++98` check) | if there is one | if there is one | if there is one |

Other languages (Go, JS/TS, SQL...) are not supported yet, except for the optional header (`--header`, `--header-style`).

Tools are optional; normfix tells you which one is missing: `autopep8`, `ruff`, `flake8`, `mypy` (Python), `rustfmt`, `cargo`, `clippy` (Rust), `shellcheck`, `valgrind`, `nm` (binutils), `make`, `cc` / `c++`.

---

## Setup

Python >= 3.8.

```sh
mkdir -p ~/tools && cp normfix.py ~/tools/normfix.py
echo "alias normfix='python3 ~/tools/normfix.py'" >> ~/.zshrc     # or ~/.bashrc
source ~/.zshrc
```

Alternative (real command on your `PATH`):

```sh
mkdir -p ~/.local/bin && cp normfix.py ~/.local/bin/normfix && chmod +x ~/.local/bin/normfix
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

Then, from any project: `normfix` (no argument = current directory, recursive). Nothing depends on a folder name.

### Identity (login / mail)

```sh
normfix --login fgirault --mail fgirault@student.42.fr --save   # saved in ~/.config/normfix/config.json
normfix --show-config
normfix --login other --mail other@student.42.fr                # one run only
NORMFIX_LOGIN=other NORMFIX_MAIL=other@student.42.fr normfix    # via environment
rm ~/.config/normfix/config.json                                # forget
```

Precedence: command line > `.normfix.json` of the project > `NORMFIX_LOGIN` / `NORMFIX_MAIL` > saved config > login/mail of an existing 42 header in your files > `$USER` (mail falls back to `<login>@student.42.fr`).

### Per-project settings: `.normfix.json`

Put it at the root of a project. normfix looks for it upward from the target. Keys are the long option names with `_`:

```json
{
  "add_include": ["libft.h"],
  "rename_include": ["header.h=libft.h"],
  "allowed_functions": ["write", "malloc", "free"],
  "forbid": ["system"],
  "test": ["tests/main.c"],
  "ldflags": "-lm",
  "cflags": "-Wall -Wextra -Werror",
  "no_header": false
}
```

Command-line options are added to / override these. This is what makes it reusable: each project carries its own subject rules.

---

## Usage

```sh
normfix                     # fix everything below the current dir (asks first)
normfix a.c b.c             # specific files
normfix -d                  # show the diff, change nothing
normfix -n                  # report only, change nothing
normfix -y                  # apply without asking
normfix --lang cpp .        # force a language
```

Values that start with `-` need `=`: `--ldflags="-lm -lpthread"`, `--cflags="-Wall -Werror"`.

### Confirmation before writing

```
3 file(s) would be modified:
  ./ft_atoi.c   [c] header updated; +include libft.h; -include header.h; fixes
  ./libft.h     [c] 42 header added; fixes

Apply? [y/N/d=show diff]
```

`d` shows the diff, `y` applies, anything else cancels. In a non-interactive shell nothing is written unless `-y`. The Makefile test runs in a temporary copy and never touches your folder. Cancelling still reports the errors of the *fixed* text.

### What C fixes, and the 25-line rule

Indentation (tabs), spacing, `return (x);`, braces on their own line, one declaration per line, declaration/assignment split, declarations moved to the top, aligned names, blank lines, `#` indentation, header guard name, blank line between functions, struct member alignment, blank line after the preprocessor block.

A fix that needs extra lines (split `int i = 0;`, `if (x) {` brace split, `if (x) return (1);`, blank after declarations...) is **only applied if the function stays <= 25 lines**; otherwise the code is left alone and the error is printed with its line and *"not auto-fixed: it would make the function exceed 25 lines"*.

Reported but never changed: `for` / `switch` / `case` / `goto` / `do`, ternaries, VLAs, comments in functions, > 25 lines, > 4 params, > 5 variables, > 5 functions per file, lines > 80 cols, naming prefixes, uppercase identifiers and macros, structs in `.c`, multiline macros.

```
ft_split.c: Error!
Error: TOO_MANY_LINES       (line:  18, col:   1):	Function has more than 25 lines (27)
Error: DECL_ASSIGN_LINE     (line:  12, col:   1):	Declaration and assignation on a single line (not auto-fixed: it would make the function exceed 25 lines)
```

### 42 header: existing header / include behaviour

| Situation | Behaviour |
|---|---|
| No header (C / `.h`) | Added: login, mail, file name, created = updated = launch time |
| No header (other languages) | Only with `--header`. Not added if the file starts with `#!`. Comment style per extension (`/* */`, `//` for `.rs` `.js` `.ts` `.go`, `#` for `.py` `.sh`...), override with `--header-style '.ext=START,END,FILL'` |
| Valid header, file unchanged | Untouched |
| Valid header, file changed | `By:` / `Created:` kept; `Updated:` = launch time + current login |
| File renamed | Name inside the header corrected |
| Broken header (starts like one but invalid) | Not replaced, not duplicated: reported as `INVALID_HEADER` |
| `--no-header` | Never adds one |

Note: a 42 header is 80 columns wide, which flake8 reports as E501 in Python. normfix ignores that on the header lines only. Python projects usually do not use the header, hence opt-in.

| Include situation | Behaviour |
|---|---|
| `--add-include X`, already there | Skipped (no duplicate) |
| `--add-include X`, missing | Inserted after the last top `#include` (or at the top / after the guard define in a `.h`) |
| `--add-include` on `.h` files | Skipped unless `--include-headers` |
| `--rename-include A=B` | Replaces `A` by `B` where present, other files untouched |
| rename + add of the same name | Rename first, then add only if still missing |

```sh
normfix --rename-include header.h=libft.h            # the subject renamed the header
normfix --add-include libft.h --add-include '<stdlib.h>'
normfix --remove-include header.h
```

### Subject rules

```sh
normfix --allowed-functions write,malloc,free .      # C: any other external call is reported
normfix --forbid 'printf,using namespace,friend' .   # C++, Python, Rust, C
```

`--allowed-functions` compiles each `.c` and reads the undefined symbols with `nm`, so it only reports **real** calls (not comments or strings), with the line. Functions defined in the processed files are fine. Anything defined elsewhere (e.g. a `libft/` folder you did not pass) must be added to the list. Globals such as `stdout` or `errno` show up as symbols too.

### Function naming (`ft_`...)

```sh
normfix --func-prefix ft_ .                       # every function must start with ft_
normfix --func-prefix ft_ --skip-static .         # ...except static helpers
normfix --func-pattern 'ft_[a-z0-9_]+' .          # or a full regex
normfix --func-prefix ft_ --func-exempt main,debug_print .
```

Checked on function definitions in `.c` and on prototypes in `.h`; `main` is exempt by default. Reported as `FUNC_NAME_FORMAT` with the line, never auto-renamed (a rename would have to follow every call site). Put it in `.normfix.json` (`"func_prefix": "ft_"`) so each project carries its own rule: libft needs `ft_`, other projects usually do not.

### Makefile

Auto-detected next to the files or one level up (`--makefile`, `--no-makefile`, `--no-make-run`). Static: `$(NAME)`, `all`, `clean`, `fclean`, `re`, a rule per binary (`NAME_1`...), `all` is the default, no `wildcard` / `*.c` / `*.o`, `-Wall -Wextra -Werror` in `CFLAGS` / `CXXFLAGS`, `-std=c++98` when C++ sources exist, listed sources exist, `.PHONY`, sub-library built with `make -C`. Dynamic, in a temp copy: `make` builds, a 2nd `make` must not relink anything, `clean` leaves no `.o`, `fclean` removes the target, `re` works. Sources present but not listed are shown as notices.

### Compile, lint and runtime

```sh
normfix --run --test test_main.c                       # C/C++: ASan + UBSan, your main
normfix --run --test test_main.c --valgrind            # + leaks, invalid access, open fds
normfix --run --sanitize thread --ldflags="-lpthread"  # philosophers: data races
normfix --run --test script.py --args "42 abc"         # Python: runs it, reports the exception
normfix --run                                          # Rust: cargo run, reports panics
normfix --mypy .                                       # Python: mypy with the 42 flags
normfix --no-compile .
```

`--run` reports: killed by signal, AddressSanitizer / LeakSanitizer / ThreadSanitizer, undefined behaviour, valgrind leaks and fds, panics, uncaught exceptions, timeouts (`--timeout`, default 5 s). It runs the files **on disk** (apply the fixes first if you want them included). Without `--test`, C/C++ only runs if exactly one file has `main`.

## Exit code

`0` if everything checked is clean, `1` otherwise: usable in a pre-commit hook.

## Limitations

- The C fixer is line-based, not a full parser. Use `-d` the first time and keep your work in git.
- `*` / `&` spacing is only fixed when clearly binary; function pointers, multi-line signatures and nested unbraced `if`/`else` chains are left alone.
- Mid-function declarations are moved to the top at function level only.
- Rules that need judgement (naming quality, useful comments, "carryall" functions) cannot be checked. Run the official `norminette` before pushing.
- Python / Rust / shell rules are the common conventions (flake8 79 cols, rustfmt + clippy, shellcheck), not an official 42 Norm: check your subject.
- `mypy` and flake8 run on a temp copy of the file, so imports of sibling modules may not resolve.
- Default mail is `<login>@student.42.fr`; save yours with `--mail ... --save` if your campus differs.
