#!/usr/bin/env python3
"""
normfix - auto-fixer and linter for the 42 Norm (v4.1)

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
    normfix.py --login user --mail user@student.42.fr file.c
"""
import argparse
import datetime
import difflib
import os
import re
import sys

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
          'struct|enum|union|volatile|register|extern|typedef|size_t|ssize_t')
OPS = re.compile(r'(->|\+\+|--)|(<<=|>>=|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|%=|'
                 r'&=|\|=|\^=|<<|>>|=|<|>|/|%|\^|\|)')
BIN_PM = re.compile(r'([\w\)\]' + PHC + r'])\s*([+\-])(?![+\-=>])\s*'
                    r'(?=[\w\(' + PHC + r'!~*&])')
BIN_AM = re.compile(r'([\w\]' + PHC + r'])\s*([*&])(?![*&=])\s*(?=[\w(' + PHC + r'])')
CAST_RE = re.compile(r'\(\s*((?:const\s+|unsigned\s+|signed\s+|struct\s+|union\s+|enum\s+)?'
                     r'[A-Za-z_]\w*(?:\s*\*+))\s*\)\s*')
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
    m = CAST_RE.sub(lambda g: '(' + re.sub(r'\s*\*\s*', ' *', g.group(1).strip()) + ')', m)
    m = re.sub(r'\b(if|while|switch|for)\s*\(', r'\1 (', m)
    m = re.sub(r'\bsizeof\s+\(', 'sizeof(', m)
    m = re.sub(r'\b(?!(?:if|while|switch|for|return|else|do|case|sizeof|%s)\b)'
               r'(\w+)\s+\(' % TYPEKW, r'\1(', m)
    m = re.sub(r'\(\s+', '(', m)
    m = re.sub(r'\s+\)', ')', m)
    m = re.sub(r'\[\s+', '[', m)
    m = re.sub(r'\s+\]', ']', m)
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
    if re.match(r'^(else\s+if|if|while)\b', m):
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
                news = [dict(it, m=p, tail='') for p in parts]
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
            is_hdr = bool(re.match(r'^(else\s+if|if|while|else)\b', s)) and not s.endswith(';')
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
    if name == 'include':
        rest = re.sub(r'\s+', '', rest)
    elif name == 'define' and rest:
        dm = re.match(r'^([A-Za-z_]\w*(?:\([^)]*\))?)\s+(.*)$', rest)
        if dm:
            rest = '%s\t%s' % (dm.group(1), fix_spacing(dm.group(2)))
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


HEADER_RE = re.compile(r'^/\* \*{74} \*/$')
ASCII_ART = ["        :::      ::::::::",
             "      :+:      :+:    :+:",
             "    +:+ +:+         +:+  ",
             "  +#+  +:+       +#+     ",
             "+#+#+#+#+#+   +#+        ",
             "     #+#    #+#          ",
             "    ###   ########.fr    "]


def _hline(left, right):
    left = left[:80 - 10 - len(right)]
    return '/*' + ' ' * 3 + left + ' ' * (80 - 10 - len(left) - len(right)) + right + ' ' * 3 + '*/'


def make_header(fname, login, mail, created=None, updated=None):
    now = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    created, updated = created or now, updated or now
    stars = '/* ' + '*' * 74 + ' */'
    return [stars, _hline('', ''), _hline('', ASCII_ART[0]),
            _hline(os.path.basename(fname), ASCII_ART[1]), _hline('', ASCII_ART[2]),
            _hline('By: %s <%s>' % (login, mail), ASCII_ART[3]), _hline('', ASCII_ART[4]),
            _hline('Created: %s by %s' % (created, login), ASCII_ART[5]),
            _hline('Updated: %s by %s' % (updated, login), ASCII_ART[6]),
            _hline('', ''), stars]


def has_header(lines):
    return len(lines) >= 11 and HEADER_RE.match(lines[0]) and HEADER_RE.match(lines[10])


def wrap_long_lines(out):
    """Wrap top-level signatures/prototypes exceeding MAX_COLS at commas."""
    wrapped = []
    for ln in out:
        if width(ln) <= MAX_COLS or '(' not in ln or not ln.rstrip().endswith((')', ');')):
            wrapped.append(ln)
            continue
        idx = ln.find('(')
        prefix = ln[:idx + 1]
        suffix = ');' if ln.rstrip().endswith(');') else ')'
        inner = ln[idx + 1:-len(suffix)]
        parts = [p.strip() for p in split_top(inner, ',')]
        if len(parts) <= 1:
            wrapped.append(ln)
            continue
        cur = prefix + parts[0]
        for p in parts[1:]:
            if width(cur + ', ' + p) <= MAX_COLS:
                cur += ', ' + p
            else:
                wrapped.append(cur + ',')
                cur = '\t\t' + p
        wrapped.append(cur + suffix)
    return wrapped


