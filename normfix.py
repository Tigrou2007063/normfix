#!/usr/bin/env python3
"""
normfix - auto-fixer for the 42 Norm (v4.1)

Fixes what can be fixed mechanically, then lints the result and prints what is
left, norminette-style:

    file.c: Error!
    Error: TOO_MANY_LINES       (line:  12, col:   1):  Function has more than 25 lines

Rule of thumb: a fix that needs EXTRA lines (split `int i = 0;`, move a `{`
to its own line, split `if (x) return (1);` ...) is only applied if the
function stays <= 25 lines. Otherwise the code is left untouched and the error
is reported with the line where it is.

Usage:
    normfix.py file.c [dir ...]        fix in place
    normfix.py -n file.c               check only (no write), show remaining errors
    normfix.py -d file.c               show a diff instead of writing
    normfix.py --login flo --mail flo@student.42nice.fr file.c   (42 header)
"""
import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

TAB = 4
MAX_FUNC_LINES = 25
MAX_COLS = 80
MAX_PARAMS = 4
MAX_VARS = 5
MAX_FUNCS = 5

PH0 = 0xE000                  # placeholders for strings / chars / comments
PHC = '\uE000-\uEFFF'


# --------------------------------------------------------------------------
# Masking: replace string/char literals and comments by 1 private-use char
# --------------------------------------------------------------------------
def is_ph(c):
    return PH0 <= ord(c) < PH0 + 0x1000


def protect(line, in_cmt=False):
    """Return (masked_line, literals, still_in_block_comment)."""
    lits, out, i, n = [], [], 0, len(line)

    def put(txt):
        lits.append(txt)
        out.append(chr(PH0 + len(lits) - 1))

    while i < n:
        c = line[i]
        if in_cmt:
            j = line.find('*/', i)
            if j < 0:
                put(line[i:])
                i = n
            else:
                put(line[i:j + 2])
                i = j + 2
                in_cmt = False
        elif line.startswith('//', i):
            put(line[i:])
            i = n
        elif line.startswith('/*', i):
            j = line.find('*/', i + 2)
            if j < 0:
                put(line[i:])
                i = n
                in_cmt = True
            else:
                put(line[i:j + 2])
                i = j + 2
        elif c in '"\'':
            j = i + 1
            while j < n and line[j] != c:
                j += 2 if line[j] == '\\' else 1
            put(line[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return ''.join(out), lits, in_cmt


def restore(m, lits):
    return ''.join(lits[ord(c) - PH0] if is_ph(c) else c for c in m)


def is_comment_only(m):
    ms = m.strip()
    return bool(ms) and all(is_ph(c) or c.isspace() for c in ms)


def peel_comment(m, lits):
    """Split a trailing comment off a masked line -> (code, tail)."""
    tail = ''
    while m and is_ph(m[-1]) and lits[ord(m[-1]) - PH0].startswith(('/*', '//')):
        tail = m[-1] + tail
        m = m[:-1].rstrip()
    return m, tail


def match_paren(s, i):
    d = 0
    for j in range(i, len(s)):
        if s[j] == '(':
            d += 1
        elif s[j] == ')':
            d -= 1
            if d == 0:
                return j
    return -1


def split_top(s, sep=','):
    parts, depth, cur = [], 0, ''
    for c in s:
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        if c == sep and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += c
    parts.append(cur)
    return parts


# --------------------------------------------------------------------------
# Expression / statement spacing
# --------------------------------------------------------------------------
TYPEKW = ('int|char|short|long|float|double|void|unsigned|signed|const|static|'
          'struct|enum|union|volatile|register|extern|typedef')
OPS = re.compile(r'(->|\+\+|--)|(<<=|>>=|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|%=|'
                 r'&=|\|=|\^=|<<|>>|=|<|>|/|%|\^|\|)')
BIN_PM = re.compile(r'([\w\)\]' + PHC + r'])\s*([+\-])(?![+\-=>])\s*'
                    r'(?=[\w\(' + PHC + r'!~*&])')
BIN_AM = re.compile(r'([\w\]' + PHC + r'])\s*([*&])(?![*&=])\s*(?=[\w(' + PHC + r'])')
UNARY_CTX = re.compile(r'\b(return|case|sizeof|else|do)\s*$')


def _op_ctx(g, grp):
    if UNARY_CTX.search(g.string[:g.start(grp)]):
        return g.group(0)
    return g.group(1) + ' ' + g.group(2) + ' '


def fix_return(m):
    mm = re.match(r'^return\b\s*(.*?)\s*;$', m)
    if not mm:
        return m
    e = mm.group(1)
    if not e:
        return 'return ;'
    if e.startswith('(') and match_paren(e, 0) == len(e) - 1:
        return 'return ' + e + ';'
    return 'return (' + e + ');'


def fix_spacing(m):
    """Normalise spacing of a masked code fragment (no leading indent)."""
    m = re.sub(r'\s+', ' ', m.strip())
    m = OPS.sub(lambda g: g.group(1) or ' ' + g.group(2) + ' ', m)
    m = BIN_PM.sub(lambda g: _op_ctx(g, 2), m)
    m = BIN_AM.sub(lambda g: _op_ctx(g, 2), m)
    m = re.sub(r'\b(if|while|switch|for)\s*\(', r'\1 (', m)
    m = re.sub(r'\bsizeof\s+\(', 'sizeof(', m)
    m = re.sub(r'\b(?!(?:if|while|switch|for|return|else|do|case|sizeof|%s)\b)'
               r'(\w+)\s+\(' % TYPEKW, r'\1(', m)
    m = re.sub(r'\(\s+', '(', m)
    m = re.sub(r'\s+\)', ')', m)
    m = re.sub(r'\s*,\s*', ', ', m)
    m = re.sub(r',\s*$', ',', m)
    m = re.sub(r'\s+;', ';', m)
    m = re.sub(r';(?=[^\s;)])', '; ', m)
    m = re.sub(r'\s{2,}', ' ', m).strip()
    return fix_return(m)


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------
NOT_TYPE = {'return', 'if', 'else', 'while', 'for', 'do', 'switch', 'case',
            'goto', 'break', 'continue', 'sizeof', 'typedef', 'default'}
LHS = re.compile(r'^(?P<pre>.*?)(?P<stars>[\s*]*)(?P<name>[A-Za-z_]\w*)'
                 r'(?P<arr>(?:\s*\[[^\]]*\])*)$')
PRE_OK = re.compile(r'[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*$')


def parse_decl(m):
    """Parse 'type [*]name[arr] [= init][, ...];' -> (pre, [declarators]) or None."""
    m = m.strip()
    if not m.endswith(';') or m.startswith('#'):
        return None
    chunks = split_top(m[:-1], ',')
    lhs, eq, init = chunks[0].partition('=')
    if init.startswith('='):
        return None
    mm = LHS.match(lhs.strip())
    if not mm:
        return None
    pre = mm.group('pre').strip()
    if not pre or not PRE_OK.match(pre):
        return None
    if any(t in NOT_TYPE for t in pre.split()):
        return None
    ds = [{'stars': mm.group('stars').count('*'), 'name': mm.group('name'),
           'arr': re.sub(r'\s+', '', mm.group('arr')),
           'init': init.strip() if eq else None}]
    for ch in chunks[1:]:
        lhs, eq, init = ch.partition('=')
        m2 = re.match(r'^\s*(?P<stars>[\s*]*)(?P<name>[A-Za-z_]\w*)'
                      r'(?P<arr>(?:\s*\[[^\]]*\])*)\s*$', lhs)
        if not m2:
            return None
        ds.append({'stars': m2.group('stars').count('*'), 'name': m2.group('name'),
                   'arr': re.sub(r'\s+', '', m2.group('arr')),
                   'init': init.strip() if eq else None})
    return re.sub(r'\s+', ' ', pre), ds


def keeps_init(pre, d):
    toks = pre.split()
    return 'static' in toks or 'const' in toks or (d['init'] or '').startswith('{')


def decl_text(ds):
    parts = []
    for d in ds:
        s = '*' * d['stars'] + d['name'] + d['arr']
        if d['init'] is not None:
            s += ' = ' + fix_spacing(d['init'])
        parts.append(s)
    return ', '.join(parts) + ';'


def tabs_to(col, cur):
    return '\t' * max(1, -(-(col - cur) // TAB))


def align_col(widths):
    return (max(widths) // TAB + 1) * TAB


def fmt_params(p):
    p = p.strip()
    if p in ('', 'void'):
        return 'void'
    out = []
    for ch in split_top(p, ','):
        ch = ch.strip()
        mm = LHS.match(ch)
        if mm and mm.group('pre').strip() and PRE_OK.match(mm.group('pre').strip()):
            pre = re.sub(r'\s+', ' ', mm.group('pre').strip())
            out.append('%s %s%s%s' % (pre, '*' * mm.group('stars').count('*'),
                                      mm.group('name'),
                                      re.sub(r'\s+', '', mm.group('arr'))))
        else:
            out.append(re.sub(r'\s*\*\s*', ' *', re.sub(r'\s+', ' ', ch)).strip()
                       if '(' not in ch else re.sub(r'\s+', ' ', ch))
    return ', '.join(out)


SIG = re.compile(r'^(?P<pre>.*?)(?P<stars>[\s*]*)(?P<name>[A-Za-z_]\w*)\s*'
                 r'\((?P<params>.*)\)$')


def parse_sig(ms):
    """ms: masked stripped text without trailing ';' -> dict or None."""
    mm = SIG.match(ms)
    if not mm:
        return None
    pre = mm.group('pre').strip()
    if not pre or not PRE_OK.match(pre) or any(t in NOT_TYPE for t in pre.split()):
        return None
    return {'pre': re.sub(r'\s+', ' ', pre), 'stars': mm.group('stars').count('*'),
            'name': mm.group('name'), 'params': mm.group('params')}


# --------------------------------------------------------------------------
# Splitting one-liners (braces, control structures)
# --------------------------------------------------------------------------
def split_line(m):
    m = m.strip()
    if m.startswith('}') and len(m) > 1:
        rest = m[1:].strip()
        if rest and rest[0] not in ';,':
            return ['}'] + split_line(rest)
    if m.endswith('{') and len(m) > 1:
        before = m[:-1].strip()
        if before and not re.search(r'(?<![=!<>])=(?!=)', before):
            return split_line(before) + ['{']
    h = rest = None
    if re.match(r'^(else\s+if|if|while|for)\b', m):
        i = m.find('(')
        j = match_paren(m, i) if i >= 0 else -1
        if j >= 0:
            h, rest = m[:j + 1], m[j + 1:].strip()
    elif re.match(r'^else\b', m):
        h, rest = 'else', m[4:].strip()
    if h and rest and rest != ';':
        return [h] + split_line(rest)
    return [m]


# --------------------------------------------------------------------------
# Function bodies
# --------------------------------------------------------------------------
def build_items(body):
    items, cmt, depth = [], False, 0
    for raw in body:
        was = cmt
        m, lits, cmt = protect(raw, cmt)
        if was or is_comment_only(m):
            items.append({'k': 'cmt', 'raw': raw.rstrip(), 'verb': was})
            continue
        if not m.strip():
            continue                       # blank lines inside a function: dropped
        code, tail = peel_comment(m.strip(), lits)
        it = {'k': 'stmt', 'lits': lits, 'tail': tail, 'depth': depth}
        pd = parse_decl(code)
        if pd:
            it.update(k='decl', pre=pd[0], ds=pd[1])
        else:
            it['m'] = fix_spacing(code)
        depth += code.count('{') - code.count('}')
        items.append(it)
    return items


def lead_end(items):
    last = 0
    for i, it in enumerate(items):
        if it['k'] == 'decl' and it['depth'] == 0:
            last = i + 1
        elif it['k'] != 'cmt':
            break
    return last


BLANK = {'k': 'blank'}


def assign_item(d, lits, depth=0):
    return {'k': 'stmt', 'lits': lits, 'tail': '', 'depth': depth,
            'm': fix_spacing('%s = %s;' % (d['name'], d['init']))}


def apply_ops(items):
    def fits(new, old):
        return len(new) <= MAX_FUNC_LINES or len(new) <= len(old)

    # 1. declarations in the middle of the function -> move to the top
    failed = set()
    while True:
        end = lead_end(items)
        j = next((k for k in range(end, len(items))
                  if items[k]['k'] == 'decl' and items[k]['depth'] == 0
                  and id(items[k]) not in failed), None)
        if j is None:
            break
        it = items[j]
        top = dict(it, ds=[dict(d) for d in it['ds']], tail='')
        repl = []
        for d, td in zip(it['ds'], top['ds']):
            if d['init'] is not None and not keeps_init(it['pre'], d):
                td['init'] = None
                repl.append(assign_item(d, it['lits']))
        if repl:
            repl[-1]['tail'] = it['tail']
        new = items[:j] + repl + items[j + 1:]
        new.insert(end, top)
        if fits(new, items):
            items = new
        else:
            failed.add(id(it))

    # 2. empty line between declarations and the rest
    end = lead_end(items)
    if 0 < end < len(items) and items[end]['k'] != 'blank':
        new = items[:end] + [BLANK] + items[end:]
        if fits(new, items):
            items = new

    # 3. one declaration per line
    j = 0
    while j < len(items):
        it = items[j]
        if it['k'] == 'decl' and len(it['ds']) > 1:
            parts = [dict(it, ds=[d], tail='') for d in it['ds']]
            parts[-1]['tail'] = it['tail']
            new = items[:j] + parts + items[j + 1:]
            if fits(new, items):
                items = new
                j += len(parts)
                continue
        j += 1

    # 4. declaration + assignment on the same line
    end = lead_end(items)
    if end and end < len(items) and items[end]['k'] == 'blank':
        pos = end + 1
        for j in range(end):
            it = items[j]
            if it['k'] != 'decl' or it['depth'] != 0:
                continue
            for d in it['ds']:
                if d['init'] is None or keeps_init(it['pre'], d):
                    continue
                new = items[:pos] + [assign_item(d, it['lits'])] + items[pos:]
                if fits(new, items):
                    items = new
                    d['init'] = None
                    pos += 1

    # 5. braces / control structures on their own line
    j = 0
    while j < len(items):
        it = items[j]
        if it['k'] == 'stmt':
            parts = split_line(it['m'])
            if len(parts) > 1:
                news = [dict(it, m=fix_spacing(p), tail='') for p in parts]
                news[-1]['tail'] = it['tail']
                new = items[:j] + news + items[j + 1:]
                if fits(new, items):
                    items = new
                    continue                # re-examine the first new part
        j += 1
    return items


CONT_START = re.compile(r'^(&&|\|\||[+\-*/%|^<>?:]|==|!=)\s')
CONT_END = re.compile(r'(&&|\|\||,|[^=!<>+\-*/%&|^]=)$')


def render(items):
    base, stack, pending = 1, [], 0
    paren, hdr_flag, stmt_ind, cont = 0, False, 1, False
    for it in items:
        k = it['k']
        if k == 'blank':
            it['ind'], it['text'] = 0, ''
            continue
        if k == 'cmt':
            it['ind'] = base + pending
            it['text'] = it['raw'] if it['verb'] else it['raw'].strip()
            continue
        if k == 'decl':
            it['ind'] = base + pending
            pending = 0
            continue
        s = it['m']
        if paren > 0 or cont or CONT_START.match(s):
            ind = stmt_ind + 1
        elif s == '{':
            ind = base + max(pending - 1, 0)
            stack.append((ind, base))
            base, pending = ind + 1, 0
        elif s.startswith('}'):
            ind, b = stack.pop() if stack else (base - 1, base - 1)
            base, pending = b, 0
        else:
            ind = base + pending
            is_hdr = bool(re.match(r'^(else\s+if|if|while|for|else)\b', s)) and not s.endswith(';')
            if is_hdr:
                hdr_flag = True
            else:
                pending = 0
        if not (paren > 0 or cont or CONT_START.match(s)):
            stmt_ind = ind
        paren += s.count('(') - s.count(')')
        cont = bool(CONT_END.search(s))
        if paren <= 0:
            paren = 0
            if hdr_flag:
                pending += 1
                hdr_flag = False
        it['ind'] = ind
        it['text'] = '\t' * ind + restore(s, it['lits']) + \
            ((' ' + restore(it['tail'], it['lits'])) if it['tail'] else '')
    # align declaration groups
    j = 0
    while j < len(items):
        if items[j]['k'] != 'decl':
            j += 1
            continue
        grp = [items[j]]
        while j + 1 < len(items) and items[j + 1]['k'] == 'decl' \
                and items[j + 1]['ind'] == grp[0]['ind']:
            j += 1
            grp.append(items[j])
        j += 1
        col = align_col([g['ind'] * TAB + len(g['pre']) for g in grp])
        for g in grp:
            cur = g['ind'] * TAB + len(g['pre'])
            g['text'] = '\t' * g['ind'] + g['pre'] + tabs_to(col, cur) + \
                restore(decl_text(g['ds']), g['lits']) + \
                ((' ' + restore(g['tail'], g['lits'])) if g['tail'] else '')
    return [it['text'] for it in items]


def fix_body(body):
    return render(apply_ops(build_items(body)))


# --------------------------------------------------------------------------
# File level
# --------------------------------------------------------------------------
def norm_ws(src):
    lines = src.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out, cmt = [], False
    for ln in lines:
        was = cmt
        _, _, cmt = protect(ln, cmt)
        ln = ln.rstrip()
        if not was:
            lead = re.match(r'^[ \t]*', ln).group(0)
            if lead:
                w = len(lead.expandtabs(TAB))
                ln = '\t' * (w // TAB) + ln[len(lead):]
        out.append(ln)
    res = []
    for ln in out:                           # no consecutive empty lines
        if ln == '' and res and res[-1] == '':
            continue
        res.append(ln)
    while res and res[0] == '':
        res.pop(0)
    while res and res[-1] == '':
        res.pop()
    return res


def depth_map(lines):
    depths, d, cmt = [], 0, False
    for ln in lines:
        m, _, cmt = protect(ln, cmt)
        depths.append(d)
        d += m.count('{') - m.count('}')
    return depths


def kr_split(lines):
    """`int f(void) {`  ->  two lines (function definitions only)."""
    depths, res, cmt = depth_map(lines), [], False
    for ln, d in zip(lines, depths):
        m, _, cmt2 = protect(ln, cmt)
        ms = m.strip()
        if d == 0 and not cmt and re.match(r'^(?!typedef|struct|union|enum)[A-Za-z_][^=]*\)\s*\{$', ms):
            res.append(ln.rstrip()[:-1].rstrip())
            res.append('{')
        else:
            res.append(ln)
        cmt = cmt2
    return res


def scan_units(lines):
    units, i, n, cmt = [], 0, len(lines), False
    while i < n:
        m, lits, cmt_after = protect(lines[i], cmt)
        ms = m.strip()
        if cmt or is_comment_only(m):
            units.append({'k': 'cmt', 's': i, 'e': i + 1})
            cmt = cmt_after
            i += 1
            continue
        cmt = cmt_after
        if not ms:
            units.append({'k': 'blank', 's': i, 'e': i + 1})
            i += 1
            continue
        if ms.startswith('#'):
            j = i
            while lines[j].rstrip().endswith('\\') and j + 1 < n:
                j += 1
            units.append({'k': 'pp', 's': i, 'e': j + 1})
            i = j + 1
            continue
        brace_next = i + 1 < n and lines[i + 1].strip() == '{'
        inline = ms.endswith('{')
        head = re.match(r'^(typedef\s+)?(struct|union|enum)\b', ms)
        kind = None
        if head and (brace_next or inline):
            kind = 'struct'
        elif brace_next and '(' in ms and ms.endswith(')'):
            kind = 'func'
        if kind:
            b = i if inline and not brace_next else i + 1
            depth, c2, end = 0, False, None
            for k in range(b, n):
                m2, _, c2 = protect(lines[k], c2)
                depth += m2.count('{') - m2.count('}')
                if depth == 0:
                    end = k
                    break
            if end is not None:
                units.append({'k': kind, 's': i, 'b': b, 'e': end + 1})
                i = end + 1
                continue
        units.append({'k': 'line', 's': i, 'e': i + 1})
        i += 1
    return units


def fix_sig_line(raw):
    m, lits, _ = protect(raw)
    ms, tail = peel_comment(m.strip(), lits)
    sg = parse_sig(ms)
    if not sg:
        return raw.rstrip()
    txt = '%s\t%s%s(%s)' % (sg['pre'], '*' * sg['stars'], sg['name'],
                            fmt_params(sg['params']))
    return restore(txt + (' ' + tail if tail else ''), lits)


def fix_pp(raw, depth):
    mm = re.match(r'^\s*#\s*(\w+)(.*)$', raw)
    if not mm:
        return raw.rstrip(), depth
    name, rest = mm.group(1), mm.group(2).strip()
    if name in ('if', 'ifdef', 'ifndef'):
        ind, depth = depth, depth + 1
    elif name in ('else', 'elif'):
        ind = max(depth - 1, 0)
    elif name == 'endif':
        depth = max(depth - 1, 0)
        ind = depth
    else:
        ind = depth
    return '#' + ' ' * ind + name + (' ' + rest if rest else ''), depth


def fix_struct(lines, u):
    """Align struct/union members and the typedef name."""
    s, b, e = u['s'], u['b'], u['e']
    head = [ln.rstrip() for ln in lines[s:b + 1]]
    body, close = lines[b + 1:e - 1], lines[e - 1]
    items = []
    for raw in body:
        m, lits, _ = protect(raw)
        code, tail = peel_comment(m.strip(), lits)
        pd = parse_decl(code) if not is_comment_only(m) else None
        if pd:
            items.append({'k': 'decl', 'pre': pd[0], 'ds': pd[1], 'lits': lits,
                          'tail': tail, 'ind': 1})
        elif raw.strip():
            items.append({'k': 'raw', 'text': '\t' + raw.strip()})
    decls = [it for it in items if it['k'] == 'decl']
    col = None
    if decls:
        col = align_col([TAB + len(d['pre']) for d in decls])
        for d in decls:
            d['text'] = '\t' + d['pre'] + tabs_to(col, TAB + len(d['pre'])) + \
                restore(decl_text(d['ds']), d['lits']) + \
                ((' ' + restore(d['tail'], d['lits'])) if d['tail'] else '')
    mm = re.match(r'^\}\s*(\w+)\s*;$', close.strip())
    if mm:
        close = '}' + tabs_to(col or TAB * 2, 1) + mm.group(1) + ';'
    elif close.strip().startswith('}'):
        close = close.strip()
    return head + [it['text'] for it in items] + [close]


STYLE_C = ('/*', '*/', '*')
STYLE_SLASH = ('//', '//', '*')
STYLE_HASH = ('#', '#', '*')
DEFAULT_STYLES = {
    **{e: STYLE_C for e in ('.c', '.h', '.cc', '.hh', '.cpp', '.hpp', '.cxx', '.tpp', '.ipp', '.php', '.css')},
    **{e: STYLE_SLASH for e in ('.rs', '.js', '.ts', '.go', '.java')},       # '#' would break Rust/JS
    '.ml': ('(*', '*)', '*'), '.mli': ('(*', '*)', '*'), '.tex': ('%', '%', '*'),
    '.html': ('<!--', '-->', '*'), '.xml': ('<!--', '-->', '*'),
}
ASCII_ART = ["        :::      ::::::::",
             "      :+:      :+:    :+:",
             "    +:+ +:+         +:+  ",
             "  +#+  +:+       +#+     ",
             "+#+#+#+#+#+   +#+        ",
             "     #+#    #+#          ",
             "    ###   ########.fr    "]


def header_style(fname, opt=None):
    ext = os.path.splitext(fname)[1].lower()
    over = getattr(opt, 'header_style_map', None) or {}
    return over.get(ext) or DEFAULT_STYLES.get(ext, STYLE_HASH)


def _hline(left, right, style=STYLE_C):
    start, end, _ = style
    left = left[:70 - len(right)]
    return start + ' ' * (5 - len(start)) + left + ' ' * (70 - len(left) - len(right)) + right \
        + ' ' * (5 - len(end)) + end


def _stars(style):
    start, end, fill = style
    return start + ' ' + fill * (80 - len(start) - len(end) - 2) + ' ' + end


def make_header(fname, login, mail, created, updated, created_by=None, updated_by=None, style=STYLE_C):
    created_by, updated_by = created_by or login, updated_by or login
    h = lambda l, r: _hline(l, r, style)
    stars = _stars(style)
    return [stars, h('', ''), h('', ASCII_ART[0]),
            h(os.path.basename(fname), ASCII_ART[1]), h('', ASCII_ART[2]),
            h('By: %s <%s>' % (login, mail), ASCII_ART[3]), h('', ASCII_ART[4]),
            h('Created: %s by %s' % (created, created_by), ASCII_ART[5]),
            h('Updated: %s by %s' % (updated, updated_by), ASCII_ART[6]),
            h('', ''), stars]


def has_header(lines, style=STYLE_C):
    st = _stars(style)
    return len(lines) >= 11 and lines[0] == st and lines[10] == st


def refresh_header(h, fname, opt, changed, style=STYLE_C):
    """Existing header: keep By/Created, bump Updated (launch time) if the file changed."""
    base = os.path.basename(fname)
    by = re.search(r'By: (.+?) <(.+?)>', h[5])
    cr = re.search(r'Created: (.+?) by (\S+)', h[7])
    up = re.search(r'Updated: (.+?) by (\S+)', h[8])
    name_ok = h[3][5:50].strip() == base
    if not (by and cr and up) or (not changed and name_ok):
        return h
    return make_header(base, by.group(1), by.group(2), cr.group(1),
                       opt.now if changed else up.group(1), cr.group(2),
                       (opt.login or up.group(2)) if changed else up.group(2), style)


INC_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')


def apply_includes(lines, fname, opt):
    """--rename-include / --remove-include / --add-include on one file."""
    base = os.path.basename(fname)
    lines = list(lines)
    for old, new in opt.rename_include:
        for i, ln in enumerate(lines):
            mm = INC_RE.match(ln)
            if mm and mm.group(2) == old:
                lines[i] = ln.replace(old, new, 1)
    for name in opt.remove_include:
        lines = [ln for ln in lines if not (INC_RE.match(ln) and INC_RE.match(ln).group(2) == name)]
    if fname.endswith('.h') and not opt.include_headers:
        return lines
    for spec in opt.add_include:
        ang = spec.startswith('<')
        name = spec.strip('<>"')
        if name == base or any(INC_RE.match(ln) and INC_RE.match(ln).group(2) == name for ln in lines):
            continue
        text = '#include %s%s%s' % ('<' if ang else '"', name, '>' if ang else '"')
        last = None
        for i, ln in enumerate(lines):          # top-of-file preprocessor region
            if ln.strip() and not ln.lstrip().startswith('#'):
                break
            if INC_RE.match(ln):
                last = i
        if last is None and fname.endswith('.h'):
            last = next((i for i, ln in enumerate(lines)
                         if re.match(r'^#\s*define\s+\w+$', ln)), None)
        if last is not None:
            lines.insert(last + 1, text)
        else:
            lines[0:0] = [text, '']
    return lines


def fix_source(src, fname, opt):
    orig_lines = src.replace('\r\n', '\n').split('\n')
    lines = kr_split(norm_ws(src))
    header = []
    if has_header(lines):
        header, lines = lines[:11], lines[11:]
        while lines and lines[0] == '':
            lines.pop(0)
    broken_header = not header and bool(lines) and lines[0].startswith('/* ***')
    lines = apply_includes(lines, fname, opt)
    units = scan_units(lines)
    out, pp = [], 0
    for idx, u in enumerate(units):
        k, s, e = u['k'], u['s'], u['e']
        if idx and units[idx - 1]['k'] == 'pp' and k in ('line', 'struct') and out and out[-1] != '':
            out.append('')                      # newline after preprocessor block
        if k == 'func':
            if out and isinstance(out[-1], (str, dict)) and out[-1] != '' \
                    and units[idx - 1]['k'] not in ('cmt', 'blank'):
                out.append('')
            out.append(fix_sig_line(lines[s]) if u['b'] == s + 1 else lines[s].rstrip())
            out.append('{')
            out.extend(fix_body(lines[u['b'] + 1:e - 1]))
            out.append('}' if lines[e - 1].strip() == '}' else lines[e - 1].rstrip())
            if idx + 1 < len(units) and units[idx + 1]['k'] != 'blank':
                out.append('')
        elif k == 'struct':
            out.extend(fix_struct(lines, u))
        elif k == 'pp':
            for ln in lines[s:e]:
                txt, pp = fix_pp(ln, pp)
                out.append(txt)
        elif k == 'line':
            m, lits, _ = protect(lines[s])
            code, tail = peel_comment(m.strip(), lits)
            code = code.strip()
            sg = parse_sig(code[:-1]) if code.endswith(');') and '=' not in code else None
            pd = None if sg else parse_decl(code)
            if sg:
                rest = '%s%s(%s);' % ('*' * sg['stars'], sg['name'], fmt_params(sg['params']))
                out.append({'pre': sg['pre'], 'rest': restore(rest, lits), 'tail': restore(tail, lits)})
            elif pd:
                out.append({'pre': pd[0], 'rest': restore(decl_text(pd[1]), lits),
                            'tail': restore(tail, lits)})
            else:
                out.append(lines[s].rstrip())
        else:
            out.append(lines[s].rstrip())
    # align prototypes / global declarations (one column per run)
    idxs = [i for i, x in enumerate(out) if isinstance(x, dict)]
    runs, cur = [], []
    for i in idxs:
        if cur and not all(out[j] == '' for j in range(cur[-1] + 1, i)):
            runs.append(cur)
            cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    for run in runs:
        col = align_col([len(out[i]['pre']) for i in run])
        for i in run:
            x = out[i]
            out[i] = x['pre'] + tabs_to(col, len(x['pre'])) + x['rest'] + \
                ((' ' + x['tail']) if x['tail'] else '')
    # header guard name
    if fname.endswith('.h'):
        guard = re.sub(r'\W', '_', os.path.basename(fname).upper())
        for i, ln in enumerate(out):
            mm = re.match(r'^#\s*ifndef\s+(\w+)$', ln) if isinstance(ln, str) else None
            if mm and i + 1 < len(out) and re.match(r'^#\s*define\s+%s$' % mm.group(1), out[i + 1]):
                out[i] = ln.replace(mm.group(1), guard)
                out[i + 1] = out[i + 1].replace(mm.group(1), guard)
                break
    while out and out[-1] == '':
        out.pop()
    # 42 header
    if header:
        orig_body = '\n'.join(l.rstrip() for l in orig_lines[11:]).strip('\n')
        header = refresh_header(header, fname, opt, orig_body != '\n'.join(out).strip('\n'))
    elif not opt.no_header and opt.login and not broken_header:
        header = make_header(os.path.basename(fname), opt.login, opt.mail, opt.now, opt.now)
    res = header + ([''] if header else []) + out
    return '\n'.join(res) + '\n'


# --------------------------------------------------------------------------
# Lint (runs on the fixed text, so line numbers match the file on disk)
# --------------------------------------------------------------------------
def width(line):
    return len(line.expandtabs(TAB))


def check_func_name(sg, opt, line, add):
    """--func-prefix / --func-pattern: naming format required by the subject (e.g. ft_)."""
    prefix, pattern = getattr(opt, 'func_prefix', None), getattr(opt, 'func_pattern', None)
    if not (prefix or pattern) or not sg:
        return
    name = sg['name']
    if name in flat(opt.func_exempt) or ('static' in sg['pre'].split() and opt.skip_static):
        return
    if pattern and not re.fullmatch(pattern, name):
        add(line, 1, 'FUNC_NAME_FORMAT', "Function name '%s' does not match %s" % (name, pattern))
    elif prefix and not name.startswith(prefix):
        add(line, 1, 'FUNC_NAME_FORMAT', "Function name '%s' must start with '%s'" % (name, prefix))


def lint(text, fname, opt=None):
    errs = []

    def add(line, col, code, msg):
        errs.append((line, col, code, msg))

    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    if not has_header(lines):
        add(1, 1, 'INVALID_HEADER', 'Missing or invalid 42 header')
    for i, ln in enumerate(lines):
        if width(ln) > MAX_COLS:
            add(i + 1, MAX_COLS + 1, 'LINE_TOO_LONG', 'Line too long (%d > %d)' % (width(ln), MAX_COLS))
    units = scan_units(lines)
    nfuncs = 0
    is_h = fname.endswith('.h')
    if is_h and not any(re.match(r'^#\s*ifndef\b', l) for l in lines):
        add(1, 1, 'HEADER_PROT_ALL', 'Missing header protection (#ifndef / # define / #endif)')
    cmt = False
    for i, ln in enumerate(lines):
        m, _, cmt = protect(ln, cmt)
        ms = m.strip()
        if ms.startswith('#'):
            if re.match(r'^#\s*include\s+"[^"]*\.c"', ln):
                add(i + 1, 1, 'INCLUDE_C', 'You cannot include a C file')
            mm = re.match(r'^#\s*define\s+([A-Za-z_]\w*)', ms)
            if mm and mm.group(1) != mm.group(1).upper():
                add(i + 1, 1, 'MACRO_NAME_CAPITAL', 'Macro name must be uppercase')
            if ln.rstrip().endswith('\\'):
                add(i + 1, 1, 'MULTLINE_MACRO', 'Multiline macros are forbidden')
    for u in units:
        k, s, e = u['k'], u['s'], u['e']
        if k == 'struct':
            if fname.endswith('.c'):
                add(s + 1, 1, 'STRUCT_IN_C', 'You cannot declare a structure in a .c file')
            hm = re.search(r'\b(struct|union|enum)\s+(\w+)', lines[s])
            pref = {'struct': 's_', 'union': 'u_', 'enum': 'e_'}
            if hm and not hm.group(2).startswith(pref[hm.group(1)]):
                add(s + 1, 1, 'USER_DEFINED_TYPEDEF', '%s name must start with %s' % (hm.group(1), pref[hm.group(1)]))
            cm = re.match(r'^\}\s*(\w+)\s*;$', lines[e - 1].strip())
            if cm and lines[s].lstrip().startswith('typedef') and not cm.group(1).startswith('t_'):
                add(e, 1, 'USER_DEFINED_TYPEDEF', 'typedef name must start with t_')
        elif k == 'line':
            m, lits, _ = protect(lines[s])
            code, _ = peel_comment(m.strip(), lits)
            if opt is not None and code.endswith(');') and '=' not in code:
                check_func_name(parse_sig(code[:-1]), opt, s + 1, add)
            tm = re.match(r'^typedef\b.*\b(\w+);$', code)
            if tm and not tm.group(1).startswith('t_'):
                add(s + 1, 1, 'USER_DEFINED_TYPEDEF', 'typedef name must start with t_')
            pd = parse_decl(code)
            if pd and 'static' not in pd[0].split() and 'const' not in pd[0].split() \
                    and 'extern' not in pd[0].split() and not is_h:
                add(s + 1, 1, 'GLOBAL_VAR_DETECTED', 'Global variable present in file (forbidden unless the project allows it)')
        elif k == 'func':
            nfuncs += 1
            lint_func(lines, u, add, opt)
    if fname.endswith('.c') and nfuncs > MAX_FUNCS:
        add(1, 1, 'TOO_MANY_FUNCS', 'More than %d functions in the file (%d)' % (MAX_FUNCS, nfuncs))
    errs.sort()
    return errs


def lint_func(lines, u, add, opt=None):
    s, b, e = u['s'], u['b'], u['e']
    body = lines[b + 1:e - 1]
    sg = parse_sig(protect(lines[s])[0].strip())
    if opt is not None:
        check_func_name(sg, opt, s + 1, add)
    if sg:
        params = sg['params'].strip()
        nparams = 0 if params in ('', 'void') else len(split_top(params, ','))
        if nparams > MAX_PARAMS:
            add(s + 1, 1, 'TOO_MANY_ARGS', 'Function has more than %d parameters' % MAX_PARAMS)
        if sg['name'] != sg['name'].lower():
            add(s + 1, 1, 'FORBIDDEN_CHAR_NAME', 'Identifiers must be snake_case (lowercase)')
    nlines, nvars = len(body), 0
    if nlines > MAX_FUNC_LINES:
        add(b + 1, 1, 'TOO_MANY_LINES', 'Function has more than %d lines (%d)' % (MAX_FUNC_LINES, nlines))
    seen_stmt = sep_seen = False
    prev = None
    cmt, depth = False, 0

    def note(extra):
        return ' (not auto-fixed: it would make the function exceed %d lines)' % MAX_FUNC_LINES \
            if nlines + extra > MAX_FUNC_LINES else ''

    for k, raw in enumerate(body):
        ln = b + 2 + k
        was = cmt
        m, lits, cmt = protect(raw, cmt)
        if was or any(lits[i].startswith(('/*', '//')) for i in range(len(lits))):
            if not (was and not raw.strip()):
                add(ln, 1, 'WRONG_SCOPE_COMMENT', 'Comment is invalid in this scope')
        if is_comment_only(m) or was:
            continue
        code, _ = peel_comment(m.strip(), lits)
        if not code:
            if prev == 'decl' and not sep_seen:
                sep_seen, prev = True, 'blank'
            else:
                add(ln, 1, 'EMPTY_LINE_FUNCTION', 'Empty line in function')
            continue
        for mm in re.finditer(r'\b(for|switch|case|goto|do)\b', code):
            add(ln, mm.start() + 1, 'FORBIDDEN_CS', 'Forbidden control structure (%s)' % mm.group(1))
        if '?' in code:
            add(ln, code.index('?') + 1, 'TERNARY_FBIDDEN', 'Ternaries are forbidden')
        pd = parse_decl(code) if depth == 0 else None
        if pd:
            pre, ds = pd
            if seen_stmt:
                add(ln, 1, 'VAR_DECL_START_FUNC', 'Variable declarations must be at the beginning of the function'
                    + note(1))
            nvars += len(ds)
            for d in ds:
                if d['name'] != d['name'].lower():
                    add(ln, 1, 'FORBIDDEN_CHAR_NAME', 'Identifiers must be snake_case (lowercase)')
                if re.search(r'\[\s*[a-z_]\w*', d['arr']):
                    add(ln, 1, 'VLA_FBIDDEN', 'Variable Length Arrays are forbidden')
                if d['init'] is not None and not keeps_init(pre, d):
                    add(ln, 1, 'DECL_ASSIGN_LINE', 'Declaration and assignation on a single line' + note(1))
            if len(ds) > 1:
                add(ln, 1, 'MULT_DECL_LINE', 'Multiple declarations on a single line' + note(len(ds) - 1))
            prev = 'decl'
        else:
            if prev == 'decl' and not sep_seen:
                add(ln, 1, 'NL_AFTER_VAR_DECL', 'Missing empty line after variable declarations' + note(1))
            n = len(split_line(fix_spacing(code)))
            if n > 1:
                if code.rstrip().endswith('{') or code.startswith('}'):
                    add(ln, 1, 'BRACE_SHOULD_EOL', 'Braces must be alone on their line' + note(n - 1))
                else:
                    add(ln, 1, 'EXP_NEWLINE', 'Expected newline after control structure' + note(n - 1))
            seen_stmt, prev = True, 'stmt'
        depth += code.count('{') - code.count('}')
    if nvars > MAX_VARS:
        add(s + 1, 1, 'TOO_MANY_VARS_FUNC', 'More than %d variables in function (%d)' % (MAX_VARS, nvars))


# --------------------------------------------------------------------------
# Makefile check (generic: works for any 42 project, not only libft)
# --------------------------------------------------------------------------
def read_logical(path):
    out, buf, start = [], '', 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for i, raw in enumerate(f.read().split('\n'), 1):
            if not buf:
                start = i
            s = raw if raw.startswith('\t') and not buf else raw
            s = s.split('#', 1)[0] if not raw.startswith('\t') else s
            if s.rstrip().endswith('\\'):
                buf += s.rstrip()[:-1] + ' '
                continue
            out.append((start, buf + s))
            buf = ''
    return out


def expand(val, vs, depth=0):
    if depth > 6:
        return val
    return re.sub(r'\$[({](\w+)[)}]', lambda g: expand(vs.get(g.group(1), g.group(0)), vs, depth + 1), val)


def sh(cmd, cwd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors='replace', timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, 'timeout after %ds' % timeout
    except FileNotFoundError:
        return 127, '%s not found' % cmd[0]


def snapshot(root):
    snap = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            try:
                snap[p] = os.stat(p).st_mtime_ns
            except OSError:
                pass
    return snap


def check_makefile(mk, run=True):
    """Return [(line, code, msg)]."""
    errs = []
    proj = os.path.dirname(os.path.abspath(mk))
    logical = read_logical(mk)
    text = '\n'.join(l for _, l in logical)
    vs, targets, first_target = {}, {}, None
    for ln, l in logical:
        if l.startswith('\t') or not l.strip():
            continue
        mv = re.match(r'^\s*([A-Za-z_]\w*)\s*([:+?]?)=\s*(.*)$', l)
        if mv:
            vs[mv.group(1)] = (vs.get(mv.group(1), '') + ' ' if mv.group(2) == '+' else '') + mv.group(3).strip()
            continue
        mt = re.match(r'^([^\s:=#][^:=]*?)\s*::?(?!=)\s*(.*)$', l)
        if mt:
            for t in mt.group(1).split():
                targets.setdefault(t, ln)
                if first_target is None and not t.startswith('.') and '%' not in t:
                    first_target = t

    def has(t):
        return t in targets or expand(t, vs) in targets or \
            any(expand(k, vs) == expand(t, vs) for k in targets)

    name = expand(vs['NAME'], vs).strip() if 'NAME' in vs else None
    if 'NAME' not in vs:
        errs.append((1, 'MAKEFILE_NAME', 'No NAME variable defined'))
    elif not has('$(NAME)'):
        errs.append((1, 'MAKEFILE_RULE', 'Missing rule: $(NAME)'))
    for r in ('all', 'clean', 'fclean', 're'):
        if r not in targets:
            errs.append((1, 'MAKEFILE_RULE', 'Missing mandatory rule: %s' % r))
    goal = vs.get('.DEFAULT_GOAL', first_target)
    if goal and goal != 'all' and 'all' in targets:
        errs.append((targets.get(goal, 1), 'MAKEFILE_DEFAULT', 'Default rule is "%s", it must be "all"' % goal))
    for v in vs:                                       # multibinary: NAME_1, NAME2, ...
        if re.match(r'^NAME_?\w+$', v) and not has('$(%s)' % v):
            errs.append((1, 'MAKEFILE_RULE', 'Missing rule for binary $(%s)' % v))
    for ln, l in logical:
        if re.search(r'\$\(wildcard|(?<![\w/])\*\.[co]\b|\bfind\s', l):
            errs.append((ln, 'MAKEFILE_WILDCARD', 'Wildcards / "*.c" / "*.o" are forbidden: name every source file'))
    for v in ('CFLAGS', 'CXXFLAGS'):
        if v in vs:
            miss = [f for f in ('-Wall', '-Wextra', '-Werror') if f not in vs[v]]
            if miss:
                errs.append((1, 'MAKEFILE_FLAGS', 'CFLAGS is missing %s' % ' '.join(miss)))
    if any(f.endswith(('.cpp', '.cc', '.cxx')) for _, _, fs in os.walk(proj) for f in fs) \
            and not re.search(r'-std=(c\+\+98|gnu\+\+98)', text):
        errs.append((1, 'MAKEFILE_STD', 'C++ sources found but no -std=c++98 in the Makefile (required by the CPP modules / webserv)'))
    if '.PHONY' not in targets:
        errs.append((1, 'MAKEFILE_PHONY', 'No .PHONY declaration (all clean fclean re)'))
    # source files
    listed = set()
    for v, val in vs.items():
        if re.match(r'(?i)^(srcs?|sources?|files?|c_files|.*_srcs?|.*_files?)$', v):
            for tok in expand(val, vs).split():
                if tok.endswith('.c'):
                    listed.add(os.path.basename(tok))
                    if '$' not in tok and not os.path.exists(os.path.join(proj, tok)) \
                            and not any(os.path.exists(os.path.join(proj, d, tok)) for d in
                                        (vs.get('SRC_DIR', '').strip(), 'src', 'srcs', '')):
                        errs.append((1, 'MAKEFILE_MISSING_SRC', 'Source file not found: %s' % tok))
    libdirs = []
    for d in sorted(os.listdir(proj)):
        full = os.path.join(proj, d)
        if os.path.isdir(full) and not d.startswith('.') and os.path.exists(os.path.join(full, 'Makefile')):
            libdirs.append(d)
            if d in text and not re.search(r'-C\s*\S*|--directory', text):
                errs.append((1, 'MAKEFILE_LIB', 'Library "%s" has its own Makefile but yours never calls make -C' % d))
    for d, dirs, names in os.walk(proj):
        dirs[:] = [x for x in dirs if not x.startswith('.') and os.path.join(d, x) not in
                   [os.path.join(proj, l) for l in libdirs]]
        for n in names:
            if n.endswith('.c') and n not in listed and listed:
                errs.append((1, 'MAKEFILE_UNLISTED_SRC', '%s exists but is not listed in the Makefile sources (ignore if it is a test file)'
                             % os.path.relpath(os.path.join(d, n), proj)))
    # dynamic checks, in a throw-away copy of the project
    if run and not errs_fatal(errs) and shutil.which('make'):
        tmp = tempfile.mkdtemp(prefix='normfix_mk_')
        try:
            work = os.path.join(tmp, 'p')
            shutil.copytree(proj, work, ignore=shutil.ignore_patterns('.git', '*.o', '*.a'))
            mkname = os.path.basename(mk)
            base = ['make', '-f', mkname]
            rc, out = sh(base, work)
            if rc != 0:
                errs.append((1, 'MAKEFILE_BUILD', '`make` failed: ' + ' | '.join(out.strip().split('\n')[-3:])))
            else:
                before = snapshot(work)
                rc2, out2 = sh(base, work)
                changed = [os.path.relpath(p, work) for p, t in snapshot(work).items() if before.get(p) != t]
                if rc2 != 0 or changed:
                    errs.append((1, 'MAKEFILE_RELINK', 'Second `make` relinks / rebuilds (%s): project would be non-functional'
                                 % (', '.join(changed[:3]) or 'make failed')))
                rc, out = sh(base + ['clean'], work)
                left = [f for _, _, fs in os.walk(work) for f in fs if f.endswith('.o')]
                if rc != 0 or left:
                    errs.append((1, 'MAKEFILE_CLEAN', '`make clean` failed or left .o files'))
                rc, out = sh(base + ['fclean'], work)
                if rc != 0 or (name and os.path.exists(os.path.join(work, name))):
                    errs.append((1, 'MAKEFILE_FCLEAN', '`make fclean` failed or did not remove %s' % (name or 'the target')))
                rc, out = sh(base + ['re'], work)
                if rc != 0:
                    errs.append((1, 'MAKEFILE_RE', '`make re` failed: ' + ' | '.join(out.strip().split('\n')[-2:])))
                elif name and not os.path.exists(os.path.join(work, name)):
                    errs.append((1, 'MAKEFILE_RE', '`make re` did not produce %s' % name))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return errs


def errs_fatal(errs):
    return any(c in ('MAKEFILE_NAME', 'MAKEFILE_MISSING_SRC') for _, c, _ in errs)


# --------------------------------------------------------------------------
# Compile / runtime checks
# --------------------------------------------------------------------------
DIAG = re.compile(r'^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+): (?P<kind>fatal error|error|warning): (?P<msg>.*)$')


def inc_dirs(proj, extra):
    dirs = [proj] + list(extra)
    for d, subs, names in os.walk(proj):
        subs[:] = [s for s in subs if not s.startswith('.')]
        if d != proj and any(n.endswith('.h') for n in names) and d.count(os.sep) - proj.count(os.sep) <= 2:
            dirs.append(d)
    seen, res = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


def compile_check(path, opt, incs, lang='c', orig=None):
    """-> [(line, col, code, msg)]. cc -Wall -Wextra -Werror (C) / c++ ... -std=c++98 (C++)."""
    import shlex
    cxx = lang == 'cpp'
    extra = [os.path.dirname(os.path.abspath(orig or path))]
    cmd = [opt.cxx if cxx else opt.cc] + shlex.split(opt.cxxflags if cxx else opt.cflags) + ['-O1'] \
        + ['-I' + d for d in incs + extra] + ['-c', path, '-o', os.devnull]
    rc, out = sh(cmd, os.getcwd(), 60)
    res = []
    if rc == 127:
        return [(1, 1, 'COMPILE_ERROR', out)]
    for l in out.split('\n'):
        d = DIAG.match(l)
        if d:
            same = os.path.abspath(d['file']) == os.path.abspath(path)
            msg = d['msg'] if same else '%s (in %s:%s)' % (d['msg'], d['file'], d['line'])
            res.append((int(d['line']) if same else 1, int(d['col']) if same else 1,
                        'COMPILE_ERROR' if d['kind'] != 'warning' else 'COMPILE_WARNING', msg))
    if rc != 0 and not res:
        res.append((1, 1, 'COMPILE_ERROR', ' | '.join(out.strip().split('\n')[-2:])))
    return res


# --------------------------------------------------------------------------
# Other languages: C++, Python, Rust, shell
# --------------------------------------------------------------------------
LANG_BY_EXT = {'.c': 'c', '.h': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
               '.hh': 'cpp', '.tpp': 'cpp', '.ipp': 'cpp', '.py': 'python', '.rs': 'rust',
               '.sh': 'shell', '.bash': 'shell'}
COMPILE_EXTS = {'c': ('.c',), 'cpp': ('.cpp', '.cc', '.cxx')}
SKIP_DIRS = {'node_modules', 'target', '__pycache__', 'venv', 'env', 'build', 'dist'}
NOTICES = []


def hygiene(text):
    lines = [l.rstrip() for l in text.replace('\r\n', '\n').split('\n')]
    while lines and lines[-1] == '':
        lines.pop()
    return '\n'.join(lines) + '\n' if lines else ''


def run_filter(cmd, text, ok=(0,)):
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in ok and r.stdout.strip() else None


def find_cargo(path):
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, 'Cargo.toml')):
            return d
        if os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def cargo_edition(path):
    c = find_cargo(path)
    if c:
        mm = re.search(r'edition\s*=\s*"(\d+)"', open(os.path.join(c, 'Cargo.toml'), errors='replace').read())
        if mm:
            return mm.group(1)
    return '2021'


def fmt_python(body, fname, opt):
    """flake8-oriented fixes (line length 79). ruff or autopep8 if installed, else whitespace only."""
    try:
        compile(body, fname, 'exec')
    except (SyntaxError, ValueError):
        return hygiene(body)                       # never run tools on broken code
    name = os.path.basename(fname)
    if shutil.which('autopep8'):                     # pycodestyle fixes: E2xx, E3xx, W2xx, ...
        body = run_filter(['autopep8', '--max-line-length', '79', '-'], body) or body
    else:
        NOTICES.append('python: install autopep8 for automatic whitespace/E-code fixes (only trailing whitespace was fixed)')
    if shutil.which('ruff'):                         # pyflakes fixes: unused imports, ...
        body = run_filter(['ruff', 'check', '--fix', '--select', 'F', '--line-length', '79',
                           '--stdin-filename', name, '-'], body, ok=(0, 1)) or body
    if opt.py_format:
        out = None
        if shutil.which('ruff'):
            out = run_filter(['ruff', 'format', '--line-length', '79', '--stdin-filename', name, '-'], body)
        elif shutil.which('black'):
            out = run_filter(['black', '-q', '-l', '79', '-'], body)
        body = out or body
    return hygiene(body.lstrip('\n'))


def fmt_rust(body, fname, opt):
    if not shutil.which('rustfmt'):
        NOTICES.append('rust: rustfmt not found (only whitespace was fixed)')
        return hygiene(body)
    td = tempfile.mkdtemp(prefix='normfix_rs_')
    try:
        f = os.path.join(td, 'x.rs')
        with open(f, 'w') as fh:
            fh.write(body)
        rc, _ = sh(['rustfmt', '--edition', cargo_edition(fname), f], td, 60)
        if rc == 0:
            with open(f) as fh:
                return hygiene(fh.read())
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return hygiene(body)


def fix_generic(src, fname, opt, lang):
    """Header + includes + language formatter (no Norm outside C)."""
    style = header_style(fname, opt)
    lines = src.replace('\r\n', '\n').split('\n')
    header = []
    if has_header(lines, style):
        header, lines = lines[:11], lines[11:]
    broken = not header and bool(lines) and lines[0].startswith(style[0] + ' ' + style[2] * 8)
    shebang = not header and bool(lines) and lines[0].startswith('#!')
    body = '\n'.join(lines).lstrip('\n')
    if lang in ('c', 'cpp'):
        body = '\n'.join(apply_includes(body.split('\n'), fname, opt))
    fmt = {'python': fmt_python, 'rust': fmt_rust}.get(lang)
    body = fmt(body, fname, opt) if fmt else hygiene(body)
    if header:
        header = refresh_header(header, fname, opt, body.strip('\n') != '\n'.join(lines).strip('\n'), style)
    elif opt.header and opt.login and not opt.no_header and not broken and not shebang:
        header = make_header(os.path.basename(fname), opt.login, opt.mail, opt.now, opt.now, style=style)
    return ('\n'.join(header) + '\n\n' if header else '') + body


def py_code_lines(text):
    import io
    import tokenize
    res = [list(l) for l in text.split('\n')]
    skip = {tokenize.STRING, tokenize.COMMENT}
    skip |= {getattr(tokenize, n) for n in ('FSTRING_START', 'FSTRING_MIDDLE', 'FSTRING_END') if hasattr(tokenize, n)}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in skip:
                (sr, sc), (er, ec) = tok.start, tok.end
                for r in range(sr, er + 1):
                    c0 = sc if r == sr else 0
                    c1 = ec if r == er else len(res[r - 1])
                    for c in range(c0, min(c1, len(res[r - 1]))):
                        res[r - 1][c] = ' '
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text.split('\n')
    return [''.join(r) for r in res]


def code_lines(text, lang):
    if lang in ('c', 'cpp', 'rust'):
        out, cmt = [], False
        for ln in text.split('\n'):
            m, _, cmt = protect(ln, cmt)
            out.append(re.sub('[' + PHC + ']', ' ', m))
        return out
    if lang == 'python':
        return py_code_lines(text)
    return text.split('\n')


def flat(xs):
    return [w.strip() for x in xs for w in str(x).split(',') if w.strip()]


def forbid_errs(text, lang, opt):
    words = flat(opt.forbid)
    if not words:
        return []
    res = []
    pats = [(w, re.compile(r'(?<![\w.])' + re.escape(w).replace(r'\ ', r'\s+') + r'\b')) for w in words]
    for i, ln in enumerate(code_lines(text, lang)):
        for w, pat in pats:
            mm = pat.search(ln)
            if mm:
                res.append((i + 1, mm.start() + 1, 'FORBIDDEN_FUNC', 'Forbidden by the subject: %s' % w))
    return res


def tool_lines(cmd, cwd):
    rc, out = sh(cmd, cwd, 120)
    return rc, out.split('\n')


def lint_generic(new, path, lang, opt):
    errs = []
    lines = new.split('\n')
    style = header_style(path, opt)
    if opt.header and not has_header(lines, style) and not (lines and lines[0].startswith('#!')):
        errs.append((1, 1, 'INVALID_HEADER', 'Missing or invalid 42 header'))
    if lang == 'cpp' and path.endswith(('.hpp', '.hh', '.h')) and not re.search(r'^#\s*ifndef\b', new, re.M):
        errs.append((1, 1, 'HEADER_PROT_ALL', 'Missing include guard (#ifndef / #define / #endif)'))
    if lang not in ('python', 'shell'):
        return errs
    td = tempfile.mkdtemp(prefix='normfix_lint_')
    try:
        tmp = os.path.join(td, os.path.basename(path))
        with open(tmp, 'w') as f:
            f.write(new)
        cwd = os.path.dirname(os.path.abspath(path))
        if lang == 'python':
            try:
                compile(new, path, 'exec')
            except SyntaxError as e:
                return errs + [(e.lineno or 1, e.offset or 1, 'SYNTAX_ERROR', e.msg)]
            if shutil.which('flake8'):
                cmd = ['flake8', tmp]
            elif shutil.which('ruff'):
                cmd = ['ruff', 'check', '--no-fix', '--select', 'E,W,F', '--line-length', '79',
                       '--output-format', 'concise', tmp]
            else:
                cmd = None
                NOTICES.append('python: install flake8 (or ruff) to lint; only syntax was checked')
            if cmd:
                _, out = tool_lines(cmd, cwd)
                for l in out:
                    mm = re.match(r'^.+?:(\d+):(\d+):?\s+([A-Z]+\d+)\s+(?:\[\*\]\s+)?(.*)$', l)
                    if mm and not (mm[3] == 'E501' and int(mm[1]) <= 11 and has_header(new.split('\n'), style)):
                        errs.append((int(mm[1]), int(mm[2]), mm[3], mm[4]))
            if opt.mypy:
                if not sh([sys.executable, '-m', 'mypy', '--version'], cwd, 20)[0] == 0:
                    NOTICES.append('python: --mypy requested but mypy is not installed')
                else:
                    _, out = tool_lines([sys.executable, '-m', 'mypy', '--ignore-missing-imports', '--disallow-untyped-defs',
                                         '--warn-return-any', '--warn-unused-ignores', '--check-untyped-defs',
                                         '--show-column-numbers', '--no-error-summary', '--no-color-output', tmp], cwd)
                    for l in out:
                        mm = re.match(r'^.+?:(\d+):(\d+): (error|warning): (.*)$', l)
                        if mm:
                            errs.append((int(mm[1]), int(mm[2]), 'MYPY', mm[4]))
        else:
            rc, out = tool_lines(['bash', '-n', tmp], cwd)
            for l in out:
                mm = re.search(r'line (\d+): (.*)$', l)
                if mm:
                    errs.append((int(mm[1]), 1, 'SYNTAX_ERROR', mm[2]))
            if shutil.which('shellcheck'):
                _, out = tool_lines(['shellcheck', '-f', 'gcc', tmp], cwd)
                for l in out:
                    d = re.match(r'^.+?:(\d+):(\d+): (\w+): (.*)$', l)
                    if d:
                        errs.append((int(d[1]), int(d[2]), 'SHELLCHECK', d[4]))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return errs


def rust_check(cdir, opt):
    """cargo clippy (or check) for a whole crate -> [(abs_path|None, line, col, code, msg)]."""
    if not shutil.which('cargo'):
        NOTICES.append('rust: cargo not found, compile check skipped')
        return []
    clippy = sh(['cargo', 'clippy', '--version'], cdir, 30)[0] == 0
    cmd = ['cargo', 'clippy', '--all-targets', '--message-format', 'short', '--', '-D', 'warnings'] if clippy \
        else ['cargo', 'check', '--all-targets', '--message-format', 'short']
    rc, out = sh(cmd, cdir, 900)
    res = []
    for l in out.split('\n'):
        d = DIAG.match(l)
        if d:
            res.append((os.path.abspath(os.path.join(cdir, d['file'])), int(d['line']), int(d['col']),
                        'COMPILE_WARNING' if d['kind'] == 'warning' and clippy is False else 'COMPILE_ERROR', d['msg']))
    if rc != 0 and not res:
        res.append((None, 1, 1, 'COMPILE_ERROR', ' | '.join(out.strip().split('\n')[-3:])))
    return res


IGNORED_SYMS = re.compile(r'^(_GLOBAL_OFFSET_TABLE_|__stack_chk_fail|__gmon_start__|_ITM_.*|__cxa_finalize|__asan.*|__ubsan.*|__tsan.*)$')


def allowed_check(items, opt, incs):
    """Authorised functions: compile each .c, read undefined symbols with nm. -> {path: errs}"""
    import shlex
    if not shutil.which('nm'):
        NOTICES.append('--allowed-functions needs `nm` (binutils)')
        return {}
    allowed = set(flat(opt.allowed_functions))
    td = tempfile.mkdtemp(prefix='normfix_nm_')
    defined, und, texts = set(), {}, {}
    try:
        for i, (path, text) in enumerate(items):
            if not path.endswith('.c'):
                continue
            src = os.path.join(td, '%d_%s' % (i, os.path.basename(path)))
            with open(src, 'w') as f:
                f.write(text)
            obj = src[:-2] + '.o'
            cmd = [opt.cc] + [f for f in shlex.split(opt.cflags) if f != '-Werror'] \
                + ['-O0', '-fno-builtin', '-fno-stack-protector'] \
                + ['-I' + d for d in incs + [os.path.dirname(os.path.abspath(path))]] + ['-c', src, '-o', obj]
            if sh(cmd, os.getcwd(), 60)[0] != 0:
                continue                                  # compile errors are reported elsewhere
            texts[path] = text
            for l in sh(['nm', '-P', obj], td)[1].split('\n'):
                parts = l.split()
                if len(parts) < 2:
                    continue
                name, typ = parts[0], parts[1]
                if sys.platform == 'darwin' and name.startswith('_'):
                    name = name[1:]
                if typ == 'U':
                    und.setdefault(path, set()).add(name)
                elif typ not in ('w', 'v', 'W', 'V'):
                    defined.add(name)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    res = {}
    for path, syms in und.items():
        lines = code_lines(texts[path], 'c')
        for sym in sorted(syms - defined - allowed):
            if IGNORED_SYMS.match(sym):
                continue
            ln = next((k + 1 for k, l in enumerate(lines) if re.search(r'\b%s\b' % re.escape(sym), l)), 1)
            res.setdefault(path, []).append((ln, 1, 'FORBIDDEN_FUNC', "'%s' is not in the allowed functions list" % sym))
    return res


# --------------------------------------------------------------------------
# Runtime checks
# --------------------------------------------------------------------------
def run_native(files, opt, incs, cxx):
    """C / C++: build with sanitizers (+ optional valgrind), run, report crashes."""
    import shlex
    exts = COMPILE_EXTS['cpp' if cxx else 'c']
    srcs = [f for f in files if f.endswith(exts)]
    is_main = lambda f: re.search(r'\bint\s+main\s*\(', open(f, errors='replace').read())
    mains = [f for f in srcs if is_main(f)]
    if opt.test:
        srcs = [f for f in srcs if f not in mains] + opt.test
    elif not mains:
        return [('RUN_SKIPPED', 'No main() found: give one with --test test_main.c')]
    elif len(mains) > 1:
        return [('RUN_SKIPPED', 'Several main() found (%s): pass --test to choose' % ', '.join(mains))]
    comp = opt.cxx if cxx else opt.cc
    std = [f for f in shlex.split(opt.cxxflags if cxx else opt.cflags) if f.startswith('-std')]
    san = '-fsanitize=thread' if opt.sanitize == 'thread' else '-fsanitize=address,undefined'
    tmp = tempfile.mkdtemp(prefix='normfix_run_')
    res = []
    try:
        exe = os.path.join(tmp, 'a.out')
        cmd = [comp, '-g', '-Wall', '-Wextra', san, '-fno-omit-frame-pointer'] + std \
            + ['-I' + d for d in incs] + srcs + ['-o', exe] + shlex.split(opt.ldflags)
        rc, out = sh(cmd, os.getcwd(), 120)
        if rc != 0:
            return [('RUN_BUILD', 'Cannot build the test binary: ' + ' | '.join(out.strip().split('\n')[-3:]))]
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=1', UBSAN_OPTIONS='print_stacktrace=1')
        try:
            r = subprocess.run([exe] + shlex.split(opt.args), capture_output=True, text=True, errors='replace',
                               timeout=opt.timeout, env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return [('RUN_TIMEOUT', 'No exit after %ds (infinite loop / deadlock / waiting on stdin?)' % opt.timeout)]
        err = r.stderr
        if r.returncode < 0:
            res.append(('RUN_SIGNAL', 'Killed by %s' % signal.Signals(-r.returncode).name))
        mm = re.search(r'(?:ERROR|WARNING): (AddressSanitizer|LeakSanitizer|ThreadSanitizer): (.*)', err)
        if mm:
            frames = re.findall(r'#\d+ \S+ in (\S+) (\S+:\d+)', err[mm.end():])
            where = next(((fn, loc) for fn, loc in frames if not loc.startswith(('/usr', '/lib')) and 'sanitizer' not in loc), None)
            res.append(('RUN_' + mm.group(1).upper().replace('SANITIZER', ''),
                        mm.group(2).strip() + (' -> %s (%s)' % where if where else '')))
        for m in sorted(set(re.findall(r'^(\S+:\d+:\d+: runtime error: .*)$', err, re.M)))[:5]:
            res.append(('RUN_UB', m))
        if r.returncode > 0 and not res:
            res.append(('RUN_EXIT', 'Exit status %d' % r.returncode))
        if opt.show_output:
            print('--- program stdout ---\n%s--- stderr ---\n%s' % (r.stdout[-2000:], err[-2000:]))
        if opt.valgrind:
            if not shutil.which('valgrind'):
                NOTICES.append('--valgrind requested but valgrind is not installed')
            else:
                exe2 = os.path.join(tmp, 'vg.out')
                rc, out = sh([comp, '-g', '-O0'] + std + ['-I' + d for d in incs] + srcs + ['-o', exe2] + shlex.split(opt.ldflags),
                             os.getcwd(), 120)
                if rc == 0:
                    rc, out = sh(['valgrind', '--leak-check=full', '--show-leak-kinds=all', '--track-fds=yes',
                                  '--error-exitcode=42', exe2] + shlex.split(opt.args), tmp, opt.timeout * 20)
                    lost = re.search(r'definitely lost: ([\d,]+) bytes', out)
                    if lost and lost.group(1) != '0':
                        res.append(('RUN_VG_LEAK', 'valgrind: %s bytes definitely lost' % lost.group(1)))
                    errs_n = re.search(r'ERROR SUMMARY: (\d+) errors', out)
                    if errs_n and int(errs_n.group(1)) > 0 and not lost:
                        res.append(('RUN_VG_ERROR', 'valgrind: %s error(s) (invalid read/write, uninitialised value...)' % errs_n.group(1)))
                    fd = re.search(r'FILE DESCRIPTORS: (\d+) open \((\d+) std\)', out)
                    if fd and int(fd.group(1)) > int(fd.group(2)):
                        res.append(('RUN_VG_FD', 'valgrind: %d file descriptor(s) still open at exit' % (int(fd.group(1)) - int(fd.group(2)))))
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_python(files, opt):
    import shlex
    entries = opt.test or [f for f in files if re.search(r'__name__\s*==\s*[\'"]__main__[\'"]', open(f, errors='replace').read())]
    if not entries:
        return [('RUN_SKIPPED', 'No script with an `if __name__ == "__main__"` guard: give one with --test script.py')]
    res = []
    for e in entries:
        try:
            r = subprocess.run([sys.executable, os.path.abspath(e)] + shlex.split(opt.args), capture_output=True, text=True, errors='replace',
                               timeout=opt.timeout, stdin=subprocess.DEVNULL, cwd=os.path.dirname(os.path.abspath(e)))
        except subprocess.TimeoutExpired:
            res.append(('RUN_TIMEOUT', '%s: no exit after %ds' % (e, opt.timeout)))
            continue
        if r.returncode < 0:
            res.append(('RUN_SIGNAL', '%s: killed by %s' % (e, signal.Signals(-r.returncode).name)))
        elif 'Traceback (most recent call last)' in r.stderr:
            tail = [l for l in r.stderr.strip().split('\n') if l.strip()]
            frames = re.findall(r'File "([^"]+)", line (\d+)', r.stderr)
            res.append(('RUN_EXCEPTION', '%s: %s%s' % (e, tail[-1], ' (at %s:%s)' % frames[-1] if frames else '')))
        elif r.returncode != 0:
            res.append(('RUN_EXIT', '%s: exit status %d' % (e, r.returncode)))
        if opt.show_output:
            print('--- %s stdout ---\n%s--- stderr ---\n%s' % (e, r.stdout[-2000:], r.stderr[-2000:]))
    return res


def run_rust(cdir, opt):
    import shlex
    if not shutil.which('cargo'):
        return [('RUN_SKIPPED', 'cargo not found')]
    rc, out = sh(['cargo', 'build', '--quiet'], cdir, 900)
    if rc != 0:
        return [('RUN_BUILD', ' | '.join(out.strip().split('\n')[-3:]))]
    try:
        r = subprocess.run(['cargo', 'run', '--quiet', '--'] + shlex.split(opt.args), cwd=cdir, capture_output=True,
                           text=True, errors='replace', timeout=opt.timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [('RUN_TIMEOUT', 'No exit after %ds' % opt.timeout)]
    res = []
    pm = re.search(r"panicked at ([^\n]*?)(?::\n|\n|$)(.*)", r.stderr)
    if r.returncode < 0:
        res.append(('RUN_SIGNAL', 'Killed by %s' % signal.Signals(-r.returncode).name))
    elif pm:
        res.append(('RUN_PANIC', 'panicked at %s %s' % (pm.group(1).strip(), pm.group(2).strip().split('\n')[0])))
    elif r.returncode != 0:
        res.append(('RUN_EXIT', 'Exit status %d' % r.returncode))
    if opt.show_output:
        print('--- stdout ---\n%s--- stderr ---\n%s' % (r.stdout[-2000:], r.stderr[-2000:]))
    return res


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
                           'normfix', 'config.json')


def load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find_project_config(paths):
    start = os.path.abspath(paths[0] if paths else '.')
    d = start if os.path.isdir(start) else os.path.dirname(start)
    for _ in range(6):
        c = os.path.join(d, '.normfix.json')
        if os.path.exists(c):
            return load_json(c), c
        if os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    return {}, None


def collect(paths):
    exts = tuple(LANG_BY_EXT)
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS)
                files += [os.path.join(root, n) for n in sorted(names) if n.lower().endswith(exts)]
        elif p.lower().endswith(exts):
            files.append(p)
        else:
            print('%s: ignored (supported: %s; Makefiles are checked with --makefile)' % (p, ' '.join(sorted(exts))))
    return files


def lang_of(path, opt, ctx):
    if opt.lang != 'auto':
        return opt.lang
    ext = os.path.splitext(path)[1].lower()
    if ext == '.h' and ctx['cpp'] and not ctx['c']:
        return 'cpp'
    return LANG_BY_EXT.get(ext)


def detect_identity(files):
    for f in files:
        try:
            head = open(f, encoding='utf-8', errors='replace').read(2500)
        except OSError:
            continue
        mm = re.search(r'By: (\S+) <(\S+?)>', head)
        if mm:
            return mm.group(1), mm.group(2)
    return None, None


def find_makefile(files, paths):
    cands = []
    for p in paths:
        d = p if os.path.isdir(p) else os.path.dirname(os.path.abspath(p))
        cands += [d, os.path.dirname(os.path.abspath(d))]
    for d in cands:
        for n in ('Makefile', 'makefile', 'GNUmakefile'):
            if os.path.exists(os.path.join(d, n)):
                return os.path.join(d, n)
    return None


def summarize(src, new, path):
    style = header_style(path)
    tags = []
    old_h, new_h = has_header(src.split('\n'), style), has_header(new.split('\n'), style)
    if not old_h and new_h:
        tags.append('42 header added')
    elif old_h and new_h and src.split('\n')[:11] != new.split('\n')[:11]:
        tags.append('header updated')
    inc = lambda t: [m.group(2) for m in map(INC_RE.match, t.split('\n')) if m]
    oi, ni = inc(src), inc(new)
    add, rem = [x for x in ni if x not in oi], [x for x in oi if x not in ni]
    if add:
        tags.append('+include ' + ', '.join(add))
    if rem:
        tags.append('-include ' + ', '.join(rem))
    tags.append('fixes')
    return '; '.join(tags)


def build_parser():
    ap = argparse.ArgumentParser(
        prog='normfix',
        description='Fix and check 42 projects. C: full Norm v4.1 fixer. C++/Python/Rust/shell: header, formatter, '
                    'linters. Everywhere: Makefile audit, compile check, sanitizers. '
                    'Without paths it works on the current directory.')
    ap.add_argument('paths', nargs='*', help='files or directories (default: .)')
    ap.add_argument('-n', '--check', action='store_true', help='do not write anything, only report')
    ap.add_argument('-d', '--diff', action='store_true', help='show a diff, do not write')
    ap.add_argument('-y', '--yes', action='store_true', help='do not ask for confirmation before writing')
    ap.add_argument('--lang', default='auto', choices=['auto', 'c', 'cpp', 'python', 'rust', 'shell'],
                    help='force the language of every file (default: from the extension)')
    g = ap.add_argument_group('identity / 42 header')
    g.add_argument('--login', help='login for new headers and "Updated: ... by"')
    g.add_argument('--mail', help='mail for new headers (default: <login>@student.42.fr)')
    g.add_argument('--save', action='store_true', help='save --login/--mail/--cc/--cflags/--cxx/--cxxflags as defaults and exit (if no paths)')
    g.add_argument('--show-config', action='store_true', help='print the saved config and exit')
    g.add_argument('--header', action='store_true', help='also add the 42 header to non-C files (C/.h always get it)')
    g.add_argument('--no-header', action='store_true', help='never add a missing 42 header')
    g.add_argument('--header-style', action='append', default=[], metavar='.EXT=START,END,FILL',
                   help="comment delimiters for an extension, e.g. '.rs=//,//,*'. Repeatable")
    g = ap.add_argument_group('includes (C / C++, applied to every processed file)')
    g.add_argument('--add-include', action='append', default=[], metavar='NAME',
                   help='add #include "NAME" if missing (use <stdlib.h> for system headers). Repeatable')
    g.add_argument('--rename-include', action='append', default=[], metavar='OLD=NEW',
                   help='replace #include "OLD" by "NEW" (e.g. header.h=libft.h). Repeatable')
    g.add_argument('--remove-include', action='append', default=[], metavar='NAME', help='remove #include of NAME. Repeatable')
    g.add_argument('--include-headers', action='store_true', help='--add-include also applies to .h files')
    g = ap.add_argument_group('subject rules')
    g.add_argument('--forbid', action='append', default=[], metavar='WORDS',
                   help='comma-separated identifiers forbidden by the subject (printf, "using namespace", friend...)')
    g.add_argument('--allowed-functions', action='append', default=[], dest='allowed_functions', metavar='FUNCS',
                   help='C only: comma-separated authorised external functions (write,malloc,free); '
                        'any other external call (found with nm) is reported')
    g.add_argument('--func-prefix', metavar='PREFIX',
                   help='C: every function (definitions and prototypes) must start with PREFIX, e.g. ft_ (never auto-renamed)')
    g.add_argument('--func-pattern', metavar='REGEX', help='C: function names must fully match this regex (overrides --func-prefix)')
    g.add_argument('--func-exempt', action='append', default=['main'], metavar='NAMES',
                   help='comma-separated function names exempt from the naming rule (default: main)')
    g.add_argument('--skip-static', action='store_true', help='naming rule ignores static functions')
    g = ap.add_argument_group('Makefile')
    g.add_argument('--makefile', help='Makefile to check (default: searched next to the files / one level up)')
    g.add_argument('--no-makefile', action='store_true', help='skip the Makefile checks')
    g.add_argument('--no-make-run', action='store_true', help='static Makefile checks only (do not run make in a temp copy)')
    g = ap.add_argument_group('compilation / linting / runtime')
    g.add_argument('--cc')
    g.add_argument('--cflags', help='default: -Wall -Wextra -Werror')
    g.add_argument('--cxx')
    g.add_argument('--cxxflags', help='default: -Wall -Wextra -Werror -std=c++98')
    g.add_argument('--ldflags', default='', help='extra linker flags for --run (e.g. "-lm -lpthread")')
    g.add_argument('-I', '--include-dir', action='append', default=[], help='extra include dir')
    g.add_argument('--no-compile', action='store_true', help='skip compilation / cargo checks')
    g.add_argument('--mypy', action='store_true', help='Python: also run mypy (42 flags)')
    g.add_argument('--py-format', action='store_true', help='Python: also run ruff format / black (line length 79)')
    g.add_argument('--run', action='store_true', help='run the program: C/C++ with sanitizers, Python script, cargo run')
    g.add_argument('--sanitize', choices=['address', 'thread'], default='address',
                   help='sanitizer for --run on C/C++ (thread = philosophers / webserv races)')
    g.add_argument('--valgrind', action='store_true', help='C/C++ --run: also run valgrind (leaks, invalid access, open fds)')
    g.add_argument('--test', action='append', default=[], metavar='FILE',
                   help='entry point for --run: your test main (C/C++, replaces any main) or the script to run (Python)')
    g.add_argument('--args', default='', help='arguments passed to the program in --run')
    g.add_argument('--timeout', type=int, default=5)
    g.add_argument('--show-output', action='store_true')
    return ap


def main():
    ap = build_parser()
    pre, _ = ap.parse_known_args()
    gcfg = load_json(CONFIG_PATH)
    pcfg, pcfg_path = find_project_config(pre.paths)
    dests = {a.dest for a in ap._actions}
    defaults = {k: gcfg[k] for k in ('cc', 'cflags', 'cxx', 'cxxflags') if k in gcfg}
    defaults.update({k.replace('-', '_'): v for k, v in pcfg.items()})
    ap.set_defaults(**{k: v for k, v in defaults.items() if k in dests})
    opt = ap.parse_args()
    opt.now = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')     # launch time

    if opt.show_config:
        print('%s\n%s' % (CONFIG_PATH, json.dumps(gcfg, indent=2) if gcfg else '(no saved config)'))
        print('project config: %s' % (pcfg_path or '(none: create .normfix.json in your project)'))
        return 0
    if opt.save:
        for k in ('login', 'mail', 'cc', 'cflags', 'cxx', 'cxxflags'):
            if getattr(opt, k):
                gcfg[k] = getattr(opt, k)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(gcfg, f, indent=2)
        print('saved to %s: %s' % (CONFIG_PATH, json.dumps(gcfg)))
        if not opt.paths:
            return 0
    opt.paths = opt.paths or ['.']
    opt.cc = opt.cc or 'cc'
    opt.cxx = opt.cxx or 'c++'
    opt.cflags = opt.cflags or '-Wall -Wextra -Werror'
    opt.cxxflags = opt.cxxflags or '-Wall -Wextra -Werror -std=c++98'
    try:
        opt.rename_include = [tuple(x.split('=', 1)) for x in opt.rename_include]
        assert all(len(t) == 2 for t in opt.rename_include)
        opt.header_style_map = {}
        for spec in opt.header_style:
            ext, val = spec.split('=', 1)
            st, en, fl = val.split(',')
            opt.header_style_map[ext.lower()] = (st, en, fl)
    except (AssertionError, ValueError):
        ap.error("--rename-include expects OLD=NEW, --header-style expects '.ext=START,END,FILL'")

    files = collect(opt.paths)
    if not files:
        print('nothing to do: no supported source file found')
        return 0
    ctx = {'cpp': any(f.lower().endswith(('.cpp', '.cc', '.cxx', '.hpp', '.hh', '.tpp', '.ipp')) for f in files),
           'c': any(f.lower().endswith('.c') for f in files)}
    langs = {f: lang_of(f, opt, ctx) for f in files}
    det_login, det_mail = detect_identity(files)
    opt.login = opt.login or os.environ.get('NORMFIX_LOGIN') or gcfg.get('login') or det_login or os.environ.get('USER')
    mail = opt.mail or os.environ.get('NORMFIX_MAIL') or (gcfg.get('mail') if gcfg.get('login', opt.login) == opt.login else None)
    opt.mail = mail or (det_mail if det_login == opt.login else None) or \
        ('%s@student.42.fr' % opt.login if opt.login else None)
    mk = None if opt.no_makefile else (opt.makefile or find_makefile(files, opt.paths))
    proj = os.path.dirname(os.path.abspath(mk)) if mk else os.path.dirname(os.path.abspath(files[0]))
    incs = inc_dirs(proj, [os.path.abspath(d) for d in opt.include_dir])
    if pcfg_path:
        print('(project config: %s)' % os.path.relpath(pcfg_path))

    # phase 1: compute fixes (nothing is written yet)
    bad, data = 0, []
    for path in files:
        try:
            with open(path, encoding='utf-8') as f:
                src = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            print('%s: cannot read (%s)' % (path, exc))
            bad += 1
            continue
        lang = langs[path]
        new = fix_source(src, path, opt) if lang == 'c' else fix_generic(src, path, opt, lang)
        data.append((path, lang, src, new))
    changed = [(p, s_, n_) for p, _, s_, n_ in data if s_ != n_]

    # phase 2: confirmation, then write
    write = not (opt.check or opt.diff) and bool(changed)
    show = lambda: [sys.stdout.writelines(difflib.unified_diff(s_.splitlines(True), n_.splitlines(True), 'a/' + p, 'b/' + p))
                    for p, s_, n_ in changed]
    if opt.diff:
        show()
    if write and not opt.yes:
        print('Login/mail used for headers: %s <%s>\n' % (opt.login, opt.mail))
        print('%d file(s) would be modified:' % len(changed))
        for p, s_, n_ in changed:
            print('  %-30s [%s] %s' % (p, langs[p], summarize(s_, n_, p)))
        if not sys.stdin.isatty():
            print('\nNot a terminal: nothing written. Re-run with -y to apply (or -d to see the diff).')
            write = False
        else:
            while True:
                ans = input('\nApply? [y/N/d=show diff] ').strip().lower()
                if ans == 'd':
                    show()
                    continue
                write = ans in ('y', 'yes')
                break
    written = set()
    if write:
        for p, s_, n_ in changed:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(n_)
            written.add(p)

    # phase 3: report on the fixed text
    rust_res = {}
    if not opt.no_compile:
        for path, lang, _, _ in data:
            c = find_cargo(path) if lang == 'rust' else None
            if c and c not in rust_res:
                rust_res[c] = rust_check(c, opt)
        if any(p not in written and s_ != n_ for p, _, s_, n_ in data if langs[p] == 'rust'):
            NOTICES.append('rust: cargo checked the files on disk (fixes not written)')
    allowed = allowed_check([(p, n_) for p, l, _, n_ in data if l == 'c'], opt, incs) if flat(opt.allowed_functions) else {}
    for path, lang, src, new in data:
        if lang == 'c':
            errs = [x for x in lint(new, path, opt) if x[2] != 'GLOBAL_VAR_DETECTED']
        else:
            errs = lint_generic(new, path, lang, opt)
        errs += forbid_errs(new, lang, opt)
        errs += allowed.get(path, [])
        if lang in COMPILE_EXTS and path.endswith(COMPILE_EXTS[lang]) and not opt.no_compile:
            td = tempfile.mkdtemp(prefix='normfix_')
            tmpf = os.path.join(td, os.path.basename(path))
            with open(tmpf, 'w') as f:
                f.write(new)
            errs += compile_check(tmpf, opt, incs, lang, orig=path)
            shutil.rmtree(td, ignore_errors=True)
        if lang == 'rust':
            c = find_cargo(path)
            errs += [(l, cc, k, m) for ap_, l, cc, k, m in rust_res.get(c, []) if ap_ == os.path.abspath(path)]
        errs.sort(key=lambda x: (x[0], x[1]))
        if errs:
            bad += 1
            print('%s: Error!' % path)
            for line, col, code, msg in errs:
                print('Error: %-19s (line: %3d, col: %3d):\t%s' % (code, line, col, msg))
        else:
            print('%s: OK!' % path)
    for c, res in rust_res.items():
        general = [r for r in res if r[0] is None or not any(os.path.abspath(p) == r[0] for p in files)]
        if general:
            bad += 1
            print('%s: Error!' % os.path.join(os.path.relpath(c), 'Cargo.toml'))
            for ap_, l, cc, k, m in general:
                print('Error: %-19s (%s:%d):\t%s' % (k, os.path.relpath(ap_) if ap_ else 'cargo', l, m))
    if mk:
        errs = check_makefile(mk, run=not opt.no_make_run)
        notes = [e for e in errs if e[1] == 'MAKEFILE_UNLISTED_SRC']
        errs = [e for e in errs if e[1] != 'MAKEFILE_UNLISTED_SRC']
        if errs:
            bad += 1
            print('%s: Error!' % os.path.relpath(mk))
            for line, code, msg in sorted(errs):
                print('Error: %-19s (line: %3d, col:   1):\t%s' % (code, line, msg))
        else:
            print('%s: OK!' % os.path.relpath(mk))
        for line, code, msg in notes:
            print('Notice: %-18s :\t%s' % (code, msg))
    if opt.run:
        res = []
        nat = [p for p, l, _, _ in data if l in ('c', 'cpp')]
        if nat:
            res += run_native(nat, opt, incs, any(langs[p] == 'cpp' for p in nat))
        py = [p for p, l, _, _ in data if l == 'python']
        if py:
            res += run_python(py, opt)
        for c in {find_cargo(p) for p, l, _, _ in data if l == 'rust'} - {None}:
            res += run_rust(c, opt)
        if not res:
            print('[run]: OK! (no crash, no sanitizer report)')
        else:
            bad += 1 if any(c != 'RUN_SKIPPED' for c, _ in res) else 0
            print('[run]: Error!')
            for code, msg in res:
                print('Error: %-19s:\t%s' % (code, msg))
        if changed and not written:
            NOTICES.append('--run used the files on disk (fixes not written)')
    for n in sorted(set(NOTICES)):
        print('Notice: %s' % n)
    if written:
        print('\n%d file(s) rewritten' % len(written))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