def fix_source(src, fname, opt):
    orig_lines = src.replace('\r\n', '\n').split('\n')
    lines = kr_split(norm_ws(src))
    header = []
    if has_header(lines):
        header, lines = lines[:11], lines[11:]
        while lines and lines[0] == '':
            lines.pop(0)
    units = scan_units(lines)
    out, pp = [], 0
    for idx, u in enumerate(units):
        k, s, e = u['k'], u['s'], u['e']
        if k == 'func':
            out.append(fix_sig_line(lines[s]) if u['b'] == s + 1 else lines[s].rstrip())
            out.append('{')
            out.extend(fix_body(lines[u['b'] + 1:e - 1]))
            out.append('}' if lines[e - 1].strip() == '}' else lines[e - 1].rstrip())
            if idx + 1 < len(units) and units[idx + 1]['k'] != 'blank':
                out.append('')
        elif k == 'struct':
            out.extend(fix_struct(lines, u))
            if idx + 1 < len(units) and units[idx + 1]['k'] != 'blank':
                out.append('')
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
    out = wrap_long_lines(out)
    # header guard enforcement for .h files
    if fname.endswith('.h'):
        guard = re.sub(r'\W', '_', os.path.basename(fname).upper())
        has_guard = False
        for i, ln in enumerate(out):
            mm = re.match(r'^#\s*ifndef\s+(\w+)$', ln) if isinstance(ln, str) else None
            if mm and i + 1 < len(out) and re.match(r'^#\s*define\s+%s$' % mm.group(1), out[i + 1]):
                out[i] = ln.replace(mm.group(1), guard)
                out[i + 1] = out[i + 1].replace(mm.group(1), guard)
                has_guard = True
                break
        if not has_guard:
            out = ['#ifndef ' + guard, '# define ' + guard, ''] + out + ['', '#endif']
    while out and out[-1] == '':
        out.pop()
    # 42 header
    body_changed = '\n'.join(out) != '\n'.join(l.rstrip() for l in orig_lines[11:]).strip('\n') \
        if header else True
    if header:
        if body_changed and opt.login:
            ts = re.search(r'Created: (\S+ \S+) by', header[7])
            header = make_header(fname, opt.login, opt.mail, ts.group(1) if ts else None)
    elif not opt.no_header and opt.login:
        header = make_header(fname, opt.login, opt.mail)
    res = header + ([''] if header else []) + out
    return '\n'.join(res) + '\n'


# --------------------------------------------------------------------------
# Lint (runs on the fixed text, so line numbers match the file on disk)
# --------------------------------------------------------------------------
def width(line):
    return len(line.expandtabs(TAB))


def lint(text, fname):
    errs = []

    def add(line, col, code, msg):
        errs.append((line, col, code, msg))

    base = os.path.basename(fname)
    if base != base.lower() or not re.match(r'^[a-z0-9_]+\.[ch]$', base):
        add(1, 1, 'INVALID_FILE_NAME', 'File name must be lowercase snake_case ending in .c or .h')
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
            tm = re.match(r'^typedef\b.*\b(\w+);$', code)
            if tm and not tm.group(1).startswith('t_'):
                add(s + 1, 1, 'USER_DEFINED_TYPEDEF', 'typedef name must start with t_')
            pd = parse_decl(code)
            if pd and 'static' not in pd[0].split() and 'const' not in pd[0].split() \
                    and 'extern' not in pd[0].split() and not is_h:
                add(s + 1, 1, 'GLOBAL_VAR_DETECTED', 'Global variable present in file (forbidden unless the project allows it)')
        elif k == 'func':
            nfuncs += 1
            lint_func(lines, u, add)
    if fname.endswith('.c') and nfuncs > MAX_FUNCS:
        add(1, 1, 'TOO_MANY_FUNCS', 'More than %d functions in the file (%d)' % (MAX_FUNCS, nfuncs))
    errs.sort()
    return errs


def lint_func(lines, u, add):
    s, b, e = u['s'], u['b'], u['e']
    body = lines[b + 1:e - 1]
    sg = parse_sig(protect(lines[s])[0].strip())
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
# CLI
# --------------------------------------------------------------------------
def collect(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in sorted(names) if n.endswith(('.c', '.h'))]
        else:
            files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser(description='Auto-fix and lint C files for the 42 Norm v4.1')
    ap.add_argument('paths', nargs='+', help='.c/.h files or directories')
    ap.add_argument('-n', '--check', action='store_true', help='do not write, only report')
    ap.add_argument('-d', '--diff', action='store_true', help='show a diff, do not write')
    ap.add_argument('--login', default=os.environ.get('USER42') or os.environ.get('USER'),
                    help='login for the 42 header (adds/updates it)')
    ap.add_argument('--mail', help='mail for the 42 header (default: <login>@student.42.fr)')
    ap.add_argument('--no-header', action='store_true', help='do not add a missing 42 header')
    ap.add_argument('--allow-globals', action='store_true', help='ignore GLOBAL_VAR_DETECTED errors')
    opt = ap.parse_args()
    if opt.login and not opt.mail:
        opt.mail = '%s@student.42.fr' % opt.login
    bad = 0
    for path in collect(opt.paths):
        try:
            with open(path, encoding='utf-8') as f:
                src = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            print('%s: cannot read (%s)' % (path, exc))
            bad += 1
            continue
        new = fix_source(src, path, opt)
        if opt.diff:
            sys.stdout.writelines(difflib.unified_diff(
                src.splitlines(True), new.splitlines(True), 'a/' + path, 'b/' + path))
        elif not opt.check and new != src:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new)
        errs = lint(new, path)
        if opt.allow_globals:
            errs = [x for x in errs if x[2] != 'GLOBAL_VAR_DETECTED']
        if errs:
            bad += 1
            print('%s: Error!' % path)
            for line, col, code, msg in errs:
                print('Error: %-19s (line: %3d, col: %3d):\t%s' % (code, line, col, msg))
        else:
            print('%s: OK!' % path)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

