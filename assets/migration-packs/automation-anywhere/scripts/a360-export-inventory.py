#!/usr/bin/env python3
"""Inventory an Automation Anywhere Automation 360 bot export - for reading it at genome extraction and for deriving
the migration catalogs at genome execution.

Usage:
  a360-export-inventory.py check   <EXPORT_DIR>               # renderer completeness self-test over dump, compact dump, cards, targets and data; exit 1 on a miss
  a360-export-inventory.py profile <EXPORT_DIR> [--out DIR]   # identity, packages, vocabulary, call graph, roots, credentials, windows, evidence
  a360-export-inventory.py cards   <EXPORT_DIR> [--out DIR]   # one compact card per bot
  a360-export-inventory.py dump    <EXPORT_DIR> <BOT> [--compact]  # one bot, line by line, in execution order, then its VARIABLES legend
                                                              #   (BOT = id, repository path, or a name that occurs once - a repeated name lists the copies)
  a360-export-inventory.py raw     <EXPORT_DIR> <BOT> [--lines FROM[-TO]] [--find TEXT] [--var NAME]
                                                              # the bot file's own nodes (by dump's line numbers, or holding a text) and a variable's
                                                              #   declaration: every key the file holds, secrets masked - to check a reading against the
                                                              #   source, not against a rendering; a targeted lookup, never a reading of a whole bot
  a360-export-inventory.py targets <EXPORT_DIR> [--out DIR]   # UI target catalog: windows + captured controls with decoded object properties, the action applied
                                                              #   and every setting of the command; keystroke and window commands with every setting;
                                                              #   scripted targets (SAP scripting ids, XPaths, selector fragments carried as literals outside captures)
  a360-export-inventory.py data    <EXPORT_DIR> [--out DIR]   # bot inventory (call edges with argument bindings) + data sources the bots read and write

<EXPORT_DIR> is the export root (the folder holding `Bots/`), the `Bots/` folder itself, or any folder above it that
contains exactly one `Bots/` tree. Bot files are extension-less JSON documents with `nodes`, `variables` and
`packages`; `<Bot>Metadata/` folders hold the recorder's screenshots and are never read for logic.

Which mode runs which command, and where the output goes: automation-anywhere/source-guide.md in this repository
(command block at the top); why nothing is written beside the genome: the uipath-planner skill's Source Map contract;
execution-time derivation: the uipath-genome skill's migration preflight.
Writes a360-profile.txt / a360-cards.txt / a360-targets.{json,md} / a360-process-data.json / a360-data-sources.{json,md}
into --out (default: current directory); dump and raw print to stdout.
Never prints a literal typed into a password field, a default of a variable whose name contains password/pwd/secret/token
or whose type is CREDENTIAL, or a value of an attribute so named - in free text (comments, descriptions, log lines)
neither. No rendering cuts a value at a fixed length. Credential Vault references (locker/credential.attribute) are identity, not secrets, and
are kept - the uipath-planner skill's Platform Dependencies rules. Enumeration attributes that name an
action or a mode (`passwordtextAction`, `getCharacters`, `typeOfInput`, ...) are never secrets and are never masked.

Bot identity: Automation 360 exports carry no numeric object ids; a bot is identified by its repository path below
`Bots/`. The script derives a stable six-digit id from that path (CRC-32, folded into 100000-999999) so Source Map rows
can carry `Name` (id) and the skill's check-genome.mjs can resolve the step tables' citations against the files `data`
writes; the same export always yields the same ids.
"""
import argparse
import base64
import collections
import itertools
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import zlib
import xml.etree.ElementTree as ET

SENSITIVE = re.compile(r'passw|pwd|secret|token|api[_-]?key', re.I)
# Attributes whose value is an enumeration naming an action or a mode (`passwordtextAction=SETTEXT`,
# `getCharacters=BEFOREAFTER`): never a secret, even when the attribute name matches SENSITIVE or the node assigns into
# a password-named variable. Without this exemption the verb of every type-into and the mode of every parsing step
# would be harvested as a "secret" and masked wherever the word appears.
ENUM_ATTR = re.compile(r'Action$|^(getCharacters|typeOfInput|typeOfInputStringToType|operator|delayType|timeUnit|readStatus|'
                       r'bodyFormat|openOption|browser|waitType|filterType|loopType|connectionMode|connectionOption|serverType|'
                       r'authType|protocol|inOrOutParamter|outputParamType|paramType|logLevel|forOptions|fileAccessMode|'
                       r'exceptionType|advancedWait|presetType)$')
# String literals that are UI identities of a scripting model rather than recorder captures: a SAP GUI scripting id
# (`wnd[0]/usr/ctxtRF05A-AGKON`, also the bare `usr/...` / `tbar[0]/...` tail some wrappers take), a UiPath selector
# fragment. `targets` lists them per bot as `scriptedTargets` so an identity carried as the argument of a sub-bot,
# script, DLL or package call reaches the catalog (source guide § UI Target Locators). An XPath is not in the grammar:
# outside a capture it is an XML node path (`XML.getSingleNodeV2`), not a UI identity.
SCRIPTED_TARGET = re.compile(r'^\s*(?:wnd\[\d+\]/\S*|(?:usr|tbar\[\d+\]|mbar|sbar|titl)/\S+|<(?:wnd|html|ctrl|uia|java|sap|webctrl)\b[^>]*>)\s*$')
# The same id embedded in a longer script statement (`session.findById("wnd[0]/usr/.../shell").setCurrentCell 0, "X"`).
EMBEDDED_SAP_ID = re.compile(r'wnd\[\d+\]/[^"\'\s\\)]+')
# Literal credentials that hide inside otherwise harmless attributes (an `Authorization` header in a disabled test call):
# a bearer/basic scheme followed by a literal, and JSON Web Tokens (three base64url segments starting with `eyJ`).
BEARER_LITERAL = re.compile(r'(?i)\b(bearer|basic)\s+(?!\$)([A-Za-z0-9._~+/=\-]{16,})')
JWT_LITERAL = re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}')
BRANCH_COMMANDS = ('else', 'elseIf', 'catch', 'finally')
# The Screen package's actions, each saving a screenshot to a file: Capture desktop, Capture area, Capture window
SCREENSHOTS = {('Screen', 'captureDesktop'), ('Screen', 'captureArea'), ('Screen', 'captureWindow')}
# Not steps: authoring notes, the run's own log and screenshots, timing, debugging leftovers, migration plumbing.
NOISE = {('Comment', 'Comment'), ('LogToFile', 'logToFile'), ('Delay', 'delay'), ('LegacyAutomation', 'getKeystrokesDelay'),
         ('MessageBox', 'messageBox'), ('Step', 'step')} | SCREENSHOTS
SEPARATOR = re.compile(r'^[=\-\*\|#_~\s]*$')
# A literal holding text with a variable's shape (a template's own `$name$` placeholder): a `string` channel is never
# substituted, so the text is not a read of that variable
TOKEN_SHAPED = re.compile(r'\$[A-Za-z_@][^$\s]*\$')
REPO_PREFIX = re.compile(r'^(repository:///)?(automation anywhere/)?bots/', re.I)
GLOBAL_VALUE = re.compile(r'\$@([A-Za-z0-9_]+)\$')
SYSTEM_VAR = re.compile(r'\$System:([A-Za-z0-9_]+)')
VAR_REF = re.compile(r'\$([A-Za-z0-9_\-]+)')
VAR_NAME = re.compile(r'[A-Za-z0-9_\-]+')
SCRIPT_EXT = ('.bat', '.cmd', '.ps1', '.vbs', '.py', '.js', '.exe')
SCRIPT_HOSTS = ('powershell.exe', 'powershell', 'cmd.exe', 'cmd', 'cscript.exe', 'wscript.exe')
# Commands a logger sub-bot may consist of (besides the log line itself): control flow, string and number preparation,
# the log's own file or folder created, error handling. A bot made only of these with at least one logToFile, and
# storing into no output variable, is a logger bot - calls to it are the run's own logging, not steps (source guide
# § Evidence; `Export._is_logger`).
LOGGER_ALLOWED_PACKAGES = {'LogToFile', 'If', 'String', 'Number', 'Datetime', 'System', 'Boolean', 'Comment', 'Delay', 'Loop',
                           'ErrorHandler', 'TaskBot', 'File', 'Folder', 'MessageBox', 'Step', 'Screen', 'LegacyAutomation', 'Dictionary', 'List'}
LOGGER_FILE_COMMANDS = {'createFile', 'createFolder'}
# Commands that store into the variable an attribute names, with no `returnTo`: `Window.SetTitle` rewrites the window
# variable in `sourceWindow` to the new title (source guide § Target Resolution 1)
STORED_ATTRS = {('Window', 'SetTitle'): ('sourceWindow',)}
# Commands the target catalog lists per bot as window operations, with every setting: window handling, waits, and
# mouse clicks at a point of a window (a coordinate click has no control, only its window and the point), and clicks on
# an image found in a window (an image file or a window area matched in the window's picture)
WINDOW_PACKAGES = ('Window', 'Wait', 'Mouse', 'ImageRecognition')
# The mail-send commands: the classic send and its second version, whose attachments are a file list and whose server
# settings are `emailServerHost` / `emailServerPort`
MAIL_SENDS = ('sendMail', 'SendMailV2')
# Data-bearing commands the data catalog lists (package, command) -> kind
DATA_COMMANDS = {('CsvTxt', 'OpenCSVTXT'): 'csv-file', ('TextFile', 'ReadFile'): 'text-file', ('TextFile', 'WriteFile'): 'text-file',
                 ('PDF', 'extractText'): 'pdf-file', ('Browser', 'downloadFile'): 'download', ('Json', 'StartSession'): 'json-file'}
# Attributes by which the data catalog recognises a mail or document-store command of a package it has no rule for
RECIPIENT_ATTRS = {'sendTo', 'toAddress', 'recipients', 'recipient', 'forwardTo'}
MAILBOX_ATTRS = {'mailbox', 'messageId', 'delegatedMail', 'folderName'}
REMOTE_STORE_ATTRS = {'documentLibraryName', 'sharepointName', 'sharepointSubsite', 'siteUrl', 'remotePath'}
MAIL_DETAIL_ATTRS = {'sendCc', 'cc', 'subject', 'attachments', 'sendAs', 'downloadPath', 'pattern', 'localFile', 'relativePath',
                     'filterByFrom', 'filterBySubject', 'filterByAttachments', 'messageStatus', 'mailShouldBeMarkedAsRead'}

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def stable_id(key):
    """Six-digit id derived from a repository path or data-source identity; same input, same id."""
    return 100000 + zlib.crc32(key.encode('utf-8')) % 900000


def at(bot, ln):
    """A node's citation: bot name and id, then the editor line - bots are named alike across folders, so a name and a
    line alone can point at two copies."""
    return f"`{bot.name}` ({bot.id}):{ln}"


def repo_path(s):
    """`repository:///Automation%20Anywhere/Bots/My%20Tasks/X/Y` -> `My Tasks/X/Y`."""
    s = urllib.parse.unquote(s or '').replace('\\', '/')
    return REPO_PREFIX.sub('', s).strip('/')


def capture_action(attrs):
    """The action a Recorder capture applies: the one attribute named `<control>Action` (textbox, button, client, label,
    passwordtext, combobox, tree, listview, radio, checkbox, menu, ...)."""
    for k, v in attrs.items():
        if k and k.endswith('Action') and k != 'defaultAction' and isinstance(v, dict) and v.get('string'):
            return v['string']
    return None


def is_script_launch(attrs, x=None, bot=None):
    """`runApp` of a batch / PowerShell / VBScript file or of a script host = a script the export does not contain."""
    fp = attrs.get('filePath') or {}
    s = (fp.get('expression') or fp.get('string') or '').lower()
    params = attrs.get('parameters') or {}
    p = (params.get('expression') or params.get('string') or '').lower()
    base = os.path.basename(s.replace('\\', '/'))
    return base in SCRIPT_HOSTS or s.endswith(SCRIPT_EXT[:-1]) or any(p.endswith(e) or (e + ' ') in p for e in SCRIPT_EXT[:-1])


# The text renderings print a value as written. Python's repr and JSON escape it - a folder path `C:\A\B` reads as
# `C:\\A\\B`, a quote as `\"` - and a reader copies the escaped form. The JSON outputs (a360-targets.json,
# a360-process-data.json, a360-data-sources.json, raw) keep JSON's escapes: a JSON reader decodes them.
INVISIBLE = re.compile(r'[^\x20-\x7e]|\\(?=u[0-9A-Fa-f]{4})')


def visible(s):
    """`s` with every character a reader cannot see - line breaks, tabs, format characters, spaces other than U+0020 -
    written as \\uXXXX: each is part of the value, and a line break printed as it is would split the rendering's line.
    A backslash before `u` and four hex digits is written \\u005C, so that every \\uXXXX shown is one character."""
    return INVISIBLE.sub(lambda m: f"\\u{ord(m.group()):04X}" if m.group() == '\\' or unicodedata.category(m.group())[0] in 'CZ' else m.group(), s)


def oneline(s):
    """Free text (a comment, a description) on one line: line breaks as ` / `."""
    return (s or '').replace('\r\n', ' / ').replace('\n', ' / ')


def prose(s):
    """`oneline`, other invisible characters `visible`. Free text a rendering quotes is `lit(oneline(...))`: `visible`
    twice would write its own \\uXXXX as \\u005C."""
    return visible(oneline(s))


def lit(s):
    """A text value in double quotes as written, `visible`."""
    return f'"{visible(s)}"'


def json_text(o):
    """A JSON-shaped value (dict, list, text, number, boolean, None) laid out as JSON writes it, each text `lit`."""
    if isinstance(o, dict):
        return '{' + ', '.join(f"{lit(str(k))}: {json_text(v)}" for k, v in o.items()) + '}'
    if isinstance(o, (list, tuple)):
        return '[' + ', '.join(map(json_text, o)) + ']'
    return lit(o) if isinstance(o, str) else json.dumps(o)


def py_text(o):
    """A list, tuple or dict of texts laid out as Python prints it, each text in Python's quotes as written, `visible`."""
    if isinstance(o, str):
        q = '"' if "'" in o and '"' not in o else "'"
        return f"{q}{visible(o)}{q}"
    if isinstance(o, dict):
        return '{' + ', '.join(f"{py_text(k)}: {py_text(v)}" for k, v in o.items()) + '}'
    if isinstance(o, tuple):
        return '(' + ', '.join(map(py_text, o)) + (',)' if len(o) == 1 else ')')
    if isinstance(o, list):
        return '[' + ', '.join(map(py_text, o)) + ']'
    return repr(o)  # a number, a boolean, None


def title_regex(w):
    """A window title matched by a regular expression instead of a wildcard title (`nameRegex`): its conditions, each
    a `STRING` or a `PATTERN` with its text (vendor documentation), and the regex flags set on it."""
    rx = w.get('nameRegex') or {}
    return [(c.get('type') or '?', channel(c)) for c in rx.get('list') or []], [k for k, on in (rx.get('regexFlags') or {}).items() if on is True]


def fmt_window(w):
    """A recorded window as `win("<title>" <class> <exe>)`; a preset target (`presetType` CURRENTLY_ACTIVE: whatever
    window has the focus) as `win(<CURRENTLY_ACTIVE>)` - it carries no title, and an empty title is not a window. A
    title matched without regard to case (`nameCaseInsensitive`) says so: the target's title is then verified live.
    A title matched by a regular expression prints each condition as `pattern"…"` / `string"…"` with its flags; a
    window picked as a browser tab prints its kind (`windowType=BROWSER_IE`) and the tab's URL at capture."""
    if not w:
        return None
    conds, flags = title_regex(w)
    if w.get('presetType') not in (None, '', 'NONE') and not w.get('name') and not conds:
        return f"win(<{w['presetType']}>)"
    exe = os.path.basename((w.get('path') or '').replace('\\', '/')) or None
    parts = [lit(w.get('name') or '')] if w.get('name') or not conds else []
    parts += [f"{kind.lower()}{lit(text)}" for kind, text in conds]
    if flags:
        parts.append('regexFlags=' + ','.join(flags))
    if w.get('class'):
        parts.append(w['class'])
    if exe:
        parts.append(exe)
    if w.get('nameCaseInsensitive') is True:
        parts.append('nameCaseInsensitive')
    if w.get('windowType') not in (None, '', 'NATIVE'):
        parts.append(f"windowType={w['windowType']}")
    if w.get('browserTabUrl'):
        parts.append(f"tabUrl={lit(w['browserTabUrl'])}")
    return 'win(' + ' '.join(parts) + ')'


def window_record(w, variable=None):
    """A recorded window as the catalogs keep it: title, class, executable name, preset target, and - when set - the
    case rule, the title's regular expression, the window kind other than native and the browser tab's URL."""
    conds, flags = title_regex(w)
    rec = {'variable': variable, 'title': w.get('name') or None, 'class': w.get('class') or None,
           'exe': os.path.basename((w.get('path') or '').replace('\\', '/')) or None,
           'presetType': w.get('presetType') if w.get('presetType') not in (None, '', 'NONE') else None}
    if w.get('nameCaseInsensitive') is True:
        rec['nameCaseInsensitive'] = True
    if conds:
        rec['titleRegex'] = {'conditions': [{'type': k, 'text': t} for k, t in conds], 'flags': flags}
    if w.get('windowType') not in (None, '', 'NATIVE'):
        rec['windowType'] = w['windowType']
    if w.get('browserTabUrl'):
        rec['tabUrl'] = w['browserTabUrl']
    return rec


def fmt_catalog_window(w):
    """A window as the catalogs record it (`window_record`) in the `win(...)` form, then the size a command resizes it
    to before it acts (`resize(width=…, height=…)`) when the command carries one."""
    rx = w.get('titleRegex') or {}
    raw = {'name': w.get('title'), 'class': w.get('class'), 'path': w.get('exe'), 'presetType': w.get('presetType'),
           'nameCaseInsensitive': w.get('nameCaseInsensitive'), 'windowType': w.get('windowType'), 'browserTabUrl': w.get('tabUrl'),
           'nameRegex': {'list': [{'type': c['type'], 'string': c['text']} for c in rx.get('conditions') or []],
                         'regexFlags': {f: True for f in rx.get('flags') or []}}}
    rs = w.get('resize')
    return fmt_window(raw) + (f" resize(width={rs['width']}, height={rs['height']})" if rs else '')


def decode_blob(blob):
    """The recorder stores the captured object tree as base64 JSON; strip the protobuf memo fields it carries."""
    if not blob:
        return {}
    try:
        d = json.loads(base64.b64decode(blob).decode('utf-8'))
    except Exception as e:  # noqa: BLE001 - a bad blob is reported, not fatal
        return {'error': str(e)}

    def prune(o):
        if isinstance(o, dict):
            return {k: prune(v) for k, v in o.items() if k not in ('memoizedIsInitialized', 'unknownFields', 'memoizedSize', 'memoizedHashCode', 'actionsMemoizedSerializedSize', 'searchCriteriaMemoizedSerializedSize')}
        if isinstance(o, list):
            return [prune(x) for x in o]
        return o
    return prune(d)


def vision_record(d):
    """An AISense capture's decoded blob, which carries no object tree: the control a vision model recognised on the
    screen (`mapping.control`: type, the text it shows, bounding box, score) and the anchors it was matched with, every
    key kept but the picture itself, whose size stays. None for the recorder's object-tree captures."""
    if not isinstance(d, dict) or 'mapping' not in d or 'objNode' in d:
        return None

    def keep(o):
        if isinstance(o, dict):
            return {k: (f"<{len(v)} base64 characters>" if k == 'imageBase64' and isinstance(v, str) else keep(v))
                    for k, v in o.items() if v not in (None, '', [], {})}
        if isinstance(o, list):
            return [keep(v) for v in o]
        return o
    return keep(d)


def as_bool(v):
    """The truth of a BOOLEAN value. The `boolean` channel holds a JSON boolean or the strings "true" / "false"; an
    empty channel is unset. A bare truthiness test reads the string "false" as true - every flag test goes through here."""
    b = (v or {}).get('boolean')
    if isinstance(b, bool):
        return b
    return isinstance(b, str) and b.strip().lower() == 'true'


def read_names(expr):
    """The bot variables an expression reads. `$$` is a literal dollar; a reference nests inside another's brackets
    (`$list[$i$]$`, `$v.Pkg:fn($w$)$`); `$Pkg:Value$`, `$System:X$` and `$@Global$` are not bot variables."""
    names = []

    def ref(i):  # i: just past an opening `$`; returns the index past its closing `$`
        m = VAR_NAME.match(expr, i)
        if m and not expr.startswith(':', m.end()):
            names.append(m.group())
        depth, j = 0, m.end() if m else i
        while j < len(expr):
            c = expr[j]
            if c == '$' and depth <= 0:
                return j + 1
            if c == '$':
                j = ref(j + 1)
                continue
            depth += 1 if c in '([{' else -1 if c in ')]}' else 0
            j += 1
        return j
    i = 0
    while i < len(expr):
        if expr.startswith('$$', i):
            i += 2
        elif expr[i] == '$':
            i = ref(i + 1)
        else:
            i += 1
    return names


def var_key(name):
    """A variable's identity as the platform compares names: letter case ignored, `_` and `-` the same character
    (vendor documentation), so every spelling with one key reads and writes one variable."""
    return name.lower().replace('-', '_')


def own_values(o):
    """Every value object a node holds in its own settings, its body and arms excluded."""
    if isinstance(o, dict):
        yield o
        for k, v in o.items():
            if k not in ('children', 'branches'):
                yield from own_values(v)
    elif isinstance(o, list):
        for v in o:
            yield from own_values(v)


def flat_attrs(n):
    """A node's attributes as name -> value, including the options nested under a choice (`doExport` carries its
    `filePath` as a sub-attribute): a lookup by name must find a setting wherever the command nests it. A top-level
    attribute wins over a nested one of the same name."""
    out = {}
    for a in n.get('attributes', []) or []:
        if a.get('name'):
            out[a['name']] = a.get('value') or {}
    for a in n.get('attributes', []) or []:
        for sub in a.get('attributes') or []:
            if sub.get('name') and sub['name'] not in out:
                out[sub['name']] = sub.get('value') or {}
    return out


def channel(v):
    """The raw text of a scalar value. Every value dict carries all channels (`string`, `number`, `boolean`, ...) with
    the unused ones empty, so the channel is chosen by the value's `type` and by emptiness, never by key presence:
    a NUMBER lives in `number`, a BOOLEAN in `boolean`. An expression (a variable reference) wins over a literal."""
    if not v:
        return ''
    if v.get('expression') not in (None, ''):
        return v['expression']
    order = {'NUMBER': ('number', 'string'), 'BOOLEAN': ('boolean', 'string')}.get(v.get('type'), ('string', 'number', 'boolean'))
    for k in order:
        val = v.get(k)
        if val not in (None, ''):
            return str(val).lower() if k == 'boolean' else str(val)
    return ''


def joined_tests(n, join):
    """The single tests of a node's condition when every link joins them with `join` (AND or OR), groups included;
    None when another join occurs. One test needs no join."""
    conds = [a for a in n.get('attributes', []) or []
             if (a.get('value') or {}).get('type') in ('CONDITIONAL', 'CONDITIONALGROUP') or a.get('groupAttribute') is not None]
    if len(conds) > 1 and (channel(flat_attrs(n).get('operator')) or 'AND').upper() != join:
        return None
    tests = []

    def take(x):  # a test, or a holder of a parenthesised group
        if x.get('groupAttribute') is not None:
            return chain(x['groupAttribute'])
        tests.append(x)
        return True

    def chain(x):
        if not take(x):
            return False
        c = x.get('operatorAttribute')
        while c:
            if (c.get('operator') or 'AND').upper() != join or not take(c):
                return False
            c = c.get('operatorAttribute')
        return True
    return tests if all([chain(a) for a in conds]) else None


def equality_tests(n):
    """[(variable key, '=' or '!=', literal, case rule, text)]: the comparisons a node's test holds for its whole body -
    one comparison, or comparisons joined by AND only (an OR anywhere: None, no single value follows). Only the string
    condition family counts: its `EQ` / `NEQ` and case rule are stated, while the legacy family states no case rule
    and leaves an empty operand undocumented. Case rule: True matched, False ignored."""
    tests = joined_tests(n, 'AND')
    if tests is None:
        return None
    out = []
    for t in tests:
        if (t.get('value') or {}).get('conditionalName') != 'stringVariable':
            continue
        sub = {x.get('name'): x.get('value') or {} for x in t.get('attributes', []) or []}
        m = re.fullmatch(r'\$([^$.{}\[\]]+)\$', (sub.get('variable') or {}).get('expression') or '')
        op, value = (sub.get('operator') or {}).get('string'), sub.get('value') or {}
        if not m or op not in ('EQ', 'NEQ') or value.get('expression') not in (None, '') or channel(sub.get('matchCase')) == '':
            continue
        text = value.get('string') or ''
        out.append((var_key(m.group(1)), '=' if op == 'EQ' else '!=', text, as_bool(sub.get('matchCase')),
                    f"${m.group(1)}$ {op} {lit(text)} {'[matchCase]' if as_bool(sub.get('matchCase')) else '[ignoreCase]'}"))
    return out


LIMIT_OPS = {'EqualsTo': '=', 'EQ': '=', 'GreaterThanOrEqualTo': '>=', 'GTE': '>=', 'GreaterThan': '>', 'GT': '>'}


def limit_tests(n):
    """[(variable, op, integer limit, legacy)]: the comparisons of a variable with an integer literal - '=', '>=' or
    '>' - each of which makes the node's whole test true on its own (one test, or one of an OR-only chain). `legacy`:
    an ordering test of the legacy family, which may compare text rather than numbers (undocumented)."""
    out = []
    for t in joined_tests(n, 'OR') or []:
        cname = (t.get('value') or {}).get('conditionalName')
        if cname not in ('legacyConditionVariable', 'stringVariable', 'numberVariable'):
            continue
        sub = {x.get('name'): x.get('value') or {} for x in t.get('attributes', []) or []}
        m = re.fullmatch(r'\$([^$.{}\[\]]+)(\.[A-Za-z]+:[A-Za-z]+)*\$', (sub.get('variable') or {}).get('expression') or '')
        op, value = LIMIT_OPS.get((sub.get('operator') or {}).get('string')), sub.get('value') or {}
        if m and op and value.get('expression') in (None, '') and re.fullmatch(r'-?\d+', channel(value)):
            out.append((m.group(1), op, int(channel(value)), cname == 'legacyConditionVariable' and op != '='))
    return out


def capture_signature(n):
    """A capture's identity across copies: the control's criteria and window, the action and its settings, the result
    variable - waits and delays aside, since a re-read often waits otherwise than the first read."""
    sig = {'result': (n.get('returnTo') or {}).get('variableName')}
    for a in n.get('attributes', []) or []:
        v = a.get('value') or {}
        if a.get('name') == 'uiObject':
            sig['control'] = json.dumps([(v.get('uiObject') or {}).get('criteria'), channel(v.get('uiObjectWindow'))], sort_keys=True)
        elif a.get('name') not in ('wait', 'delay', 'advancedWait', 'runInBackground'):
            sig[a.get('name')] = channel(v)
    return json.dumps(sig, sort_keys=True)


def rules_out(outer, inner):
    """True when no value passing `outer` passes `inner`; each is (op, literal, case rule), op '=' or '!='. Literals
    whose letters differ only in case compare alike or not by the rules, so such a pair is ruled out only when both
    rules agree."""
    (o1, l1, c1), (o2, l2, c2) = outer, inner
    if o1 != o2:  # one fixes the value, the other excludes the same literal
        if l1.lower() == l1.upper():
            return l1 == l2
        return c1 == c2 and (l1 == l2 if c1 else l1.lower() == l2.lower())
    if o1 == '=':  # fixed to one literal, tested for another
        return l1.lower() != l2.lower() or (c1 and c2 and l1 != l2)
    return False


class Bot:
    def __init__(self, export, abs_path, rel_path):
        self.export = export
        self.abs = abs_path
        self.path = rel_path
        self.name = os.path.basename(rel_path)
        self.folder = os.path.dirname(rel_path)
        self.id = stable_id(rel_path)
        with open(abs_path, encoding='utf-8-sig') as fh:
            self.doc = json.load(fh)
        self.vars = {v.get('name'): v for v in self.doc.get('variables', [])}
        self.lines = []  # (line, depth, node, is_branch) in editor order
        self.anc = {}  # id(node) -> the enclosing nodes, outermost first; a branch's owner encloses the branch
        self.line_no = {}  # id(node) -> its editor line
        self._number(self.doc.get('nodes', []), 0)
        self.metadata_dir = abs_path + 'Metadata'

    def _number(self, nodes, depth, parent_disabled=False, anc=()):
        """Editor line numbers; a disabled node disables its whole subtree (the runtime skips it), so the flag is
        propagated to children and branches - every `disabled` test downstream then means "not executed"."""
        for n in nodes:
            if parent_disabled:
                n['disabled'] = True
            off = bool(n.get('disabled'))
            self.anc[id(n)] = anc
            self.lines.append((len(self.lines) + 1, depth, n, False))
            self.line_no[id(n)] = len(self.lines)
            self._number(n.get('children', []), depth + 1, off, anc + (n,))
            for b in n.get('branches', []):
                if off:
                    b['disabled'] = True
                self.anc[id(b)] = anc + (n,)
                self.lines.append((len(self.lines) + 1, depth, b, True))
                self.line_no[id(b)] = len(self.lines)
                self._number(b.get('children', []), depth + 1, off or bool(b.get('disabled')), anc + (n, b))

    def rethrow_lines(self):
        """The enabled `throw` commands inside a `finally` arm: the bot can raise to its caller after its catch has run,
        so a caller that tests the error flag the bot returns never sees that failure as the flag."""
        return [ln for ln, _, n, _ in self.lines if not n.get('disabled') and n.get('commandName') == 'throw'
                and any(a.get('commandName') == 'finally' for a in self.anc.get(id(n), ()))]

    def _var_uses(self):
        """(stores, reads): each variable name as the enabled commands write it -> the lines that store into it (a
        result, a catch's returns, a loop variable, a table or list changed in place) / that read it. None when a
        table-row loop binds its columns to same-named variables (no row variable), whose names the export does not list."""
        stores, reads = collections.defaultdict(set), collections.defaultdict(set)
        for ln, _, n, _ in self.lines:
            if n.get('disabled'):
                continue
            it = (flat_attrs(n).get('iterator') or {}).get('iteratorName') or ''
            if 'TableRow' in it and not any(a.get('returnTo') for a in n.get('attributes', []) or []) and not n.get('returnTo'):
                return None
            for name in self.stored_names(n):
                stores[name].add(ln)
            for o in own_values(n):
                if isinstance(o.get('expression'), str):
                    for name in read_names(o['expression']):
                        reads[name].add(ln)
        return stores, reads

    @staticmethod
    def stored_names(n):
        """The variables one node writes as it holds them: its results, a catch's returns, a loop variable, a table or
        list changed in place, the window a set-title rewrites (STORED_ATTRS)."""
        names = {name for nm in STORED_ATTRS.get((n.get('packageName'), n.get('commandName')), ())
                 for name in read_names(channel(flat_attrs(n).get(nm)))}
        names.update(o['variableName'] for o in own_values(n)
                     if o.get('type') == 'VARIABLE' and o.get('variableName') and not o.get('packageName'))
        return names

    def catch_results(self):
        """[(name, lines)]: what an enabled catch arm hands back - the variables flagged output that its enabled
        commands (the catch's own returns included) store into and that no later line of the bot reads. A store the
        bot reads again is its own working state (a log path, the task name in an error line); the flags alone do not
        tell, since migrated bots flag nearly every variable output."""
        outputs = {var_key(nm): nm for nm, v in self.vars.items() if v.get('output')}
        uses = self._var_uses()
        last_read = collections.defaultdict(int)
        for name, lns in (uses[1] if uses else {}).items():
            last_read[var_key(name)] = max(last_read[var_key(name)], *lns)
        found = collections.defaultdict(set)
        for ln, _, n, _ in self.lines:
            if n.get('disabled') or not (n.get('commandName') == 'catch' or any(a.get('commandName') == 'catch' for a in self.anc.get(id(n), ()))):
                continue
            for name in self.stored_names(n):
                if var_key(name) in outputs and last_read[var_key(name)] <= ln:
                    found[outputs[var_key(name)]].add(ln)
        return sorted((nm, sorted(lns)) for nm, lns in found.items())

    def loop_jump(self, n):
        """Where a `break` or `continue` sends the run: the loop its label (`anchor`) names, else the nearest
        enclosing loop (vendor documentation). A `break` leaves that loop only - the commands after it still run."""
        label = channel(flat_attrs(n).get('anchor'))
        verb = 'leaves' if n.get('commandName') == 'loop.commands.break' else 'next pass of'
        if label:
            return f"({verb} the loop labelled {lit(label)})"
        loop = next((a for a in reversed(self.anc.get(id(n), ())) if a.get('commandName') == 'loop.commands.start'), None)
        return f"({verb} the loop at {self.line_no[id(loop)]})" if loop is not None else ''

    def subtree(self, n):
        """(line, node) of every node inside `n`, in editor order."""
        for ln, _, m, _ in self.lines[self.line_no[id(n)]:]:
            if not any(a is n for a in self.anc.get(id(m), ())):
                return
            yield ln, m

    def siblings_before(self, n):
        """The nodes before `n` in the list that holds it, nearest first."""
        anc = self.anc.get(id(n), ())
        nodes = (anc[-1].get('children') if anc else self.doc.get('nodes')) or []
        i = next((j for j, s in enumerate(nodes) if s is n), 0)
        return list(reversed(nodes[:i]))

    def pass_body(self, loop):
        """(line, node, enclosing nodes inside the loop) of the enabled commands one pass of `loop` runs itself - its
        body and arms, the bodies of loops nested in it excluded."""
        out = []
        for ln, m in self.subtree(loop):
            anc = self.anc.get(id(m), ())
            inner = anc[next(i for i, a in enumerate(anc) if a is loop) + 1:]
            if not m.get('disabled') and not any(a.get('commandName') == 'loop.commands.start' for a in inner):
                out.append((ln, m, inner))
        return out

    @staticmethod
    def increment_of(m):
        """The variable a command adds one to and stores back: `$c.LegacyAutomation:getIncrementedNumber…$`,
        `$c$+1` (with the parse re-assignment migrated bots add) or a Number increment by 1."""
        ret = (m.get('returnTo') or {}).get('variableName')
        if not ret:
            return None
        a, k = flat_attrs(m), re.escape(ret)
        if m.get('commandName') == 'increment':
            ok = re.fullmatch(rf'\${k}\$', channel(a.get('source')), re.I) and channel(a.get('by')) == '1'
        else:
            src = channel(a.get('sourceString') or a.get('input'))
            ok = re.fullmatch(rf'\${k}\.LegacyAutomation:getIncrementedNumber(\.[A-Za-z]+:[A-Za-z]+)*\$|\${k}\$\s*\+\s*1', src, re.I)
        return ret if ok else None

    def start_value(self, loop, key):
        """The integer literal a counter holds when `loop` starts: the nearest preceding sibling that stores into it,
        when that store assigns an integer literal. None otherwise, or when a sibling stores into it inside its body."""
        for s in self.siblings_before(loop):
            if s.get('disabled'):
                continue
            if any(var_key(nm) == key for nm in self.stored_names(s)):
                a = flat_attrs(s)
                v = a.get('sourceString') or a.get('input') or {}
                text = channel(v) if v.get('expression') in (None, '') else ''
                return int(text) if re.fullmatch(r'-?\d+', text) else None
            if any(not m.get('disabled') and key in {var_key(nm) for nm in self.stored_names(m)} for _, m in self.subtree(s)):
                return None
        return None

    def pass_limit(self, loop):
        """(passes, text, test line) of the earliest pass on which a counter test breaks out of `loop`, or None. The body adds one to
        the counter on every pass (inside no arm) and stores nothing else into it; an `if` that every pass reaches tests
        it against an integer literal and holds a `break` that leaves this loop; the counter's literal start is set just
        before the loop. A pass tests start + pass when the increment comes first, start + pass - 1 when the test does."""
        body = self.pass_body(loop)
        if any(m.get('commandName') == 'loop.commands.continue' and not channel(flat_attrs(m).get('anchor')) for _, m, _ in body):
            return None  # a pass a continue cuts short may skip the increment or the test
        incs, stores = collections.defaultdict(list), collections.defaultdict(int)
        for ln, m, inner in body:
            name = self.increment_of(m)
            if name and all(a.get('commandName') in ('try', 'step') for a in inner):
                incs[var_key(name)].append(ln)
            for nm in self.stored_names(m):
                if not re.fullmatch(rf'\${re.escape(nm)}\.LegacyAutomation:parseVariableOperation\$', channel(flat_attrs(m).get('sourceString')), re.I):
                    stores[var_key(nm)] += 1
        best = None
        for ln, m, inner in body:
            if m.get('commandName') != 'loop.commands.break' or channel(flat_attrs(m).get('anchor')):
                continue
            for i, t in enumerate(inner):
                if t.get('commandName') != 'if' or not all(a.get('commandName') in ('try', 'step') for a in inner[:i] + inner[i + 1:]):
                    continue  # the test must run on every pass and the break sit in its own body
                for name, op, limit, legacy in limit_tests(t):
                    key = var_key(name)
                    if len(incs.get(key, ())) != 1 or stores[key] != 1:
                        continue
                    start = self.start_value(loop, key)
                    if start is None:
                        continue
                    test_ln = self.line_no[id(t)]
                    first = start + 1 if incs[key][0] < test_ln else start  # the value the first pass tests
                    need = limit + 1 if op == '>' else limit
                    if op == '=' and need < first:
                        continue  # never equal: the test cannot end the loop
                    passes = max(1, need - first + 1)
                    text = (f"at most {passes} pass{'' if passes == 1 else 'es'}: line {test_ln} breaks out when ${name}$ {'=' if op == '=' else '≥' if op == '>=' else '>'} {limit}"
                            + (' (if the legacy comparison reads numbers)' if legacy else ''))
                    if best is None or passes < best[0]:
                        best = (passes, text, test_ln)
        return best

    def loop_reading(self, loop):
        """`[...]` after a loop line: a While loop's form - it tests before each pass, or runs once before the first test
        (`checkConditionAtEnd`, vendor documentation) - the pass limit a counter test sets, and a capture just before
        the loop that the body repeats (a do-while written by hand). With a pass limit, a repeat placed before the
        counter test runs on every pass and one after it misses the pass that breaks out."""
        a = flat_attrs(loop)
        parts = []
        if channel(a.get('loopType')) == 'WHILE':
            parts.append('runs once before the first test, then tests after each pass' if as_bool(a.get('checkConditionAtEnd')) else 'tests before each pass')
        limit = self.pass_limit(loop)
        passes = limit[0] if limit else None
        if limit:
            parts.append(limit[1])
        times = (a.get('times') or {}) if (a.get('iterator') or {}).get('iteratorName') == 'loop.iterators.times' else {}
        if passes is None and times.get('expression') in (None, '') and re.fullmatch(r'\d+', channel(times)):
            passes = int(channel(times))
        repeats = {}
        for ln, m, _ in self.pass_body(loop):
            if m.get('commandName') == 'capture':
                repeats.setdefault(capture_signature(m), ln)
        for s in self.siblings_before(loop)[:6]:
            if s.get('commandName') == 'loop.commands.start':
                break
            hit = next(((ln, repeats[capture_signature(m)]) for ln, m in [(self.line_no[id(s)], s)] + list(self.subtree(s))
                        if not m.get('disabled') and m.get('commandName') == 'capture' and capture_signature(m) in repeats), None)
            if hit:
                inside = passes - 1 if passes and limit and hit[1] > limit[2] else passes
                parts.append(f"line {hit[0]} runs before the loop and again at {hit[1]} inside it"
                             + (f": up to {inside + 1} times" if passes else ''))
                break
        return f"[{'; '.join(parts)}]" if parts else ''

    def ruled_out_tests(self):
        """[(line, test, enclosing line, enclosing test)]: an enabled `if` / `elseIf` whose test cannot hold where it
        sits. An enclosing arm's test - one comparison, or comparisons joined by AND only - fixes a variable to a literal
        (`EQ`) or excludes it (`NEQ`); the nested test compares the same variable with a literal that this rules out; no
        enabled command of the enclosing arm's body stores into the variable before the nested test - or anywhere in
        that body when a loop between the two can bring a later store round again. The nested arm never runs."""
        body_stores = collections.defaultdict(list)  # id(enclosing node) -> [(line, variable key)] stored in its body
        for ln_m, _, m, _ in self.lines:
            keys = set() if m.get('disabled') else {var_key(nm) for nm in self.stored_names(m)}
            anc_m = self.anc.get(id(m), ())
            for i, a in enumerate(anc_m):
                inner = anc_m[i + 1] if i + 1 < len(anc_m) else m
                if not (a.get('commandName') == 'if' and any(inner is b for b in a.get('branches', []))):  # an if's arms are not its body
                    body_stores[id(a)] += [(ln_m, k) for k in keys]
        out = []
        for ln, _, n, _ in self.lines:
            if n.get('disabled') or n.get('commandName') not in ('if', 'elseIf'):
                continue
            mine = equality_tests(n)
            if not mine:
                continue
            anc = self.anc.get(id(n), ())
            for i, a in enumerate(anc):
                inner = anc[i + 1] if i + 1 < len(anc) else n
                # an `if` holds for its body, not for its arms; an `elseIf` arm encloses its own body
                holds = (a.get('commandName') == 'if' and not any(inner is b for b in a.get('branches', []))) or a.get('commandName') == 'elseIf'
                if not holds or a.get('disabled'):
                    continue
                loop_between = any(x.get('commandName') == 'loop.commands.start' for x in anc[i + 1:])
                for key, op, value, case, text in equality_tests(a) or []:
                    if any(k == key and (loop_between or s < ln) for s, k in body_stores[id(a)]):
                        continue
                    for key2, op2, value2, case2, text2 in mine:
                        if key2 == key and rules_out((op, value, case), (op2, value2, case2)):
                            out.append((ln, text2, self.line_no[id(a)], text))
        return out

    def unassigned_reads(self):
        """The variables an enabled command reads that nothing gives a value: no default, not an input, and no
        enabled command stores into them under any spelling (`var_key`) - every read gets an empty value.
        [(name, lines, declared)]; None as `_var_uses`."""
        uses = self._var_uses()
        if uses is None:
            return None
        stores, reads = uses
        stored = {var_key(nm) for nm in stores}
        declared = collections.defaultdict(list)
        for nm, v in self.vars.items():
            declared[var_key(nm)].append(v)
        out = []
        for name in sorted(reads):
            key = var_key(name)
            given = any(v.get('input') or next(_value_leaves(v.get('defaultValue') or {}, ''), None) for v in declared[key])
            if key not in stored and not given:
                out.append((name, sorted(reads[name]), bool(declared[key])))
        return out

    def spellings(self):
        """The variables the bot writes in more than one spelling - one variable each (`var_key`). [(declared, uses)]:
        `declared` the declared spellings with their interface flags, `uses` (spelling, store lines, read lines) of
        every spelling no declaration carries - all spellings when none is declared. A spelling stored into while the
        declared variable is an input overwrites the value the caller passed. None as `_var_uses`."""
        uses = self._var_uses()
        if uses is None:
            return None
        stores, reads = uses
        groups = collections.defaultdict(set)
        for nm in list(self.vars) + list(stores) + list(reads):
            groups[var_key(nm)].add(nm)
        out = []
        for key in sorted(groups):
            names = groups[key]
            if len(names) < 2:
                continue
            declared = [(nm, [f for f in ('input', 'output') if self.vars[nm].get(f)]) for nm in sorted(names) if nm in self.vars]
            undeclared = sorted(nm for nm in names if nm not in self.vars) if declared else sorted(names)
            out.append((declared, [(nm, sorted(stores.get(nm, ())), sorted(reads.get(nm, ()))) for nm in undeclared]))
        return out

    @staticmethod
    def reads(n):
        return {name for o in own_values(n) if isinstance(o.get('expression'), str) for name in read_names(o['expression'])}

    def retry_loop(self, node, names):
        """The innermost loop around `node` that repeats on one of `names` (the values the call returns): a loop that
        runs more than once whose condition reads one, or that breaks or continues under a condition reading one.
        None when there is none, or when a try body inside that loop encloses the node and catches its exception."""
        anc = self.anc.get(id(node), ())
        for i in range(len(anc) - 1, -1, -1):
            a = anc[i]
            if a.get('commandName') == 'try' and (i + 1 == len(anc) or anc[i + 1] not in a.get('branches', [])):
                return None
            if a.get('commandName') != 'loop.commands.start':
                continue
            at = flat_attrs(a)
            times = at.get('times') or {}
            if (at.get('iterator') or {}).get('iteratorName') == 'loop.iterators.times' and times.get('number') == '1' and not times.get('expression'):
                continue
            if self.reads(a) & names:
                return a
            for _, _, m, _ in self.lines:
                if m.get('disabled') or m.get('commandName') not in ('loop.commands.break', 'loop.commands.continue'):
                    continue
                m_anc = self.anc.get(id(m), ())
                inner = [k for k, c in enumerate(m_anc) if c.get('commandName') == 'loop.commands.start']
                if inner and m_anc[inner[-1]] is a and any(c.get('commandName') in ('if', 'elseIf') and self.reads(c) & names
                                                          for c in m_anc[inner[-1] + 1:]):
                    return a
        return None

    def line_of(self, node):
        return self.line_no.get(id(node))

    def nodes(self, enabled_only=False):
        for ln, depth, n, is_branch in self.lines:
            if enabled_only and n.get('disabled'):
                continue
            yield ln, depth, n, is_branch

    def window(self, expr):
        """`$window-1$` -> the WINDOW variable's recorded title/class/executable."""
        m = re.fullmatch(r'\$([^$]+)\$', expr or '')
        v = self.vars.get(m.group(1)) if m else None
        w = ((v or {}).get('defaultValue') or {}).get('window') if v else None
        return window_record(w, m.group(1)) if w else None

    def window_value(self, v):
        """A WINDOW attribute value as the catalog records it: the variable's recorded window, or the literal window
        the command carries (a preset target such as the currently active window included), with the size the command
        resizes it to before acting (the Resize window option, vendor documentation)."""
        if not v:
            return None
        rec = self.window(v['expression']) if v.get('expression') else window_record(v['window']) if v.get('window') else None
        rs = v.get('windowResize') or {}
        if rec is not None and rs:
            rec = {**rec, 'resize': {k: channel(rs.get(k)) for k in ('width', 'height')}}
        return rec

    # ----- variables -----
    def var_groups(self):
        g = {'in': [], 'out': [], 'inout': [], 'const': [], 'local': []}
        for v in self.doc.get('variables', []):
            i, o = v.get('input'), v.get('output')
            k = 'inout' if i and o else 'in' if i else 'out' if o else 'const' if v.get('readOnly') else 'local'
            g[k].append(v['name'])
        return g

    def description(self):
        """The author's description: the first comment that starts with 'Description', else the first real comment."""
        first = None
        for _, _, n, _ in self.lines:
            if n.get('commandName') != 'Comment':
                continue
            t = (self._attr_string(n, 'comment') or '').strip()
            if not t or SEPARATOR.match(t):
                continue
            if t.lower().startswith('description'):
                return t.split(':', 1)[-1].strip()
            first = first or t
        return first

    @staticmethod
    def _attr_string(n, name):
        for a in n.get('attributes', []):
            if a.get('name') == name:
                v = a.get('value') or {}
                return v.get('expression') or v.get('string')
        return None


class Export:
    def __init__(self, root):
        self.given = root
        self.bots_dir = self._find_bots(root)
        self.root = os.path.dirname(self.bots_dir)
        self.bots = []
        self.data_files = []  # every non-bot file outside the recorder's metadata folders: configuration, templates, scripts, assemblies
        for dp, dns, fns in os.walk(self.bots_dir):
            dns[:] = [d for d in dns if not d.endswith('Metadata')]
            for f in sorted(fns):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, self.bots_dir).replace('\\', '/')
                if self._is_bot(p):
                    self.bots.append(Bot(self, p, rel))
                else:
                    self.data_files.append((rel, p))
        self.bots.sort(key=lambda b: b.path)
        self.by_path = {b.path: b for b in self.bots}
        self.by_path_ci = {b.path.lower(): b for b in self.bots}
        self.by_id = {b.id: b for b in self.bots}
        self.secrets = self._secrets()
        self.edges = [e for b in self.bots for e in self._edges(b)]
        self.called = {e['calleeId'] for e in self.edges if e['calleeId'] and not e['disabled']}
        self.called_any = {e['calleeId'] for e in self.edges if e['calleeId']}
        self.loggers = {b.id for b in self.bots if self._is_logger(b)}
        self.manifest = self._manifest()

    def _manifest(self):
        """A Control Room bot export ("Export bots") ships `manifest.json` beside the `Automation Anywhere/` tree: one entry
        per file with its content type, the dependencies the Control Room scanned (enabled Run Task targets) and the
        metadata folder each screenshot belongs to. Absent on Bot Migration output."""
        for d in (self.given, self.root, os.path.dirname(self.root), os.path.dirname(os.path.dirname(self.root))):
            p = os.path.join(os.path.abspath(d), 'manifest.json')
            if os.path.isfile(p):
                try:
                    with open(p, encoding='utf-8-sig') as fh:
                        m = json.load(fh)
                except (OSError, ValueError) as e:
                    return {'path': p, 'error': str(e)}
                files = m.get('files') or []
                deps = {}
                other_missing = []  # non-bot files the manifest lists (DLLs, configuration XML, scripts) that the package does not contain
                for f in files:
                    if f.get('contentType') == 'application/vnd.aa.taskbot':
                        deps[repo_path(f.get('path'))] = {'scanned': [repo_path(d) for d in f.get('scannedDependencies') or []],
                                                          'manual': [repo_path(d) for d in f.get('manualDependencies') or []],
                                                          'excluded': bool(f.get('excluded')), 'description': f.get('description') or '', 'author': f.get('author') or ''}
                    elif f.get('contentType') != 'image/png':
                        rel = repo_path(f.get('path'))
                        if not os.path.isfile(os.path.join(self.bots_dir, *rel.split('/'))):
                            other_missing.append(rel)
                return {'path': p, 'files': len(files), 'contentTypes': dict(collections.Counter(f.get('contentType') for f in files)),
                        'packages': m.get('packages') or [], 'bots': deps, 'otherFilesMissing': sorted(other_missing),
                        'tags': dict(collections.Counter(f"{t.get('namespace')}={t.get('value')}" for f in files for t in f.get('tags') or [])),
                        'raw': m}
        return None

    def _is_logger(self, bot):
        """A bot whose only effect is its log lines: log, flow and text commands, the log's own file or folder created,
        no sub-bot call, and no command storing into a result. A helper that copies files or hands a result
        back to its caller is a step, whatever commands it is made of - and a call taken for a log line hides it."""
        cmds = [(n.get('packageName'), n.get('commandName')) for _, _, n, _ in bot.lines if not n.get('disabled')]
        if not any(c == ('LogToFile', 'logToFile') for c in cmds):
            return False
        if any(c == ('TaskBot', 'runTask') for c in cmds):
            return False
        if not all(p in LOGGER_ALLOWED_PACKAGES and (p not in ('File', 'Folder') or c in LOGGER_FILE_COMMANDS) for p, c in cmds):
            return False
        # a result is an output only (not an input), or one a call binds back: migrated bots flag every variable in and
        # out, and a store into an in/out variable no caller receives is the bot's own working state
        uses = bot._var_uses()
        bound = {var_key(k) for e in self.edges if e['calleeId'] == bot.id and not e['disabled'] for k in e['outputs']}
        results = {var_key(nm) for nm, v in bot.vars.items() if v.get('output') and (not v.get('input') or var_key(nm) in bound)}
        return uses is not None and not any(var_key(nm) in results for nm in uses[0])

    @staticmethod
    def _find_bots(root):
        root = os.path.abspath(root)
        if os.path.basename(root).lower() == 'bots' and os.path.isdir(root):
            return root
        cand = os.path.join(root, 'Bots')
        if os.path.isdir(cand):
            return cand
        hits = []
        for dp, dns, _ in os.walk(root):
            if dp[len(root):].count(os.sep) > 3:
                dns[:] = []
                continue
            for d in dns:
                if d.lower() == 'bots':
                    hits.append(os.path.join(dp, d))
        if len(hits) == 1:
            return hits[0]
        sys.exit(f"{root}: expected an Automation 360 export with one Bots/ folder, found {len(hits)}")

    @staticmethod
    def _is_bot(p):
        if os.path.splitext(p)[1]:
            return False
        try:
            with open(p, 'rb') as fh:
                head = fh.read(4096)
            return head.lstrip(b'\xef\xbb\xbf').startswith(b'{') and b'"nodes"' in head or b'"packages"' in head
        except OSError:
            return False

    def shipped(self, ref):
        """The export's own copy of a file a command names (a DLL opened, a script run): by repository path, else by file
        name - a runner-local path (`$@AAApplicationPath$/Automation Anywhere/My Scripts/x.vbs`) names the file the
        export ships under its repository folder. None when the export holds no file of that name."""
        p = repo_path(ref).lower()
        name = os.path.basename(p)
        return next((rel for rel, _ in self.data_files if rel.lower() == p), None) or next(
            (rel for rel, _ in self.data_files if name and os.path.basename(rel).lower() == name), None)

    def resolve(self, ref):
        p = repo_path(ref)
        return self.by_path.get(p) or self.by_path_ci.get(p.lower())

    def _secrets(self):
        """Literal secret values: defaults of password-named variables and of variables the bot types CREDENTIAL (the
        source's own mark of a secret, whatever the name), literals typed into password fields, literal values of
        password-named attributes. Redacted wherever they appear afterwards, free text included."""
        out = set()

        def take(v):  # v: a literal channel or a matched literal, never an expression - a leading `$` is part of the secret
            if isinstance(v, str) and len(v) > 3 and v not in ('True', 'False', 'None'):
                out.add(v)
        for b in self.bots:
            for v in b.doc.get('variables', []):
                if SENSITIVE.search(v.get('name') or '') or v.get('type') == 'CREDENTIAL':
                    take(((v.get('defaultValue') or {}).get('string')))
            for _, _, n, _ in b.lines:
                attrs = flat_attrs(n)
                if n.get('commandName') == 'capture' and 'passwordtextAction' in attrs:
                    take((attrs.get('value') or {}).get('string'))
                for name, v in attrs.items():
                    if SENSITIVE.search(name or '') and not ENUM_ATTR.search(name or '') and v.get('type') != 'CREDENTIAL':
                        take(v.get('string'))
                    # a name/value entry list (a form field `client_secret`, a header): the entry's own name tells
                    for item in (v.get('list') or []) if isinstance(v.get('list'), list) else []:
                        d = {e.get('key'): e.get('value') or {} for e in (item or {}).get('dictionary') or []}
                        if SENSITIVE.search((d.get('name') or {}).get('string') or '') and (d.get('value') or {}).get('type') == 'STRING' \
                                and not (d.get('value') or {}).get('expression'):
                            take(d['value'].get('string'))
                # a literal assigned into a password-named variable (String.assign -> $strAppPassword$) is the
                # commonest way a secret ends up in a bot; the attribute is called sourceString, so only the target
                # tells. Only an assignment's literal is the assigned value: the delimiters and modes of a parsing
                # command that writes into a token variable (beforeAfter's `"access_token":"`, `EMPTY`) are its rule
                if n.get('commandName') == 'assign' and SENSITIVE.search((n.get('returnTo') or {}).get('variableName') or ''):
                    for name, v in attrs.items():
                        if v.get('type') == 'STRING' and not v.get('expression') and not ENUM_ATTR.search(name or ''):
                            take(v.get('string'))
                # literal tokens anywhere in the node (headers of a disabled test call, a message box echoing a token)
                blob = json.dumps(n.get('attributes') or [], ensure_ascii=False)
                for m in JWT_LITERAL.findall(blob):
                    take(m)
                for _, lit in BEARER_LITERAL.findall(blob):
                    take(lit)
        return out

    def _edges(self, bot):
        out = []
        for ln, _, n, _ in bot.lines:
            if n.get('commandName') != 'runTask':
                continue
            attrs = flat_attrs(n)
            tb = attrs.get('taskbot') or {}
            ref = (tb.get('taskbotFile') or {}).get('string') or (tb.get('taskbotFile') or {}).get('expression') or ''
            callee = self.resolve(ref)
            # a call's results: `taskbotOutput`, and the node's own `returnTo` - a map of callee output -> caller
            # variable, or one dictionary variable that receives every output (source guide § Value syntax, outputs)
            ret = n.get('returnTo') or {}
            outputs = {d['key']: d.get('value') for d in (tb.get('taskbotOutput') or {}).get('dictionary', [])}
            outputs.update({d['key']: d.get('value') for d in ret.get('dictionary') or [] if ret.get('type') == 'DICTIONARY'})
            out.append({'caller': bot.name, 'callerId': bot.id, 'line': ln, 'disabled': bool(n.get('disabled')),
                        'continueOnError': as_bool(attrs.get('continueOnError')),
                        'calleePath': repo_path(ref), 'callee': callee.name if callee else None, 'calleeId': callee.id if callee else None,
                        'inputs': {d['key']: d.get('value') for d in (tb.get('taskbotInput') or {}).get('dictionary', [])},
                        'outputs': outputs, 'outputsTo': ret.get('variableName') if ret.get('type') == 'VARIABLE' else None})
        return out

    # ----- rendering -----
    def V(self, v, bot, aname='', quote=True):
        """One attribute value as text. Expressions win over literals (they carry the variable names). A text - a
        literal, an expression, a file path - is printed in quotes as written (`lit`); `quote` False leaves it
        unquoted, as the data catalog records a path or a query."""
        if not v:
            return ''
        q = lit if quote else str
        t = v.get('type')
        if t in ('CREDENTIAL', 'LIST', 'DICTIONARY', 'TABLE', 'RECORD', 'REGION') and v.get('expression') not in (None, ''):
            # a container or credential held in a variable: the value is the expression, the literal fields are empty
            return q(self.safe(aname, v['expression']))
        if t == 'CREDENTIAL':
            c = v.get('credential') or {}
            return f"vault:{c.get('lockerName')}/{c.get('name')}.{c.get('attributeName')}"
        if t == 'WINDOW':
            w = bot.window_value(v)
            return (f"{v['expression']}=" if v.get('expression') else '') + (fmt_catalog_window(w) if w else '?')
        if t == 'SESSION':
            sn = v.get('sessionName') or {}
            # the session's scope: LOCAL to the bot, or GLOBAL - shared with the bots it calls
            scope = f" ({v['sessionTarget']})" if v.get('sessionTarget') not in (None, '', 'NONE') else ''
            return 'session:' + str(sn.get('string') or sn.get('expression') or v.get('expression') or '?') + scope
        if t == 'UIOBJECT':
            return self.fmt_uiobject(v, bot)
        if t == 'EXCEPTION':
            return str(v.get('exceptionName'))
        if t == 'LIST':
            return '[' + ', '.join(self.V(x, bot, aname) for x in v.get('list', [])) + ']'
        if t == 'TABLE':
            # a table literal (a variable default): the column names when the schema has any, then every row
            tb = v.get('table') or {}
            cols = [c.get('name') or '' for c in tb.get('schema') or []]
            rows = ['[' + ', '.join(self.V(c, bot, aname) for c in r.get('values') or []) + ']' for r in tb.get('rows') or []]
            return 'table' + (f"({', '.join(cols)})" if any(cols) else '') + '[' + ', '.join(rows) + ']'
        if t == 'RECORD':
            # a record literal (a variable default): each field's name with its value, in the schema's order
            rc = v.get('record') or {}
            fields = itertools.zip_longest([c.get('name') or '' for c in rc.get('schema') or []], rc.get('values') or [], fillvalue=None)
            return 'record{' + ', '.join(f"{name or ''}: {self.V(x, bot, name or '')}" for name, x in fields) + '}'
        if t == 'DICTIONARY':
            return '{' + ', '.join(f"{d.get('key')}: {self.V(d.get('value'), bot, d.get('key'))}" for d in v.get('dictionary', [])) + '}'
        if t == 'REGION':
            r = v.get('region') or {}
            return f"region({','.join(self.V(r.get(k), bot) for k in ('x', 'y', 'width', 'height'))})"
        if t == 'COORDINATE':  # a screen point in the command's window (a coordinate click); its recorder pictures are evidence
            c = v.get('coordinate') or {}
            return f"point({self.V(c.get('x'), bot)},{self.V(c.get('y'), bot)})"
        if t == 'VARIABLE':
            # a value a package computes (`packageName` set: the logging package's log file) reads `$Package:Name$`,
            # never as a bot variable of that name
            pkg = v.get('packageName')
            return f"${pkg}:{v.get('variableName')}$" if pkg else f"${v.get('variableName')}$"
        if t == 'TASKBOT':
            return self.fmt_taskbot(v, bot)
        if t == 'FILE':
            s = urllib.parse.unquote(v.get('expression') or v.get('string') or '')
            s = re.sub(r'^file://', '', s)
            return q(self.safe(aname, s))
        if v.get('expression') not in (None, ''):
            s = v['expression']
        elif t in ('NUMBER', 'BOOLEAN'):
            return channel(v)
        elif any(k in v for k in ('string', 'number', 'boolean')):
            s = self.safe(aname, channel(v), literal=True)
            if TOKEN_SHAPED.search(s):  # printed apart from an expression: the text is the literal, not the variable's value
                return 'literal' + lit(s)
            return q(s)
        else:
            return self.safe(aname, json_text(v))
        return q(self.safe(aname, s))

    def description(self, bot):
        """The bot's description as free text on one line (`prose`): every secret literal masked."""
        d = bot.description()
        return prose(self.safe('', d)) if d else d

    def safe(self, aname, s, literal=False):
        """`literal`: `s` comes from a literal channel, so a leading `$` is text, not a variable reference that spares it.
        An empty value is no secret: masked, a password left empty would read as one that is set."""
        if not isinstance(s, str) or s == '':
            return s
        if SENSITIVE.search(aname or '') and not ENUM_ATTR.search(aname or '') and (literal or not s.startswith('$')):
            return '***'
        for sec in self.secrets:
            if sec in s:
                s = s.replace(sec, '***')
        s = JWT_LITERAL.sub('***', s)
        s = BEARER_LITERAL.sub(lambda m: f"{m.group(1)} ***", s)
        return s

    def fmt_taskbot(self, v, bot, compact=False):
        """compact: bindings that pass the caller's same-named variable through (`X="$X$"`) are folded to a count - the
        real parameters (a literal, a differently named variable) stay visible (source guide, Call Graph Rules 5)."""
        ref = (v.get('taskbotFile') or {}).get('string') or (v.get('taskbotFile') or {}).get('expression') or ''
        callee = self.resolve(ref)
        head = f"`{callee.name}` ({callee.id})" if callee else f"UNRESOLVED `{repo_path(ref) or '?'}`"

        def bindings(d):
            items, passthrough = [], 0
            for e in (d or {}).get('dictionary', []):
                txt = self.V(e.get('value'), bot, e.get('key'))
                if compact and txt == lit(f"${e.get('key')}$"):
                    passthrough += 1
                    continue
                items.append(f"{e.get('key')}={txt}")
            if passthrough:
                items.append(f"+{passthrough} pass-through")
            return ', '.join(items)
        return f"{head} in{{{bindings(v.get('taskbotInput'))}}} out{{{bindings(v.get('taskbotOutput'))}}}"

    def fmt_returnto_dict(self, ret, bot, compact=False):
        """A node's returnTo map (callee output -> caller variable); compact folds same-named pairs."""
        items, passthrough = [], 0
        for d in ret.get('dictionary', []):
            txt = self.V(d.get('value'), bot, d.get('key'))
            if compact and txt == f"${d.get('key')}$":
                passthrough += 1
                continue
            items.append(f"{d.get('key')}: {txt}")
        if passthrough:
            items.append(f"+{passthrough} pass-through")
        return '{' + ', '.join(items) + '}'

    def fmt_stored_procedure(self, attrs, bot):
        """Database.store = run a stored procedure: name(@in=value, @out -> $variable$)."""
        name = self.V(attrs.get('query'), bot, 'query')
        params = []
        for e in (attrs.get('entryList') or {}).get('list', []):
            d = {x.get('key'): x.get('value') or {} for x in e.get('dictionary', [])}
            direction = (d.get('inOrOutParamter') or {}).get('string') or 'Input'
            pname = (d.get('parametername') or {}).get('string') or '?'
            val = self.V(d.get('parametervalue'), bot, pname)
            otype = channel(d.get('outputParamType'))
            params.append(f"{pname}[{direction}{':' + otype if otype else ''}]{'<->' if direction != 'Input' else '='}{val}")
        sess = attrs.get('session') or {}
        sess_txt = self.V(sess, bot, 'session') if sess.get('type') == 'SESSION' else (sess.get('string') or sess.get('expression') or 'Default')
        extra = ''
        if as_bool(attrs.get('doExport')):
            extra = f" export-to {self.V(attrs.get('filePath'), bot, 'filePath')}"
        return f"storedProcedure {name}({', '.join(params)}) session={sess_txt}{extra}"

    def stored_procedure_params(self, attrs, bot):
        out = []
        for e in (attrs.get('entryList') or {}).get('list', []):
            d = {x.get('key'): x.get('value') or {} for x in e.get('dictionary', [])}
            out.append({'name': (d.get('parametername') or {}).get('string'), 'direction': (d.get('inOrOutParamter') or {}).get('string') or 'Input',
                        'value': self.V(d.get('parametervalue'), bot, (d.get('parametername') or {}).get('string') or ''), 'type': (d.get('outputParamType') or {}).get('string')})
        return out

    def fmt_rest(self, attrs, bot):
        parts = [f"uri={self.V(attrs.get('uri'), bot, 'uri')}", f"auth={(attrs.get('authenticationMode') or {}).get('string')}"]
        for k in ('headerContentType',):
            if attrs.get(k):
                parts.append(f"contentType={(attrs[k].get('string') or attrs[k].get('expression'))}")
        for k, label in (('customHeaders', 'headers'), ('urlEncodedPostParameters', 'form'), ('queryParameters', 'query')):
            items = []
            for e in (attrs.get(k) or {}).get('list', []):
                d = {x.get('key'): x.get('value') or {} for x in e.get('dictionary', [])}
                off_entry = d.get('enabled') and d['enabled'].get('boolean') in (False, 'false', 'False')
                nm = (d.get('name') or {}).get('string') or '?'
                items.append(f"{'[off] ' if off_entry else ''}{nm}={self.V(d.get('value'), bot, nm)}")
            if items:
                parts.append(f"{label}[{', '.join(items)}]")
        for k in ('body', 'jsonBody', 'requestBody'):
            if attrs.get(k):
                parts.append(f"body={self.V(attrs[k], bot, 'body')}")
        return ' '.join(parts)

    def fmt_dll(self, attrs, bot):
        params = []
        for e in (attrs.get('dllParamEntryList') or {}).get('list', []):
            d = {x.get('key'): x.get('value') or {} for x in e.get('dictionary', [])}
            nm = (d.get('paramName') or {}).get('string') or '?'
            ptype = channel(d.get('paramType'))
            val = next((self.V(v, bot, nm) for k, v in d.items() if k.endswith('Value') and channel(v) != ''), None)
            if val is None:  # the value channel the parameter's type selects, even when empty
                val = next((self.V(v, bot, nm) for k, v in d.items() if k.endswith('Value')), '?')
            params.append(f"{nm}{':' + ptype if ptype else ''}={val}")
        bg = ' [background]' if as_bool(attrs.get('runFunctionInBackground')) else ''
        return f"{(attrs.get('nameSpace') or {}).get('string')}.{(attrs.get('className') or {}).get('string')}.{(attrs.get('functionName') or {}).get('string')}({', '.join(params)}){bg}"

    def capture_summary(self, v, bot):
        """UIOBJECT value -> normalized control description (also the row shape of the target catalog)."""
        uo = v.get('uiObject') or {}
        win_expr = (v.get('uiObjectWindow') or {}).get('expression')
        crit = {}
        for k, c in (uo.get('criteria') or {}).items():
            if c.get('enabled'):
                crit[k] = channel(c.get('value') or {})
        d = decode_blob(uo.get('blob'))
        on = d.get('objNode') or {}
        tech = on.get('technology') or {}
        shots = sorted({p for o in own_values(v) for k, p in o.items() if k.endswith('MetadataPath') and isinstance(p, str) and p.endswith('.png')})
        return {'technology': uo.get('technologyType'), 'controlType': uo.get('controlType'), 'browserType': uo.get('browserType') or None,
                'elevated': True if uo.get('isElevated') is True else None, 'window': bot.window_value(v.get('uiObjectWindow')) if win_expr else None,
                'windowExpr': win_expr, 'criteria': crit,
                'object': {'name': on.get('name') or None, 'role': on.get('role'), 'description': on.get('description') or None,
                           'className': on.get('className') or on.get('class') or None, 'id': on.get('id') or None, 'value': on.get('value') or None,
                           'windowTitle': on.get('windowTitle') or None, 'defaultAction': on.get('defaultAction') or None,
                           'states': on.get('states') or None, 'index': on.get('index'),
                           'path': [p.get('index') for p in ((on.get('path') or {}).get('objPath') or [])],
                           'bounds': {k: on.get(k) for k in ('left', 'top', 'width', 'height') if on.get(k) is not None},
                           'attributes': {a.get('name'): a.get('value') for a in on.get('attributes', []) if a.get('value') not in ('', None)},
                           'techType': tech.get('techType'), 'platformType': tech.get('platformType')},
                'parent': ((d.get('objParent') or {}).get('name') if isinstance(d.get('objParent'), dict) else None),
                'browserFramework': d.get('browserFramework'), 'captureVersion': d.get('captureVersion'),
                'searchCriteria': d.get('searchCriteria') or None, 'screenshots': shots, 'blobError': d.get('error'),
                'vision': vision_record(d)}

    def fmt_uiobject(self, v, bot):
        c = self.capture_summary(v, bot)
        w = c['window']
        wtxt = f"{c['windowExpr']}=" + (fmt_catalog_window(w) if w else '?') if c['windowExpr'] else ''
        crit = ', '.join(f"{k}={lit(self.safe(k, str(val)))}" for k, val in c['criteria'].items())
        name = c['object']['name'] or c['object']['description']
        return f"ui[{c['technology']}/{c['controlType']}] {wtxt} {{{crit}}}" + (f" obj={json_text(name)}" if name else '')

    def fmt_condition(self, a, bot):
        """One condition attribute. Automation 360 nests conditions two ways: several CONDITIONAL attributes on one `if`
        joined by its `operator` attribute (migrated bots), or one attribute carrying a `groupAttribute` (a parenthesised
        group) and `operatorAttribute` chains (`{operator, value, attributes, operatorAttribute}`) - native bots."""
        grouped = a.get('groupAttribute') is not None
        if grouped:
            s = '(' + self.fmt_condition(a['groupAttribute'], bot) + ')'
        else:
            s = self._fmt_single_condition(a, bot)
        chain = a.get('operatorAttribute')
        ops = set()
        while chain:
            nxt = ('(' + self.fmt_condition(chain['groupAttribute'], bot) + ')') if chain.get('groupAttribute') is not None else self._fmt_single_condition(chain, bot)
            grouped = grouped or chain.get('groupAttribute') is not None
            ops.add(chain.get('operator') or 'AND')
            s += f" {chain.get('operator') or 'AND'} {nxt}"
            chain = chain.get('operatorAttribute')
        if len(ops) > 1 and not grouped:
            # AND and OR in one chain with no parenthesised group: Automation 360 documents no precedence, so the
            # left-to-right rendering above is one reading of several (source guide § Expression Translation)
            s += ' [mixed AND/OR, precedence unstated]'
        return s

    def _fmt_single_condition(self, a, bot):
        v = a.get('value') or {}
        cname = v.get('conditionalName') or '?'
        sub = {x.get('name'): x.get('value') or {} for x in a.get('attributes', [])}
        r = lambda k: self.V(sub.get(k), bot, k)
        if cname in ('legacyConditionVariable', 'stringVariable', 'numberVariable', 'booleanVariable'):
            s = f"{r('variable')} {(sub.get('operator') or {}).get('string') or '?'} {r('value')}"
            # Case rule stated either way: `[matchCase]` / `[ignoreCase]`; no tag = the condition family states none
            # (legacyConditionVariable). `isIgnoreCarriage` true = line breaks ignored in the comparison.
            if sub.get('matchCase') is not None and channel(sub.get('matchCase')) != '':
                s += ' [matchCase]' if as_bool(sub.get('matchCase')) else ' [ignoreCase]'
            if as_bool(sub.get('isIgnoreCarriage')):
                s += ' [ignoreCarriage]'
            return s
        if cname == 'capture':
            aw = channel(sub.get('advancedWait'))
            return f"uiObjectExists({self.fmt_uiobject(sub.get('uiObject') or {}, bot)}, wait {r('wait')} s{', advancedWait ' + aw if aw else ''})"
        if cname == 'hasExtension':
            return f"hasExtension({r('sourceFilePath')} {(sub.get('operator') or {}).get('string')} {r('extensions')})"
        parts = [self.setting(k, val, bot) for k, val in sub.items()]
        return f"{cname}({', '.join(parts)})"

    def fmt_iterator(self, a, bot):
        v = a.get('value') or {}
        iname = (v.get('iteratorName') or '?').split('.')[-1]
        sub = ', '.join(self.setting(x.get('name'), x.get('value'), bot) for x in a.get('attributes', []))
        ret = a.get('returnTo') or {}
        if ret.get('type') == 'DICTIONARY':  # a table row's columns mapped to variables, named like the column or not
            rs = ' -> ' + self.fmt_returnto_dict(ret, bot)
        else:
            rs = f" -> ${ret.get('variableName')}$" if ret.get('variableName') else ''
        return f"for {iname}({sub}){rs}"

    def setting(self, nm, v, bot):
        """One setting as `name=value`. A case rule set as a literal is the tag a string condition carries -
        `[matchCase]` or `[ignoreCase]` - whichever command it sits on (a condition, a table search)."""
        v = v or {}
        if nm == 'matchCase' and v.get('expression') in (None, '') and channel(v) != '':
            return '[matchCase]' if as_bool(v) else '[ignoreCase]'
        return f"{nm}={self.V(v, bot, nm)}"

    def rest_attrs(self, n, bot, consumed):
        """Attributes a dedicated shape does not render itself, rendered generically after it - a curated formatter
        must never hide a setting (timeouts, TLS flags, options, a log's overwrite mode) the bot file holds."""
        items = []
        for a in n.get('attributes', []):
            nm = a.get('name')
            if not nm or nm in consumed:
                continue
            txt = self.setting(nm, a.get('value'), bot)
            sub = [s for s in a.get('attributes') or [] if s.get('name') not in consumed]
            if sub:
                txt += '(' + ', '.join(self.setting(s.get('name'), s.get('value'), bot) for s in sub) + ')'
            items.append(txt)
        return (' ' + ' '.join(items)) if items else ''

    def compact_line(self, n, bot, is_branch=False):
        """A node's `dump --compact` rendering (None = not printed). Folds only what the source guide names: a
        disabled node is its label (its subtree is folded by the caller), a keystroke-delay lookup is omitted, a log
        line leads with its message and target file, a delay with its time; every other setting of the node follows."""
        cmd = n.get('commandName')
        depth_label = cmd if is_branch or cmd in ('if', 'elseIf', 'try', 'throw') else f"{n.get('packageName')}.{cmd}"
        if n.get('disabled'):
            return self.render(n, bot, is_branch) if cmd == 'Comment' else f"[off] {depth_label}"
        if cmd == 'getKeystrokesDelay':
            return None
        if cmd == 'logToFile':
            # the target file stays visible: a log line into a business file (a per-reply list, a counter file that a
            # status mail later counts) is a data write, not the run's own logging - its overwrite mode with it
            la = flat_attrs(n)
            return f"log {self.V(la.get('logContent'), bot)} -> {self.V(la.get('filePath'), bot, 'filePath')}{self.rest_attrs(n, bot, {'logContent', 'filePath'})}"
        if cmd == 'delay':
            a = flat_attrs(n)
            return f"delay {self.V(a.get('delayTime'), bot)} {channel(a.get('timeUnit'))}{self.rest_attrs(n, bot, {'delayTime', 'timeUnit'})}"
        return self.render(n, bot, is_branch, compact=True)

    def render(self, n, bot, is_branch=False, compact=False):
        pk, cmd = n.get('packageName'), n.get('commandName')
        off = '[off] ' if n.get('disabled') else ''
        if cmd == 'Comment':
            t = (bot._attr_string(n, 'comment') or '').strip()
            if not t or SEPARATOR.match(t):
                return None
            return f"{off}# {prose(self.safe('', t))}"  # free text: a secret literal is masked here too
        attrs = flat_attrs(n)
        ret = n.get('returnTo') or {}
        ret_txt = ''
        if ret:
            ret_txt = ' -> ' + (f"${ret.get('variableName')}$" if ret.get('type') == 'VARIABLE'
                                else self.fmt_returnto_dict(ret, bot, compact) if ret.get('type') == 'DICTIONARY' else self.V(ret, bot))
        if n.get('returns'):
            ret_txt += ' -> ' + ', '.join(f"{k}=${(val or {}).get('variableName')}$" for k, val in n['returns'].items())
        rest_of = lambda consumed: self.rest_attrs(n, bot, consumed)
        # Commands with a dedicated shape
        if pk == 'Database' and cmd == 'store':
            return f"{off}Database.store {self.fmt_stored_procedure(attrs, bot)}{rest_of({'query', 'entryList', 'session', 'filePath'})}{ret_txt}"
        if pk == 'Rest' and cmd.startswith('rest'):
            return (f"{off}Rest.{cmd} {self.fmt_rest(attrs, bot)}"
                    f"{rest_of({'uri', 'authenticationMode', 'headerContentType', 'customHeaders', 'urlEncodedPostParameters', 'queryParameters', 'body', 'jsonBody', 'requestBody'})}{ret_txt}")
        if pk == 'DLL' and cmd.startswith('RunCSharpDLL'):
            return f"{off}DLL.{cmd} {self.fmt_dll(attrs, bot)}{rest_of({'nameSpace', 'className', 'functionName', 'dllParamEntryList', 'runFunctionInBackground'})}{ret_txt}"
        if compact and cmd == 'messageBox':
            return f"{off}MessageBox.messageBox {self.V(attrs.get('content'), bot, 'content')}{rest_of({'content'})}"
        if compact and cmd == 'runTask' and (attrs.get('taskbot') or {}).get('type') == 'TASKBOT':
            callee = self.resolve(((attrs['taskbot'].get('taskbotFile') or {}).get('string') or (attrs['taskbot'].get('taskbotFile') or {}).get('expression') or ''))
            if callee and callee.id in self.loggers:
                msg = next((self.V(d.get('value'), bot, d.get('key')) for d in (attrs['taskbot'].get('taskbotInput') or {}).get('dictionary', [])
                            if re.search(r'message|msg|text', d.get('key') or '', re.I)), '')
                return f"{off}log(`{callee.name}`) {msg}{ret_txt}"
        conds, others, op = [], [], None
        for a in n.get('attributes', []):
            v = a.get('value') or {}
            t = v.get('type')
            if t == 'CONDITIONAL' or a.get('groupAttribute') is not None:
                conds.append(self.fmt_condition(a, bot))
            elif a.get('name') == 'operator' and cmd in ('if', 'elseIf'):
                op = v.get('string')
            elif t == 'ITERATOR':
                others.append(self.fmt_iterator(a, bot))
            elif t == 'TASKBOT':
                others.append(self.fmt_taskbot(v, bot, compact))
            else:
                txt = self.setting(a.get('name'), v, bot)
                sub = a.get('attributes') or []
                if sub:  # options nested under a choice (`doExport` -> its file path, ...): part of the same setting
                    txt += '(' + ', '.join(self.setting(x.get('name'), x.get('value'), bot) for x in sub) + ')'
                others.append(txt)
        if pk == 'String' and cmd == 'beforeAfter':
            others.append(self.extracted_part(attrs, bot))
        elif cmd in ('loop.commands.break', 'loop.commands.continue'):
            others.append(bot.loop_jump(n))
        elif cmd == 'loop.commands.start':
            others.append(bot.loop_reading(n))
        text = ''
        if conds:
            text = f" {op or 'AND'} ".join(conds) if len(conds) > 1 else conds[0]
        others = [o for o in others if o]
        if others:
            text = (text + ' ' if text else '') + ' '.join(others)
        text += ret_txt
        label = cmd if is_branch or cmd in ('if', 'elseIf', 'try', 'throw') else f"{pk}.{cmd}"
        if cmd == 'loop.commands.start':
            label = 'loop'
        elif cmd in ('loop.commands.break', 'loop.commands.continue'):
            label = cmd.split('.')[-1]
        return f"{off}{label} {text}".rstrip()

    def extracted_part(self, attrs, bot):
        """The part an Extract text command keeps, as `[= text after "/"]`. `getCharacters` names where the delimiter
        stands, not the part kept (vendor documentation): `BEFORE` x keeps the text after x, `AFTER` x the text before
        it, `BEFOREAFTER` the text between its two delimiters - the line states the part so no reader takes the name."""
        def at(name, occurrence):
            n, d = channel(attrs.get(occurrence)), self.V(attrs.get(name), bot, name)
            return d if n in ('', '1') else f"occurrence {n} of {d}"
        how = channel(attrs.get('getCharacters'))
        if how == 'BEFORE':
            return f"[= text after {at('beforeString', 'beforeOccurrence')}]"
        if how == 'AFTER':
            return f"[= text before {at('afterString', 'afterOccurrence')}]"
        if how == 'BEFOREAFTER':  # the second delimiter's occurrence counts from the first delimiter
            after = at('afterStringInBeforeAfter', 'afterOccurrenceInBeforeAfter')
            return (f"[= text between {at('beforeStringInBeforeAfter', 'beforeOccurrenceInBeforeAfter')} and "
                    f"{after}{' after it' if after.startswith('occurrence ') else ''}]")
        return ''


# ----------------------------------------------------------------------------------------------------------------------
def find_bot(x: Export, which):
    """(bot, None) for a bot id, a repository path, or a bot name that occurs once; (None, message) otherwise. Bots are
    named alike across folders (a logon bot copied under each process), so a lookup by name that took the first match
    would dump one copy and hide the other - a repeated name lists the copies instead."""
    w = str(which).strip()
    if w.isdigit():
        hit = next((b for b in x.bots if b.id == int(w)), None)
        if hit:
            return hit, None
    hit = x.resolve(w)
    if hit:
        return hit, None
    hits = [b for b in x.bots if b.name == w] or [b for b in x.bots if b.name.lower() == w.lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"no bot with id, path or name {which!r}; bots: {py_text([f'{b.name} ({b.id})' for b in x.bots])}"
    return None, (f"bot name {which!r} occurs {len(hits)} times — dump it by id: "
                  + '; '.join(f"{b.id} {b.path} lines={len(b.lines)}" for b in hits))


def variable_line(x: Export, bot, v):
    """One entry of the VARIABLES legend: `$name$ TYPE/subtype [in] [out] [readOnly] default=<value>` - every default
    rendered from its value, a table or a list included (a header row kept in a table default is the data contract)."""
    dv = v.get('defaultValue') or {}
    flags = ' '.join(f for f, on in (('in', v.get('input')), ('out', v.get('output')), ('readOnly', v.get('readOnly'))) if on)
    typ = v.get('type') or '?'
    if v.get('subtype') not in (None, '', 'UNDEFINED'):
        typ += f"/{v['subtype']}"
    if v.get('sessionType'):  # the package a session variable's session belongs to (`Excel_MS`)
        typ += f"({v['sessionType']})"
    t = f"${v.get('name')}$ {typ}" + (f" [{flags}]" if flags else '')
    if any(s != '' for _, s in _value_leaves(dv, '')) or dv.get('type') == 'WINDOW':
        t += f" default={x.V(dv, bot, v.get('name') or '')}"
    if v.get('description'):
        t += f" description={lit(oneline(x.safe('', v['description'])))}"
    return t


def dump(x: Export, which, compact=False):
    """compact: a disabled node is one line with its subtree folded, a log line leads with its message, a delay with
    its time, keystroke-delay lookups are omitted - the logic stays, the plumbing goes (`Export.compact_line`)."""
    bot, err = find_bot(x, which)
    if err:
        return err
    L = []
    pr = L.append
    props = bot.doc.get('properties') or {}
    # run settings the bot carries: a run-time limit ("7200s"; "0s" = none), a child-session run, the number model
    run = ' '.join(f"{k}={props[k]}" for k in ('timeout', 'runInChildWindow', 'runInChildWindowMode', 'improvedNumberSupport') if props.get(k) not in (None, ''))
    pr(f"# `{bot.name}` ({bot.id})  path={bot.path}  priority={props.get('automationPriority')} botCodeVersion={props.get('botCodeVersion')} "
       f"migrationJournal={py_text(bot.doc.get('migrationJournalReviewIds') or [])} lines={len(bot.lines)} disabled={sum(1 for _, _, n, _ in bot.lines if n.get('disabled'))}"
       + (f" {run}" if run else '') + (' LOGGER-BOT (calls to it are log lines)' if bot.id in x.loggers else ''))
    pr(f"PACKAGES: {', '.join(f'{p.get('name')} {p.get('version')}' for p in bot.doc.get('packages', []))}")
    d = x.description(bot)
    if d:
        pr(f"DESCRIPTION: {d}")
    g = bot.var_groups()
    for k, label in (('in', 'INPUTS'), ('out', 'OUTPUTS'), ('inout', 'IN/OUT'), ('const', 'CONSTANTS')):
        if g[k]:
            pr(f"{label}: {', '.join(g[k])}")
    pr(f"LOCALS: {len(g['local'])} (every variable with its type and default: VARIABLES below)")
    pr('')
    # compact mode: depth of a disabled node whose subtree is being folded, and whether that node is an arm. A disabled
    # `if` or `try` folds its arms with its body; a disabled arm folds only its own body - the `elseIf` or `else` after
    # it is a sibling arm that runs
    fold_below, fold_arms = None, False
    folded = 0
    for ln, depth, n, is_branch in bot.lines:
        if fold_below is not None:
            if depth > fold_below or (depth == fold_below and is_branch and fold_arms):
                folded += 1
                continue
            if folded:
                L[-1] += f" (+{folded} lines folded)"
            fold_below, folded = None, 0
        t = x.compact_line(n, bot, is_branch) if compact else x.render(n, bot, is_branch)
        if t is None:
            continue
        pr(f"{ln:>4} {'  ' * depth}{t}")
        if compact and n.get('disabled') and n.get('commandName') != 'Comment':
            fold_below, fold_arms = depth, not is_branch
    if folded:
        L[-1] += f" (+{folded} lines folded)"
    variables = bot.doc.get('variables', [])
    if variables:
        L += ['', 'VARIABLES (every variable the bot declares: type, interface flags, default):']
        L += [f"  {variable_line(x, bot, v)}" for v in variables]
    return '\n'.join(L)


# ----------------------------------------------------------------------------------------------------------------------
# raw: the bot file's own structure, for checking a reading against the source rather than against a rendering. Every
# rendering above is this script's reading of the file; a finding that rests on one (a missing operator, an unset flag,
# a value never passed) is confirmed here, where no renderer chose what to show.
RAW_DEFAULTS = {('disabled', False), ('taskbotSelf', False), ('securelyRecorded', False), ('repository', False), ('sessionTarget', 'NONE'),
                ('operator', 'NONE')}
RAW_CHANNEL = {'STRING': 'string', 'NUMBER': 'number', 'BOOLEAN': 'boolean'}
RAW_MASKED = ('string', 'expression')  # the channels in which a value under a sensitive name is masked, as `Export.V` masks them


def raw_value(x: Export, o, aname=''):
    """`o` as the file holds it, every key kept. Dropped: empty values, the defaults every value repeats (RAW_DEFAULTS)
    and the recorder's blobs (their size stays). The empty channel a scalar's type names stays, so an empty literal reads
    as set to empty, never as unset. Strings are masked as every rendering masks them: a value under a sensitive
    attribute, variable or binding name (the nearest `name` or `key` above it), and every harvested secret, JWT and
    bearer literal anywhere."""
    if isinstance(o, list):
        return [raw_value(x, v, aname) for v in o]
    if isinstance(o, str):
        return x.safe('', o)
    if not isinstance(o, dict):
        return o
    inner = next((o[k] for k in ('name', 'key') if isinstance(o.get(k), str) and o[k]), aname)
    own = RAW_CHANNEL.get(o.get('type')) if o.get('expression') in (None, '') else None
    out = {}
    for k, v in o.items():
        if k == 'blob' and isinstance(v, str) and v:
            out[k] = f"<{len(v)} base64 characters, decoded by targets>"
        elif (v in ('', None, [], {}) and k != own) or (isinstance(v, (str, bool)) and (k, v) in RAW_DEFAULTS):
            continue
        elif isinstance(v, str):
            out[k] = x.safe(aname if k in RAW_MASKED else '', v, literal=k == 'string') if v else v
        else:
            out[k] = raw_value(x, v, inner)
    return out


def raw_lines(nodes):
    """(line, depth, node, own disabled, disabled by an ancestor, body span) in editor order, counted afresh from the
    file - the node, its children, then each branch (`else`, `elseIf`, `catch`, `finally`) and its children: the
    numbering `dump` uses, derived without `Bot._number`, which marks a disabled node's subtree in the loaded document."""
    out, span = [], {}

    def walk(ns, depth, off):
        for n in ns or []:
            own = bool(n.get('disabled'))
            out.append([len(out) + 1, depth, n, own, off])
            start = len(out) + 1
            walk(n.get('children'), depth + 1, off or own)
            if len(out) >= start:
                span[id(n)] = (start, len(out))
            for b in n.get('branches') or []:
                b_own = bool(b.get('disabled'))
                out.append([len(out) + 1, depth, b, b_own, off or own])
                start = len(out) + 1
                walk(b.get('children'), depth + 1, off or own or b_own)
                if len(out) >= start:
                    span[id(b)] = (start, len(out))
    walk(nodes, 0, False)
    return [(ln, depth, n, own, off, span.get(id(n))) for ln, depth, n, own, off in out]


def raw(x: Export, which, lines=None, find=None, var=None):
    """(text, exit code): the nodes of one bot in a line range or holding a text, and a variable's declaration, as the
    file holds them (`raw_value`). A targeted lookup - the lines a finding cites - never a reading of the whole bot."""
    bot, err = find_bot(x, which)
    if err:
        return err, 2
    with open(bot.abs, encoding='utf-8-sig') as fh:
        doc = json.load(fh)
    rows = raw_lines(doc.get('nodes'))
    line_of = {id(n): ln for ln, _, n, _, _, _ in rows}
    L = [f"# raw `{bot.name}` ({bot.id})  path={bot.path}  lines={len(rows)}  (the file's keys; empty values, repeated defaults "
         f"and blobs dropped; secrets masked; [off] = disabled here, [off by parent] = inside a disabled node)"]
    code = 0
    if var:
        hits = [v for v in doc.get('variables') or [] if v.get('name') == var]
        if hits:
            L += [f"VARIABLE {json.dumps(raw_value(x, v), ensure_ascii=False)}" for v in hits]
        else:
            L.append(f"no variable {var!r} in `{bot.name}`; variables: {', '.join(v.get('name') or '' for v in doc.get('variables') or [])}")
            code = 2
    shown = 0
    for ln, depth, n, own, off, body in rows if lines or find else []:
        if lines and not lines[0] <= ln <= lines[1]:
            continue
        cmd = f"{n.get('packageName')}.{n.get('commandName')}"
        text = json.dumps(raw_value(x, {k: v for k, v in n.items() if k not in ('children', 'branches', 'packageName', 'commandName')}),
                          ensure_ascii=False)
        if find and find not in f"{cmd} {text}":
            continue
        tail = ([f"body={body[0]}-{body[1]}"] if body else []) + \
               ([f"branches={','.join(f'{b.get('commandName')}@{line_of[id(b)]}' for b in n['branches'])}"] if n.get('branches') else [])
        state = '[off] ' if own else '[off by parent] ' if off else ''
        L.append(f"{ln:>4} {'  ' * depth}{state}{cmd} {text}" + (f"  {' '.join(tail)}" if tail else ''))
        shown += 1
    if (lines or find) and not shown:
        L.append(f"no line {'in ' + '-'.join(map(str, lines)) if lines else ''}{' ' if lines and find else ''}{'holding ' + repr(find) if find else ''}")
    return '\n'.join(L), code


def profile(x: Export):
    L = []
    pr = L.append
    all_nodes = [(b, ln, n) for b in x.bots for ln, _, n, _ in b.lines]
    enabled = [(b, ln, n) for b, ln, n in all_nodes if not n.get('disabled')]
    pr(f"## EXPORT {x.root}")
    pr(f"bots={len(x.bots)} lines={len(all_nodes)} disabled={len(all_nodes) - len(enabled)} data files={len(x.data_files)} "
       f"metadata folders={sum(1 for b in x.bots if os.path.isdir(b.metadata_dir))}")
    migrated = [f"{b.name} ({b.id})" for b in x.bots if b.doc.get('migrationJournalReviewIds')]
    pr(f"migration journal (Enterprise 11 -> Automation 360 Bot Migration): {len(migrated)} bots carry review ids: {py_text(migrated)}")
    if x.loggers:
        pr(f"logger bots (only log, flow and string commands, no file but the log's own, no result stored; calls to them are log lines, not steps): {py_text([f'{b.name} ({b.id})' for b in x.bots if b.id in x.loggers])}")
    pr("\n## MANIFEST (Control Room bot export: manifest.json beside the repository tree)")
    m = x.manifest
    if not m:
        pr("  none - the export is Bot Migration output or a copied repository tree; dependencies come from the bots' Run Task commands only")
    elif m.get('error'):
        pr(f"  {m['path']}: unreadable ({m['error']})")
    else:
        pr(f"  {m['path']}: {m['files']} files {py_text(m['contentTypes'])}; export-level packages: {len(m['packages'])}; "
           f"bots excluded from the export: {py_text([p for p, d in m['bots'].items() if d['excluded']])}; descriptions/authors given: "
           f"{sum(1 for d in m['bots'].values() if d['description'])}/{sum(1 for d in m['bots'].values() if d['author'])}")
        if m.get('tags'):
            pr(f"  file tags (namespace=value: files) - the platform the bots are built and meant to run on: {py_text(m['tags'])}")
        # Cross-check: the Control Room's scanned dependencies against the enabled Run Task edges the bots carry
        path_of = {b.id: b.path for b in x.bots}
        edges_by_caller = collections.defaultdict(set)
        for e in x.edges:
            if not e['disabled']:
                edges_by_caller[path_of[e['callerId']]].add(e['calleePath'].lower())
        missing_in_manifest, missing_in_bots, manual_only, file_deps, not_in_export = [], [], [], collections.Counter(), []
        for p, d in m['bots'].items():
            scanned, manual = {s.lower() for s in d['scanned']}, {s.lower() for s in d['manual']}
            mine = edges_by_caller.get(p, set())
            if p.lower() not in x.by_path_ci:
                not_in_export.append(p)
                continue
            for s in sorted(mine - scanned - manual):
                missing_in_manifest.append(f"{os.path.basename(p)} -> {os.path.basename(s)}")
            for s in sorted((scanned | manual) - mine):
                # a dependency is a bot when it names one; anything else (a DLL, a configuration or state file, a
                # script) is a repository file the bot references - a file dependency, not a stale call edge
                if s not in x.by_path_ci and os.path.splitext(s)[1]:
                    file_deps[os.path.basename(s)] += 1
                elif s in manual and s not in scanned:
                    manual_only.append(f"{os.path.basename(p)} -> {os.path.basename(s)}")
                else:
                    missing_in_bots.append(f"{os.path.basename(p)} -> {os.path.basename(s)}")
        listed = lambda v: py_text(v) if v else 'none'
        pr(f"  bots listed in the manifest but absent from the tree: {listed(not_in_export)}")
        pr(f"  enabled Run Task edges the manifest does not list: {listed(missing_in_manifest)}")
        pr(f"  scanned bot dependencies no enabled Run Task carries (disabled or stale): {listed(missing_in_bots)}")
        pr(f"  manual bot dependencies (added by the author; the bot does not call them directly): {listed(manual_only)}")
        pr(f"  file dependencies (repository files the bots reference - DLLs opened, configuration and state files read, "
           f"scripts run; file: bots): {listed(dict(sorted(file_deps.items())))}")
        pr(f"  non-bot files the manifest lists but the package does not contain (configuration files, DLLs, scripts the bots depend on; "
           f"their values are not in the export): {listed(m.get('otherFilesMissing'))}")
    pk = collections.defaultdict(set)
    for b in x.bots:
        for p in b.doc.get('packages', []):
            pk[p.get('name')].add(p.get('version'))
    pr("\n## PACKAGES (name: versions)")
    for k in sorted(pk):
        pr(f"  {k}: {py_text(sorted(pk[k]))}")
    pr("\n## FOLDERS (path: bots)")
    byf = collections.defaultdict(list)
    for b in x.bots:
        byf[b.folder].append(b)
    for f in sorted(byf):
        pr(f"  {f}: {py_text([b.name for b in byf[f]])}")
    pr("\n## FILES in the export besides the bots (configuration files, templates, scripts, assemblies: path (bytes))")
    for rel, p in x.data_files:
        pr(f"  {rel} ({os.path.getsize(p)} bytes)")
    pr("\n## COMMAND VOCABULARY (package.command: enabled / disabled)")
    voc_on = collections.Counter((n.get('packageName'), n.get('commandName')) for _, _, n in enabled)
    voc_off = collections.Counter((n.get('packageName'), n.get('commandName')) for _, _, n in all_nodes if n.get('disabled'))
    for (p, c), cnt in sorted(voc_on.items(), key=lambda kv: -kv[1]):
        pr(f"  {p}.{c}: {cnt} / {voc_off.get((p, c), 0)}")
    for (p, c), cnt in sorted(voc_off.items()):
        if (p, c) not in voc_on:
            pr(f"  {p}.{c}: 0 / {cnt}")
    pr("\n## BOTS (name (id) | folder | lines on/off | in | out | in/out | windows | recorder captures on/off | calls | screenshots | logs | message boxes waiting+closing on/off | description)")
    for b in x.bots:
        g = b.var_groups()
        caps = [n for _, _, n, _ in b.lines if n.get('commandName') == 'capture']
        pr(f"  `{b.name}` ({b.id}) | {b.folder} | {sum(1 for _, _, n, _ in b.lines if not n.get('disabled'))}/{sum(1 for _, _, n, _ in b.lines if n.get('disabled'))}"
           f" | in={len(g['in'])} out={len(g['out'])} inout={len(g['inout'])} | windows={sum(1 for v in b.doc.get('variables', []) if v.get('type') == 'WINDOW')}"
           f" | recorder captures={sum(1 for n in caps if not n.get('disabled'))}/{sum(1 for n in caps if n.get('disabled'))}"
           f" | calls={sum(1 for e in x.edges if e['callerId'] == b.id and not e['disabled'])}"
           f" | shots={screenshot_count(b)}"
           f" | logs={sum(1 for _, _, n, _ in b.lines if n.get('commandName') == 'logToFile' and not n.get('disabled'))}"
           f" | msgbox={msgbox_counts(b)}"
           f" | {x.description(b) or ''}")
    pr("\n## CALL GRAPH (caller -> callee: uses [disabled uses] {continueOnError uses})")
    # bots are named alike across folders (a logon bot under each process): every graph line names caller and callee
    # by name and id, never by name alone, or two bots' edges merge into one
    caller_of = lambda e: f"`{e['caller']}` ({e['callerId']})"
    callee_of = lambda e: f"`{e['callee']}` ({e['calleeId']})" if e['callee'] else f"UNRESOLVED {e['calleePath']}"
    agg = collections.defaultdict(lambda: [0, 0, 0])
    for e in x.edges:
        k = (caller_of(e), callee_of(e))
        agg[k][1 if e['disabled'] else 0] += 1
        if e['continueOnError'] and not e['disabled']:
            agg[k][2] += 1
    for (c, t), (on, off, coe) in sorted(agg.items()):
        pr(f"  {c} -> {t}: {on} [{off}] {{{coe}}}")
    roots = [b for b in x.bots if b.id not in x.called]
    leaves = [b for b in x.bots if not any(e['callerId'] == b.id and not e['disabled'] for e in x.edges)]
    pr(f"\n## ROOTS (never called by an enabled Run Task): {py_text([f'`{b.name}` ({b.id})' for b in roots])}")
    pr(f"## LEAVES (call nothing): {py_text([f'`{b.name}` ({b.id})' for b in leaves])}")
    pr(f"## UNRESOLVED callees: {py_text(sorted({e['calleePath'] for e in x.edges if not e['callee']}))}")
    live = {(caller_of(e), callee_of(e)) for e in x.edges if not e['disabled']}
    pr(f"## DEAD CALLS (disabled Run Task only): {py_text(sorted({(caller_of(e), callee_of(e)) for e in x.edges if e['disabled']} - live))}")
    pr("\n## CREDENTIAL VAULT REFERENCES (locker/credential.attribute: attribute of command, uses)")
    creds = collections.Counter()
    globs, sysv = collections.Counter(), collections.Counter()
    for b, ln, n in all_nodes:
        for a in n.get('attributes', []):
            s = json.dumps(a.get('value') or {})
            for m in GLOBAL_VALUE.findall(s):
                globs[m] += 1
            for m in SYSTEM_VAR.findall(s):
                sysv[m] += 1
            for c in re.findall(r'"credential":\s*(\{[^}]*\})', s):
                cd = json.loads(c)
                creds[(f"{cd.get('lockerName')}/{cd.get('name')}.{cd.get('attributeName')}", a.get('name'), n.get('commandName'), bool(n.get('disabled')))] += 1
    for (ref, an, cmd, off), cnt in sorted(creds.items()):
        pr(f"  {ref}: {an} of {cmd}{' [off]' if off else ''}, {cnt}")
    pr(f"\n## GLOBAL VALUES ($@name$): {py_text(dict(globs))}")
    pr(f"## SYSTEM VARIABLES ($System:name): {py_text(dict(sysv))}")
    pr("\n## LEGACY CREDENTIAL NOTES (migration comments naming the Enterprise 11 credential each action used)")
    for b, ln, n in all_nodes:
        if n.get('commandName') == 'Comment':
            t = b._attr_string(n, 'comment') or ''
            if 'legacy bot referred' in t:
                pr(f"  {at(b, ln)} {' '.join(py_text(re.findall(r'Credential name=([^,]+), attribute name=([^\s/]+)', t)).split())}")
    pr("\n## WINDOWS (title | class | executable [| title matched without regard to case]): bots")
    wins = collections.defaultdict(set)
    for b in x.bots:
        for v in b.doc.get('variables', []):
            if v.get('type') == 'WINDOW':
                w = b.window(f"${v['name']}$")
                if w:
                    wins[(w['title'], w['class'], w['exe'], bool(w.get('nameCaseInsensitive')))].add(f"{b.name} ({b.id})")
    for (t, c, e, nocase), bs in sorted(wins.items(), key=lambda kv: str(kv[0])):
        pr(f"  {json_text(t)} | {c} | {e}{' | nameCaseInsensitive' if nocase else ''}: {py_text(sorted(bs))}")
    pr("\n## UI CAPTURES (technology/control/action: enabled / disabled)")
    capc = collections.Counter()
    for b, ln, n in all_nodes:
        if n.get('commandName') != 'capture':
            continue
        attrs = flat_attrs(n)
        uo = (attrs.get('uiObject') or {}).get('uiObject') or {}
        act = capture_action(attrs)
        capc[(uo.get('technologyType'), uo.get('controlType'), act, bool(n.get('disabled')))] += 1
    for (t, c, a, off), cnt in sorted(capc.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), str(kv[0][2]), kv[0][3])):
        pr(f"  {t}/{c}/{a}: {'0 / ' + str(cnt) if off else str(cnt)}")
    pr("\n## APPLICATIONS, BROWSERS, SCRIPTS (command: attributes)")
    for b, ln, n in enabled + [(b, ln, n) for b, ln, n in all_nodes if n.get('disabled') and n.get('commandName') in ('openbrowser', 'runApp', 'RunScript')]:
        if n.get('commandName') in ('openbrowser', 'runApp', 'RunScript'):
            attrs = flat_attrs(n)
            script = channel(attrs.get('scriptPath') or attrs.get('filePath'))
            shipped = x.shipped(script) if n.get('commandName') == 'RunScript' or is_script_launch(attrs) else None
            tag = (f" [script in the export: {shipped}]" if shipped
                   else ' [script - not in the export]' if n.get('commandName') == 'runApp' and is_script_launch(attrs) else '')
            pr(f"  {at(b, ln)} {x.render(n, b)}{tag}")
    pr("\n## API CALLS, DLL FUNCTIONS (command: attributes; secrets are vault references)")
    for b, ln, n in all_nodes:
        if n.get('packageName') in ('Rest', 'DLL', 'Json') and n.get('commandName') not in ('Close', 'EndSession'):
            pr(f"  {at(b, ln)} {x.render(n, b)}")
    pr("\n## STORED PROCEDURES (name: callers) - Database.store = run a stored procedure on the connected database")
    procs = collections.defaultdict(list)
    for b, ln, n in all_nodes:
        if n.get('packageName') == 'Database' and n.get('commandName') == 'store':
            attrs = flat_attrs(n)
            procs[x.V(attrs.get('query'), b, 'query')].append(f"{at(b, ln)}{' [off]' if n.get('disabled') else ''}")
    for name in sorted(procs):
        pr(f"  {name}: {procs[name]}")
    pr("\n## EMAIL (command: attributes)")
    for b, ln, n in all_nodes:
        if n.get('packageName') == 'Email' and n.get('commandName') in ('emailConnect', *MAIL_SENDS, 'moveEmail', 'saveAttachment'):
            pr(f"  {at(b, ln)} {x.render(n, b)}")
        for a in n.get('attributes', []):
            if (a.get('value') or {}).get('type') == 'ITERATOR' and 'email' in ((a.get('value') or {}).get('iteratorName') or ''):
                pr(f"  {at(b, ln)} {x.render(n, b)}")
    pr("\n## DATABASE / EXCEL / XML / CSV / TEXT / PDF (command: attributes)")
    for b, ln, n in all_nodes:
        if n.get('packageName') in ('Database', 'Excel_MS', 'XML', 'CsvTxt', 'TextFile', 'PDF') and n.get('commandName') not in ('disconnect', 'CloseSpreadsheet', 'endSession', 'GoToCell', 'CloseCsvTxt', 'ReadFromCsvTxt'):
            pr(f"  {at(b, ln)} {x.render(n, b)}")
    pr("\n## FILES AND FOLDERS (enabled commands)")
    for b, ln, n in enabled:
        if n.get('packageName') in ('File', 'Folder'):
            pr(f"  {at(b, ln)} {x.render(n, b)}")
    pr("\n## EVIDENCE AND NOISE per bot (logToFile / Screen package screenshots / delay total s / message boxes waiting+closing on/off / comments / getKeystrokesDelay) - enabled only unless noted")
    for b in x.bots:
        c = collections.Counter(n.get('commandName') for _, _, n, _ in b.lines if not n.get('disabled'))
        delay_s = 0.0
        for _, _, n, _ in b.lines:
            if n.get('commandName') == 'delay' and not n.get('disabled'):
                a = {a.get('name'): a.get('value') or {} for a in n.get('attributes', [])}
                try:
                    v = float((a.get('delayTime') or {}).get('number'))
                    delay_s += v / 1000 if (a.get('timeUnit') or {}).get('string') == 'MILLISECONDS' else v
                except (TypeError, ValueError):
                    pass
        pr(f"  {b.name} ({b.id}): logs={c.get('logToFile', 0)} shots={screenshot_count(b)} delays={c.get('delay', 0)} (~{delay_s:.0f}s fixed) msgbox={msgbox_counts(b)} comments={c.get('Comment', 0)} keydelay={c.get('getKeystrokesDelay', 0)}")
    pr("\n## MIGRATION PLUMBING (variables the Bot Migration generated: m-*, M-*, Loop-*, Error-*)")
    for b in x.bots:
        gen = [v['name'] for v in b.doc.get('variables', []) if re.match(r'^(m-|M-|Loop-|Error-|window-|Prompt-Assignment|my-list-variable)', v['name'])]
        pr(f"  {b.name} ({b.id}): {len(gen)} of {len(b.doc.get('variables', []))} variables")
    pr("\n## VARIABLES READ BUT NEVER ASSIGNED (no default, not an input, no enabled command stores into them under any "
       "spelling: every read gets an empty value - a result the command does not store, a name or path nothing fills)")
    found = False
    for b in x.bots:
        u = b.unassigned_reads()
        if u is None:
            pr(f"  {b.name} ({b.id}): not checked - a table-row loop binds its columns to same-named variables")
        elif u:
            pr(f"  {b.name} ({b.id}): " + '; '.join(f"{name}{'' if decl else ' (undeclared)'} read at {lns}" for name, lns, decl in u))
        found = found or u is None or bool(u)
    if not found:
        pr("  none")
    pr("\n## INPUTS A CALL DOES NOT BIND (the callee reads the input, which has no default; the call binds no spelling of it, "
       "so the callee reads it empty - a compact dump folds the bindings it has, never the ones it lacks)")
    unbound = collections.defaultdict(list)  # (callee, inputs left unbound) -> the calls
    for e in x.edges:
        callee = x.by_id.get(e['calleeId'])
        if e['disabled'] or not callee or callee.id in x.loggers:
            continue
        uses = callee._var_uses()
        if uses is None:
            continue
        # an input the callee assigns itself is a working variable flagged in/out (migrated bots flag nearly all), not
        # a value it takes from the caller
        read, stored = {var_key(nm) for nm in uses[1]}, {var_key(nm) for nm in uses[0]}
        bound = {var_key(k) for k in e['inputs']}
        missing = [nm for nm, v in callee.vars.items() if v.get('input') and var_key(nm) in read - stored - bound
                   and not next(_value_leaves(v.get('defaultValue') or {}, ''), None)]
        if missing:
            unbound[(f"`{callee.name}` ({callee.id})", ', '.join(missing))].append(f"`{e['caller']}` ({e['callerId']}):{e['line']}")
    for (callee, missing), calls in sorted(unbound.items()):
        pr(f"  {callee}: {missing} — {len(calls)} call(s): {', '.join(calls)}")
    if not unbound:
        pr("  none")
    pr("\n## CATCH RESULTS A CALL DOES NOT RECEIVE (the callee's catch stores into an output - its error status, its reason - "
       "that the call binds back under no spelling: the caller never learns of that failure; a compact dump folds the results "
       "a call receives into a count)")
    lost, catches = collections.defaultdict(list), {}
    for e in x.edges:
        callee = x.by_id.get(e['calleeId'])
        if e['disabled'] or not callee or callee.id in x.loggers or e['outputsTo']:
            continue  # a call whose one dictionary variable receives every output misses none
        if callee.id not in catches:
            catches[callee.id] = callee.catch_results()
        received = {var_key(k) for k in e['outputs']}
        missing = [f"{nm} stored at {py_text(lns)}" for nm, lns in catches[callee.id] if var_key(nm) not in received]
        if missing:
            lost[(f"`{callee.name}` ({callee.id})", '; '.join(missing))].append(f"`{e['caller']}` ({e['callerId']}):{e['line']}")
    for (callee, missing), calls in sorted(lost.items()):
        pr(f"  {callee}: {missing} — {len(calls)} call(s): {', '.join(calls)}")
    if not lost:
        pr("  none")
    pr("\n## TESTS AN ENCLOSING TEST RULES OUT (an if or elseIf that cannot hold where it sits: an enclosing arm's string "
       "test, alone or joined by AND, fixes or excludes the value it compares, and no store into the variable can reach the "
       "nested test - the arm never runs)")
    found = False
    for b in x.bots:
        for ln, test, outer_ln, outer in b.ruled_out_tests():
            pr(f"  {b.name} ({b.id}): {ln} {test} - ruled out by {outer_ln} {outer}")
            found = True
    if not found:
        pr("  none")
    pr("\n## VARIABLE SPELLINGS (one variable written in several spellings: names ignore letter case and `_` = `-`, so "
       "every spelling reads and writes the declared variable; a spelling stored into an input overwrites what the caller passed)")
    found = False
    for b in x.bots:
        sp = b.spellings()
        if sp is None:
            pr(f"  {b.name} ({b.id}): not checked - a table-row loop binds its columns to same-named variables")
        for declared, uses in sp or []:
            target = ' and '.join(f"{nm}{' (' + ', '.join(flags) + ')' if flags else ''}" for nm, flags in declared) or 'no declaration'
            said = '; '.join(f"{nm}" + (f" stored at {st}" if st else '') + (f" read at {rd}" if rd else '') for nm, st, rd in uses)
            pr(f"  {b.name} ({b.id}): {said} - is {target}")
        found = found or sp is None or bool(sp)
    if not found:
        pr("  none")
    return '\n'.join(L)


def screenshot_count(b):
    """The bot's enabled Screen package screenshots."""
    return sum(1 for _, _, n, _ in b.lines if (n.get('packageName'), n.get('commandName')) in SCREENSHOTS and not n.get('disabled'))


def msgbox_counts(b):
    """A bot's message boxes as `<waiting>+<closing> on/<disabled> off`: an enabled box that does not close itself
    waits for a click and blocks an unattended run; one with its close-after option (`closeMsgBox`) closes after its
    time-out, a timed pause (source guide § Step vocabulary)."""
    boxes = [n for _, _, n, _ in b.lines if n.get('commandName') == 'messageBox']
    on = [n for n in boxes if not n.get('disabled')]
    closing = sum(1 for n in on if as_bool(flat_attrs(n).get('closeMsgBox')))
    return f"{len(on) - closing} waiting+{closing} closing on/{len(boxes) - len(on)} off"


CARD_RENDERED = ('conditions', 'loops', 'database', 'api/dll', 'keystrokes', 'apps')  # card items that carry the node's full line


def cards(x: Export):
    return '\n'.join(line for b in x.bots for line in bot_card(x, b)[0])


def real_bindings(x: Export, e, bot):
    """The bindings of a call that carry something other than the caller's same-named variable (Call Graph Rules 5)."""
    return [(k, x.V(v, bot, k)) for k, v in e['inputs'].items() if x.V(v, bot, k) != lit(f"${k}$")]


def real_results(x: Export, e, bot):
    """The results of a call stored into a variable named otherwise than the callee's output (`{vDateDiff: $DateDiff_any$}`):
    the same-named pairs are shared state, these are where the caller keeps what the call returns."""
    return [(k, x.V(v, bot, k)) for k, v in e['outputs'].items() if x.V(v, bot, k) not in (f"${k}$", lit(f"${k}$"))]


def bot_card(x: Export, b):
    """One bot's card lines, and - for `check` - the text the card renders per node it lists in full (by editor line)
    plus its real-parameters line. The card is an index: counts and line numbers for mail, workbook and file commands,
    the node's whole rendered line for conditions, loops, database, API / DLL, keystrokes and launches."""
    L, rendered = [], {}
    pr = L.append
    g = b.var_groups()
    on = [(ln, n) for ln, _, n, _ in b.lines if not n.get('disabled')]
    pr(f"### `{b.name}` ({b.id})  [{b.folder}] lines={len(b.lines)} enabled={len(on)} steps={sum(1 for _, n in on if (n.get('packageName'), n.get('commandName')) not in NOISE and n.get('commandName') not in BRANCH_COMMANDS)}"
       + ('  LOGGER-BOT' if b.id in x.loggers else ''))
    d = x.description(b)
    if d:
        pr(f"  description: {d}")
    pr(f"  inputs: {py_text(g['in'] + g['inout'])}")
    pr(f"  outputs: {py_text(g['out'] + g['inout'])}" if g['out'] or g['inout'] else "  outputs: []")
    # callers by the callee's id: two bots named alike (a logon bot kept under each process) have different callers
    pr(f"  callers: {py_text(sorted({f'`{e['caller']}` ({e['callerId']})' for e in x.edges if e['calleeId'] == b.id and not e['disabled']}))}")
    calls, log_calls = [], 0
    for e in x.edges:
        if e['callerId'] == b.id and not e['disabled']:
            if e['calleeId'] in x.loggers:
                log_calls += 1
                continue
            t = f"`{e['callee']}` ({e['calleeId']})" if e['callee'] else f"UNRESOLVED {e['calleePath']}"
            if not calls or calls[-1] != t:
                calls.append(t)
    pr(f"  calls (in order): {py_text(calls)}" + (f"  (+{log_calls} calls to logger bots)" if log_calls else ''))
    real_params = collections.OrderedDict()
    for e in x.edges:
        if e['callerId'] == b.id and not e['disabled'] and e['callee'] and e['calleeId'] not in x.loggers:
            for item in [f"{k}={txt}" for k, txt in real_bindings(x, e, b)] + [f"{k} -> {txt}" for k, txt in real_results(x, e, b)]:
                s = real_params.setdefault(f"`{e['callee']}` ({e['calleeId']})", [])
                if item not in s:
                    s.append(item)
    if real_params:
        rendered['real parameters'] = "  real parameters passed (non pass-through bindings): " + '; '.join(f"{c}: [{', '.join(v)}]" for c, v in real_params.items())
        pr(rendered['real parameters'])
    wins = []
    for v in b.doc.get('variables', []):
        if v.get('type') == 'WINDOW':
            w = b.window(f"${v['name']}$")
            if w:
                t = fmt_catalog_window(w)
                if t not in wins:
                    wins.append(t)
    if wins:
        pr(f"  windows: {'; '.join(wins)}")  # each already rendered: encoding it again would turn its \uXXXX into text
    caps = collections.Counter()
    for ln, n in on:
        if n.get('commandName') == 'capture':
            attrs = flat_attrs(n)
            uo = (attrs.get('uiObject') or {}).get('uiObject') or {}
            caps[f"{uo.get('technologyType')}/{uo.get('controlType')}/{capture_action(attrs)}"] += 1
    if caps:
        pr(f"  ui captures: {py_text(dict(caps))}")
    items = collections.OrderedDict((k, []) for k in ('conditions', 'loops', 'database', 'api/dll', 'keystrokes', 'apps', 'email', 'excel', 'files'))
    tries = throws = stops = 0
    for ln, n in on:
        cmd, pk = n.get('commandName'), n.get('packageName')
        kind = None
        if cmd in ('if', 'elseIf'):
            kind = 'conditions'
        elif cmd == 'loop.commands.start':
            kind = 'loops'
        elif cmd == 'try':
            tries += 1
        elif cmd == 'throw':
            throws += 1
        elif cmd == 'stopTask':
            stops += 1
        elif pk == 'Email' and cmd in (*MAIL_SENDS, 'emailConnect', 'moveEmail', 'saveAttachment'):
            items['email'].append(f"{ln}:{cmd}")
        elif pk == 'Database' and cmd in ('connect', 'sqlQuery', 'insertUpdateDelete', 'store', 'exportToDataTable'):
            kind = 'database'
        elif pk == 'Excel_MS':
            items['excel'].append(f"{ln}:{cmd}")
        elif pk in ('File', 'Folder') or (pk, cmd) in DATA_COMMANDS:
            items['files'].append(f"{ln}:{cmd}")
        elif cmd == 'Keystrokes':
            kind = 'keystrokes'
        elif cmd in ('runApp', 'openbrowser', 'RunScript'):
            kind = 'apps'
        elif pk in ('Rest', 'DLL') and cmd not in ('Open', 'Close'):
            kind = 'api/dll'
        if kind:
            rendered[ln] = f"{ln}:{x.render(n, b, cmd == 'elseIf')}"
            items[kind].append(rendered[ln])
    for label, its in items.items():  # one item per line, as rendered - never a list's repr, which re-escapes every backslash
        if its and label in CARD_RENDERED:
            pr(f"  {label} ({len(its)}):")
            for it in its:
                pr(f"    {it}")
        elif its:
            pr(f"  {label} ({len(its)}): {', '.join(its)}")
    pr(f"  error handling: try={tries} throw={throws} stopTask={stops} catch-returns={sum(1 for _, _, n, _ in b.lines if n.get('commandName') == 'catch' and n.get('returns'))}")
    rethrows = b.rethrow_lines()
    if rethrows:
        pr(f"  throw inside finally: {rethrows} - the bot can raise to its caller after its catch has run (and set any error flag it returns)")
    retried = []
    for e in x.edges:
        callee = x.by_id.get(e['calleeId'])
        if e['callerId'] != b.id or e['disabled'] or e['continueOnError'] or not callee or not callee.rethrow_lines():
            continue
        n = b.lines[e['line'] - 1][2]
        outs = {(v or {}).get('variableName') for v in e['outputs'].values()} - {None, ''}  # the output map and the node's returnTo map
        loop = b.retry_loop(n, outs)
        if loop:
            retried.append(f"{e['line']} `{e['callee']}` ({e['calleeId']}) in the loop at {b.line_of(loop)}")
    if retried:
        pr(f"  calls to bots with a throw inside finally, in a loop testing what they return: {py_text(retried)} - an exception from the callee leaves the loop; only a failure it returns reaches the loop's test")
    pr(f"  evidence: logs={sum(1 for _, n in on if n.get('commandName') == 'logToFile')} screenshots={screenshot_count(b)} "
       f"delays={sum(1 for _, n in on if n.get('commandName') == 'delay')} messageboxes={msgbox_counts(b)}")
    pr('')
    return L, rendered


def node_settings(x: Export, n, bot, skip=()):
    """Every attribute of a node as name -> rendered value (a nested option as `name/sub`), secrets masked. The catalog
    carries a command's settings by denylist, not allowlist: a list of known-interesting names drops the ones nobody
    listed - a keystroke command's delay between keys, a capture's advanced wait."""
    out = {}
    for a in n.get('attributes', []):
        nm = a.get('name')
        if not nm or nm in skip:
            continue
        v = a.get('value') or {}
        if v.get('type') == 'UIOBJECT':
            continue
        if v.get('type') != 'CONDITIONAL':  # a condition's settings are its sub-attributes
            out[nm] = x.V(v, bot, nm)
        for s in a.get('attributes') or []:
            if s.get('name') and (s.get('value') or {}).get('type') != 'UIOBJECT':
                out[f"{nm}/{s['name']}"] = x.V(s.get('value'), bot, s['name'])
    return out


def targets(x: Export):
    cat = []
    for b in x.bots:
        wins = []
        for v in b.doc.get('variables', []):
            if v.get('type') == 'WINDOW':
                w = b.window(f"${v['name']}$")
                if w:
                    wins.append(w)
        controls, keystrokes, window_ops, scripted = [], [], [], []
        for ln, _, n, _ in b.lines:
            cmd = n.get('commandName')
            attrs = flat_attrs(n)
            if cmd != 'capture':
                # the second identity channel: a scripting-model id (SAP `wnd[n]/…`, an XPath, a selector fragment) carried as a
                # literal or expression anywhere in the node - the argument of a script, DLL or package call, a variable default
                win = next((v.get('expression') for v in attrs.values() if v.get('type') == 'WINDOW' and v.get('expression')), None)
                for aname, literal in literal_strings(n.get('attributes', [])):
                    if SCRIPTED_TARGET.match(literal):
                        scripted.append({'line': ln, 'disabled': bool(n.get('disabled')), 'command': f"{n.get('packageName')}.{cmd}",
                                         'attribute': aname, 'target': x.safe(aname, literal), 'window': b.window(win) if win else None})
                    else:
                        for emb in EMBEDDED_SAP_ID.findall(literal):  # an id inside a script statement
                            scripted.append({'line': ln, 'disabled': bool(n.get('disabled')), 'command': f"{n.get('packageName')}.{cmd}",
                                             'attribute': f"{aname} (embedded in a script statement)", 'target': emb, 'window': b.window(win) if win else None})
            if cmd == 'capture' or any((a.get('value') or {}).get('conditionalName') == 'capture' for a in n.get('attributes', [])):
                uiv = attrs.get('uiObject')
                if not uiv:  # capture used as an If condition: the UIOBJECT sits under the condition attribute
                    for a in n.get('attributes', []):
                        for s in a.get('attributes', []):
                            if (s.get('value') or {}).get('type') == 'UIOBJECT':
                                uiv = s['value']
                if not uiv:
                    continue
                c = x.capture_summary(uiv, b)
                act = capture_action(attrs) or ('EXISTS' if cmd != 'capture' else None)
                val = attrs.get('value') or {}
                value = val.get('expression') or val.get('string')
                if 'passwordtextAction' in attrs and value and not str(value).startswith('$'):
                    value = '***'
                cred = (attrs.get('valueCV') or {}).get('credential')
                c.update({'bot': b.name, 'botId': b.id, 'line': ln, 'uid': n.get('uid'), 'disabled': bool(n.get('disabled')), 'action': act,
                          'value': x.safe('value', value) if value else None,
                          'credential': f"{cred.get('lockerName')}/{cred.get('name')}.{cred.get('attributeName')}" if cred else None,
                          'typeOfInput': (attrs.get('typeOfInput') or {}).get('string'), 'runInBackground': as_bool(attrs.get('runInBackground')),
                          'wait': channel(attrs.get('wait')) or None,
                          'returnTo': (n.get('returnTo') or {}).get('variableName'),
                          'settings': node_settings(x, n, b)})  # every setting the command carries, by name
                controls.append(c)
            elif cmd == 'Keystrokes':
                keystrokes.append({'line': ln, 'disabled': bool(n.get('disabled')), 'window': b.window_value(attrs.get('windowValue')),
                                   'keys': x.safe('keys', (attrs.get('stringToType') or {}).get('expression') or (attrs.get('stringToType') or {}).get('string')),
                                   'delay': channel(attrs.get('delay')) or None,  # milliseconds between keys
                                   'settings': node_settings(x, n, b)})
            elif n.get('packageName') in WINDOW_PACKAGES or cmd in ('windowExists', 'windowTitleExists'):
                wv = next((v for v in attrs.values() if v.get('type') == 'WINDOW'), None)
                window_ops.append({'line': ln, 'disabled': bool(n.get('disabled')), 'command': cmd, 'window': b.window_value(wv),
                                   'newTitle': (attrs.get('newTitle') or {}).get('expression') or (attrs.get('newTitle') or {}).get('string'),
                                   'settings': node_settings(x, n, b)})
        for v in b.doc.get('variables', []):  # a scripting id kept as a variable default is the same channel
            dv = v.get('defaultValue') or {}
            literal = dv.get('string') or dv.get('expression') or ''
            if isinstance(literal, str) and SCRIPTED_TARGET.match(literal):
                scripted.append({'line': None, 'disabled': False, 'command': 'variable default', 'attribute': v.get('name'),
                                 'target': x.safe(v.get('name'), literal), 'window': None})
        cat.append({'bot': b.name, 'botId': b.id, 'path': b.path, 'windows': wins, 'controls': controls, 'keystrokes': keystrokes, 'windowOps': window_ops,
                    'scriptedTargets': scripted,
                    'metadataFolder': os.path.relpath(b.metadata_dir, x.bots_dir).replace('\\', '/') if os.path.isdir(b.metadata_dir) else None})
    techs = collections.Counter(c['technology'] for e in cat for c in e['controls'])
    crit = collections.Counter(f"{c['technology']}:{k}" for e in cat for c in e['controls'] for k in c['criteria'])
    md = ["# Automation 360 UI target catalog\n",
          f"{len(cat)} bots, {sum(len(e['windows']) for e in cat)} window variables, {sum(len(e['controls']) for e in cat)} captured controls "
          f"({sum(1 for e in cat for c in e['controls'] if c['disabled'])} disabled), {sum(len(e['keystrokes']) for e in cat)} keystroke commands, "
          f"{sum(len(e['scriptedTargets']) for e in cat)} scripted targets (identities carried as literals outside recorder captures). "
          f"Technologies: {', '.join(f'{k} ({v})' for k, v in techs.most_common())}. Enabled criteria: {', '.join(f'{k} ({v})' for k, v in crit.most_common())}\n"]
    # a command's settings are renderings (`Export.V`), each printed as written: a JSON string around one re-escapes
    # the quotes it already carries
    settings_text = lambda s: '{' + ', '.join(f"{lit(k)}: {t}" for k, t in s.items()) + '}'
    for e in cat:
        if not (e['windows'] or e['controls'] or e['keystrokes'] or e['scriptedTargets']):
            continue
        md.append(f"\n## `{e['bot']}` ({e['botId']}) — {e['path']}\n")
        win_text = lambda w: fmt_catalog_window(w) if w else '?'  # noqa: E731
        if e['windows']:
            md.append("Windows: " + '; '.join(f"${w['variable']}$ = {win_text(w)}" for w in e['windows']))
        for c in e['controls']:
            o = c['object']
            md.append(f"- line {c['line']}{' [off]' if c['disabled'] else ''} {c['technology']}/{c['controlType']} **{c['action']}**"
                      + (f" value={json_text(c['value'])}" if c['value'] else '') + (f" credential={c['credential']}" if c['credential'] else '')
                      + f" — window {win_text(c['window'])}"
                      + f" — criteria {json_text(c['criteria'])}"
                      + (f" — recognised {json_text(c['vision'])}" if c.get('vision') else
                         f" — object name={json_text(o['name'])} role={o['role']} class={o['className']} id={o['id']} path={o['path']}")
                      + (f" parent={py_text(c['parent'])}" if c['parent'] else '') + (f" shots={py_text(c['screenshots'])}" if c['screenshots'] else '')
                      + f" — settings {settings_text(c['settings'])}")
        for k in e['keystrokes']:
            md.append(f"- line {k['line']}{' [off]' if k['disabled'] else ''} KEYSTROKES to {win_text(k['window'])}: {json_text(k['keys'])}"
                      + (f" — {k['delay']} ms between keys" if k['delay'] else '') + f" — settings {settings_text(k['settings'])}")
        for o in e['windowOps']:
            md.append(f"- line {o['line']}{' [off]' if o['disabled'] else ''} WINDOW {o['command']} {win_text(o['window'])}"
                      + f" — settings {settings_text(o['settings'])}")
        for t in e['scriptedTargets']:
            where = f"line {t['line']}" if t['line'] else t['command']
            md.append(f"- {where}{' [off]' if t['disabled'] else ''} SCRIPTED TARGET {t['command']} {t['attribute']}={json_text(t['target'])}"
                      + (f" — window {win_text(t['window'])}" if t['window'] else ''))
    return cat, '\n'.join(md)


def literal_strings(attributes):
    """(attribute name, text) for every STRING literal or expression inside an attribute list, descending into LIST,
    DICTIONARY, TASKBOT (the sub-bot call's input bindings), condition and iterator sub-attributes - the values a
    scripted UI identity can hide in."""
    for a in attributes or []:
        name = a.get('name') or a.get('key') or '?'
        v = a.get('value') or {}
        if isinstance(v, dict):
            for s in (v.get('expression'), v.get('string')):
                if isinstance(s, str) and s:
                    yield name, s
            for item in v.get('list') or []:
                yield from literal_strings([{'name': name, 'value': item}])
            for d in v.get('dictionary') or []:
                yield from literal_strings([{'name': f"{name}.{d.get('key')}", 'value': d.get('value')}])
            for k in ('taskbotInput', 'taskbotOutput'):  # a TASKBOT value nests two DICTIONARY values
                if isinstance(v.get(k), dict):
                    yield from literal_strings([{'name': f"{name}.{k}", 'value': v[k]}])
        yield from literal_strings(a.get('attributes') or [])


# A query's comments and string literals name no table; a table follows one of these keywords, each part of a dotted
# name bracketed, quoted or bare
SQL_NOISE = re.compile(r"--[^\n]*|/\*.*?\*/|'[^']*'", re.S)
SQL_PART = r'(?:\[[^\]]*\]|"[^"]*"|`[^`]*`|[^\s,;()\[\]"`.]+)'
SQL_TABLE = re.compile(rf'(?i)\b(?:from|join|into|update|table)\s+({SQL_PART}(?:\.{SQL_PART})*)')


def citable(s):
    """`s` as a step table cites it, `Name` (id) in a markdown cell: each run of line breaks, tabs and other spaces one
    space, other invisible characters dropped, `|` written `¦` and a backtick `'` - the cell's and the citation's own
    delimiters."""
    s = re.sub(r'\s+', ' ', s or '').strip()
    return ''.join(c for c in s if c == ' ' or unicodedata.category(c)[0] not in 'CZ').replace('|', '¦').replace('`', "'")


def query_tables(q):
    """The tables a query names after FROM, JOIN, INTO, UPDATE or TABLE, in order, once each, without brackets or
    quotes and a worksheet without its `$` (`[CONFIG$]`, `[Tracker$$]` in an expression -> `CONFIG`, `Tracker`)."""
    out = []
    for t in SQL_TABLE.findall(SQL_NOISE.sub(' ', q or '')):
        t = re.sub(r'[\[\]"`]', '', t)
        if not re.fullmatch(r'\$[^$]+\$', t):  # a table held in a variable keeps the token's closing `$`
            t = re.sub(r'\$\$?$', '', t)
        if t and t not in out:
            out.append(t)
    return out


def query_label(q, tables):
    """What a query does, for its name: its verb and the tables it names (`SELECT CONFIG`), or the variable that holds
    it (`$vQuery$`)."""
    m = re.match(r'\s*(\$[^$\s]+\$|[A-Za-z]+)', SQL_NOISE.sub(' ', q or ''))
    head = (m.group(1) if m.group(1).startswith('$') else m.group(1).upper()) if m else 'query'
    named = ', '.join(tables[:3]) + (f" +{len(tables) - 3}" if len(tables) > 3 else '')
    return f"{head} {named}" if tables else head


def data(x: Export):
    pd = []
    for b in x.bots:
        calls = []
        for e in x.edges:
            if e['callerId'] != b.id:
                continue
            calls.append({'line': e['line'], 'calleeId': e['calleeId'], 'callee': e['callee'], 'calleePath': e['calleePath'], 'disabled': e['disabled'],
                          'continueOnError': e['continueOnError'], 'recordset': None,
                          'inputs': {k: x.V(v, b, k) for k, v in e['inputs'].items()}, 'outputs': {k: x.V(v, b, k) for k, v in e['outputs'].items()},
                          'outputsTo': f"${e['outputsTo']}$" if e['outputsTo'] else None})
        g = b.var_groups()
        pd.append({'id': b.id, 'name': b.name, 'path': b.path, 'folder': b.folder, 'recordset': None, 'loggerBot': b.id in x.loggers,
                   'priority': (b.doc.get('properties') or {}).get('automationPriority'), 'migrationJournalReviewIds': b.doc.get('migrationJournalReviewIds') or [],
                   'inputs': g['in'] + g['inout'], 'outputs': g['out'] + g['inout'],
                   'screenshots': screenshot_count(b),
                   'logs': sum(1 for _, _, n, _ in b.lines if n.get('commandName') == 'logToFile' and not n.get('disabled')),
                   'calls': calls})
    # Data sources: what the bots read and write outside the UI. Automation 360 has no recordsets; these are the
    # counterpart a step table's Data sets cell cites, by name and id.
    src = collections.OrderedDict()
    first = {}  # key -> the details of the use that created the entry

    def add(kind, text, bot, ln, **extra):
        """One use of a data source. Uses of one source share its identity (kind and the text that identifies it: a
        path, a URL, a procedure, an account) but not their details - a second mail through the same account has its
        own subject and attachments, a second read of a workbook its own sheet: a use whose details differ from the
        entry's is kept under `perUse`, never dropped. The source is named by its text; a text a step table cannot cite
        as written is named `citable` and kept whole under `text`."""
        key = f"{kind}|{text}"
        u = at(bot, ln) if isinstance(bot, Bot) else f"{bot}:{ln}"  # bots named alike are told apart by id
        if key not in src:
            name = citable(text)
            src[key] = {'id': stable_id(key), 'name': name, 'kind': kind, **({'text': text} if name != text else {}), 'usedBy': [], **extra}
            first[key] = extra
        elif extra != first[key]:
            src[key].setdefault('perUse', {})[u] = extra
        if u not in src[key]['usedBy']:
            src[key]['usedBy'].append(u)
    for rel, p in x.data_files:
        entry = {'inExport': True, 'path': rel}
        if rel.lower().endswith('.xml'):
            try:
                with open(p, 'rb') as fh:
                    raw = fh.read(1_000_000)  # config files are small; a DTD or entity declaration is not a config file
                if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
                    entry['error'] = 'skipped: document carries a DTD or entity declaration'
                else:
                    entry['values'] = {el.tag: (el.text or '').strip() for el in ET.fromstring(raw)}
            except ET.ParseError as e:
                entry['error'] = str(e)
        if rel.lower().endswith('.dll'):  # named as the `DLL.Open` that loads it names it, so both are one source
            add('dll-file', 'repository:///Automation Anywhere/Bots/' + rel, '-', 0, **entry)
        else:
            add('config-file', os.path.basename(rel), '-', 0, **entry)
    for b in x.bots:
        sess_url = {}
        for ln, _, n, _ in b.lines:
            if n.get('disabled'):
                continue
            cmd, pk = n.get('commandName'), n.get('packageName')
            attrs = flat_attrs(n)
            def r(k):
                """An attribute's value as plain text: a string, a file or a value held in a variable, unquoted; a session
                or window reference in its own form."""
                v = attrs.get(k) or {}
                held = v.get('type') not in ('UIOBJECT', 'WINDOW', 'NUMBER', 'BOOLEAN') and v.get('expression') not in (None, '')
                return x.V(v, b, k, quote=not (v.get('type') in ('STRING', 'FILE') or held))
            def session_of(v):
                """A session is named by a STRING attribute (`session="X"`), a SESSION value, or the connect's returnTo."""
                v = v or {}
                if v.get('type') == 'SESSION':
                    sn = v.get('sessionName') or {}
                    return sn.get('string') or sn.get('expression') or v.get('expression') or 'Default'
                return v.get('string') or v.get('expression') or 'Default'
            if pk == 'Excel_MS' and cmd in ('OpenSpreadsheet', 'CreateSpreadsheet'):
                add('workbook', r('filePath'), b, ln, mode=channel(attrs.get('fileAccessMode')), sheet=channel(attrs.get('sheetName')))
            elif pk == 'Database' and cmd == 'connect':
                url = r('connectionURL')
                sess = ((n.get('returnTo') or {}).get('sessionName') or {}).get('string') or session_of(attrs.get('session'))
                sess_url[sess] = url
                oledb = 'oledb' in str(url).lower() or 'excel' in str(url).lower()
                add('database-connection', url, b, ln, session=sess, kindHint='workbook via OLEDB' if oledb else 'database server (connection string from configuration)')
            elif pk == 'Database' and cmd in ('sqlQuery', 'insertUpdateDelete', 'exportToDataTable'):
                q = r('query')
                sess = session_of(attrs.get('session'))
                tables = query_tables(q)
                add('database-connection', sess_url.get(sess, f"session {sess}"), b, ln)
                # a query is one command at one line, identified by path and line (bots named alike each hold their own
                # query at their own line) and named by what it does and where it runs - copies of a bot share its name
                # and lines, so the bot is named with its id; its SQL, long, over several lines and repeated across
                # bots, is its `query`
                key = f"query|{b.path}:{ln}"
                src[key] = {'id': stable_id(key), 'name': citable(f"{query_label(q, tables)} at {b.name} ({b.id}):{ln}"), 'kind': 'query', 'query': q,
                            'session': sess, 'connection': sess_url.get(sess), 'tables': tables, 'usedBy': [at(b, ln)],
                            'export': as_bool(attrs.get('doExport')), 'exportTo': r('filePath') if attrs.get('filePath') else None,
                            'toTable': (n.get('returnTo') or {}).get('variableName')}
            elif pk == 'Database' and cmd == 'store':
                name = r('query')
                sess = session_of(attrs.get('session'))
                params = x.stored_procedure_params(attrs, b)
                add('stored-procedure', name, b, ln, connection=sess_url.get(sess), parameters=[f"{p['direction'][:2].lower()} {p['name']}" for p in params],
                    outputs=[p['name'] for p in params if p['direction'] != 'Input'],
                    exportTo=r('filePath') if as_bool(attrs.get('doExport')) and attrs.get('filePath') else None)
            elif pk == 'Rest' and cmd.startswith('rest'):
                uri = r('uri')
                headers = [((({e.get('key'): e.get('value') or {} for e in d.get('dictionary', [])}).get('name') or {}).get('string')) for d in (attrs.get('customHeaders') or {}).get('list', [])]
                form = [((({e.get('key'): e.get('value') or {} for e in d.get('dictionary', [])}).get('name') or {}).get('string')) for d in (attrs.get('urlEncodedPostParameters') or {}).get('list', [])]
                creds = sorted(set(re.findall(r'vault:[^\s,\]}]+', x.fmt_rest(attrs, b))))
                add('api-endpoint', f"{cmd[4:].upper()} {uri}", b, ln, auth=channel(attrs.get('authenticationMode')), headers=headers, form=form,
                    credentials=creds, responseTo=x.fmt_returnto_dict(n.get('returnTo') or {}, b) if (n.get('returnTo') or {}).get('type') == 'DICTIONARY' else (n.get('returnTo') or {}).get('variableName'))
            elif pk == 'DLL' and cmd == 'Open':
                shipped = x.shipped(r('file'))
                add('dll-file', r('file'), b, ln, **({'inExport': True, 'path': shipped} if shipped else {'inExport': False}))
            elif pk == 'DLL' and cmd.startswith('RunCSharpDLL'):
                add('dll-function', x.fmt_dll(attrs, b).split('(')[0], b, ln, call=x.fmt_dll(attrs, b), resultTo=(n.get('returnTo') or {}).get('variableName'))
            elif (pk, cmd) in DATA_COMMANDS:
                # a download is named by what it fetches, and its file is where it saves it
                path_attr = next((k for k in (('url',) if cmd == 'downloadFile' else ('filePath', 'outputFile', 'url')) if attrs.get(k)), None)
                add(DATA_COMMANDS[(pk, cmd)], r(path_attr) if path_attr else '?', b, ln, command=cmd,
                    **({'saveTo': r('filePath')} if cmd == 'downloadFile' and attrs.get('filePath') else {}),
                    **({'textTo': r('outputFile')} if cmd == 'extractText' and attrs.get('outputFile') else {}))
            elif pk == 'Credential' and cmd in ('assign', 'assignToCV'):
                cv = 'credentialVariable' if attrs.get('credentialVariable') else 'stringVar'
                add('credential', x.V(attrs.get(cv), b, cv), b, ln, command=f"{pk}.{cmd}", toVariable=(n.get('returnTo') or {}).get('variableName'))
            elif pk == 'Email' and cmd in MAIL_SENDS:
                acct = attrs.get('ewsUsernameInteractive') or attrs.get('username') or attrs.get('fromAddress')
                files = next((k for k in ('attachmentsFilePath', 'fileList') if attrs.get(k)), None)
                add('mailbox-send', f"{channel(attrs.get('serverType'))} from {x.V(acct, b) if acct else '?'}", b, ln,
                    to=r('toAddress') if attrs.get('toAddress') else None, cc=r('cc') if attrs.get('cc') else None,
                    subject=r('subject') if attrs.get('subject') else None, attachments=r(files) if files else None)
            elif cmd == 'runApp' and is_script_launch(attrs):
                add('script', f"{r('filePath')} {r('parameters') if attrs.get('parameters') else ''}".strip(), b, ln, inExport=False)
            elif pk == 'XML' and cmd == 'startSession':
                add('xml-file', r('filePath'), b, ln)
            elif pk == 'XML' and cmd == 'getSingleNodeV2':
                add('xml-node', r('xPath'), b, ln)
            elif cmd == 'RunScript':
                add('script', r('scriptPath'), b, ln)
            elif cmd == 'saveAttachment':
                add('folder', r('folderPath'), b, ln, role='attachment download')
            elif cmd == 'moveEmail':
                add('mailbox-folder', f"{r('sourceFolder')} -> {r('destinationfolderPath')}", b, ln, filter={'readStatus': channel(attrs.get('readStatus')), 'subject': r('subject'), 'from': r('from')})
            elif cmd == 'emailConnect':
                add('mailbox', f"{channel(attrs.get('serverType'))} {channel(attrs.get('protocol')) or channel(attrs.get('exchangeService')) or ''} {r('serverHost') if attrs.get('serverHost') else ''}".strip(),
                    b, ln, account=x.V(attrs.get('ewsUsernameInteractive') or attrs.get('username'), b), auth=channel(attrs.get('authType')), session=r('session'))
            elif cmd == 'logToFile':
                add('log-file', r('filePath'), b, ln)
            elif (pk, cmd) in SCREENSHOTS:
                add('screenshot-file', r('filePath'), b, ln)
            elif pk in ('File', 'Folder') and cmd in ('createFolder', 'copyFiles', 'deleteFiles', 'renameFiles', 'createFile'):
                for k in ('folderPath', 'sourceFilePath', 'destinationPath', 'filePath'):
                    if attrs.get(k):
                        add('file-operation', f"{cmd} {k}={r(k)}", b, ln)
            elif set(attrs) & (RECIPIENT_ATTRS | MAILBOX_ATTRS | REMOTE_STORE_ATTRS):
                # a command of a package this script has no rule for (a house mail or document-store package) that
                # names recipients, a mailbox or a remote store: recognised by what its attributes address
                shown = {k: r(k) for k in sorted(set(attrs) & (RECIPIENT_ATTRS | MAILBOX_ATTRS | REMOTE_STORE_ATTRS | MAIL_DETAIL_ATTRS))}
                for kind, keys in (('mailbox-send', RECIPIENT_ATTRS), ('mailbox', MAILBOX_ATTRS), ('remote-store', REMOTE_STORE_ATTRS)):
                    if set(attrs) & keys:
                        break
                where = ' '.join(f"{k}={shown[k] or '(default)'}" for k in sorted(set(attrs) & keys))
                add(kind, f"{pk}.{cmd} {where}", b, ln, command=f"{pk}.{cmd}", attributes=shown)
            for a in n.get('attributes', []):
                v = a.get('value') or {}
                if v.get('type') == 'ITERATOR' and 'email' in (v.get('iteratorName') or ''):
                    sub = {s.get('name'): s.get('value') or {} for s in a.get('attributes', [])}
                    # the folder by its value channel: a folder held in a variable is its expression, not "?"
                    add('mailbox-folder', f"read {channel(sub.get('folder')) or '?'} ({channel(sub.get('readStatus'))})", b, ln,
                        filter={'from': x.V(sub.get('from'), b), 'subject': x.V(sub.get('subject'), b)})
                if v.get('type') == 'ITERATOR' and ('files' in (v.get('iteratorName') or '') or 'folder' in (v.get('iteratorName') or '')):
                    sub = {s.get('name'): s.get('value') or {} for s in a.get('attributes', [])}
                    add('folder', x.V(sub.get('folderPath'), b, quote=False) if sub.get('folderPath') else '?', b, ln,
                        role='file loop' if 'files' in v.get('iteratorName') else 'subfolder loop')
                if v.get('type') == 'ITERATOR' and 'resultset' in (v.get('iteratorName') or ''):
                    sub = {s.get('name'): s.get('value') or {} for s in a.get('attributes', [])}
                    add('database-connection', sess_url.get(session_of(sub.get('session')), f"session {session_of(sub.get('session'))}"), b, ln, role='result-set loop')
    rs = list(src.values())
    md = ["# Automation 360 data sources (secrets redacted)\n", f"{len(rs)} sources across {len(x.bots)} bots. Kinds: {dict(collections.Counter(s['kind'] for s in rs))}\n"]
    for kind in sorted({s['kind'] for s in rs}):
        md.append(f"\n## {kind}\n")
        for s in rs:
            if s['kind'] != kind:
                continue
            extra = {k: v for k, v in s.items() if k not in ('id', 'name', 'kind', 'usedBy')}
            md.append(f"- ({s['id']}) `{visible(s['name'])}` — used by {', '.join(s['usedBy'])}"
                      + (f" — {json_text(extra)}" if extra else ''))
    return pd, rs, '\n'.join(md)


SCALAR_TYPES = ('STRING', 'NUMBER', 'BOOLEAN', 'FILE', 'DATETIME')


def _value_leaves(v, path):
    """(path, text) for every non-empty scalar a value carries, recursing into lists, dictionaries and sub-bot bindings."""
    if not isinstance(v, dict):
        return
    t = v.get('type')
    if t == 'WINDOW':  # the size a command resizes its window to, whether the window is a variable or a literal
        for k in ('width', 'height'):
            yield from _value_leaves((v.get('windowResize') or {}).get(k), f"{path}.windowResize.{k}")
    if t not in SCALAR_TYPES and t != 'UIOBJECT' and v.get('expression') not in (None, ''):
        yield path, v['expression']  # a list, table, credential or window held in a variable
        return
    if t == 'CREDENTIAL':
        c = v.get('credential') or {}
        for k in ('lockerName', 'name', 'attributeName'):
            if c.get(k):
                yield f"{path}.{k}", str(c[k])
        return
    if t == 'SESSION':
        yield from _value_leaves(v.get('sessionName'), path + '.sessionName')
        if v.get('sessionTarget') not in (None, '', 'NONE'):
            yield path + '.sessionTarget', v['sessionTarget']
        return
    if t == 'WINDOW':
        w = v.get('window') or {}
        if w.get('name'):
            yield path + '.name', w['name']
        if w.get('presetType') not in (None, '', 'NONE'):
            yield path + '.presetType', w['presetType']
        if w.get('nameCaseInsensitive') is True:
            yield path + '.nameCaseInsensitive', 'nameCaseInsensitive'
        rx = w.get('nameRegex') or {}
        for c in rx.get('list') or []:
            for k in ('expression', 'string'):
                if (c or {}).get(k) not in (None, ''):
                    yield path + '.nameRegex', str(c[k])
        for k, on in (rx.get('regexFlags') or {}).items():
            if on is True:
                yield path + '.regexFlags', k
        if w.get('windowType') not in (None, '', 'NATIVE'):
            yield path + '.windowType', w['windowType']
        if w.get('browserTabUrl'):
            yield path + '.browserTabUrl', w['browserTabUrl']
        return
    if t == 'COORDINATE':
        for k in ('x', 'y'):
            yield from _value_leaves((v.get('coordinate') or {}).get(k), f"{path}.coordinate.{k}")
        return
    if t == 'RECORD':
        rc = v.get('record') or {}
        for c in rc.get('schema') or []:
            if c.get('name'):
                yield path + '.schema', c['name']
        for c in rc.get('values') or []:
            yield from _value_leaves(c, path + '.record')
        return
    if t == 'TABLE':
        tb = v.get('table') or {}
        for c in tb.get('schema') or []:
            if c.get('name'):
                yield path + '.schema', c['name']
        for r in tb.get('rows') or []:
            for c in r.get('values') or []:
                yield from _value_leaves(c, path + '[][]')
        return
    if t in SCALAR_TYPES:
        # every non-empty channel, read here rather than through `channel`: a renderer that picks the wrong channel
        # would otherwise lose the value from the rendering and from these expectations alike
        for k in ('expression', 'string', 'number', 'boolean'):
            if v.get(k) not in (None, ''):
                yield path, str(v[k]).lower() if k == 'boolean' else str(v[k])
    elif t == 'VARIABLE' and v.get('variableName'):
        # a package value must render with its package: the bare name would read as a bot variable
        yield path, f"{v['packageName']}:{v['variableName']}" if v.get('packageName') else v['variableName']
    elif t == 'LIST':
        for item in v.get('list') or []:
            yield from _value_leaves(item, path + '[]')
    elif t == 'DICTIONARY':
        for d in v.get('dictionary') or []:
            yield path + '.key', d.get('key') or ''
            yield from _value_leaves(d.get('value'), f"{path}.{d.get('key')}")
    elif t == 'TASKBOT':
        for side in ('taskbotInput', 'taskbotOutput'):
            for d in (v.get(side) or {}).get('dictionary') or []:
                yield f"{path}.{side}.key", d.get('key') or ''
                yield from _value_leaves(d.get('value'), f"{path}.{side}.{d.get('key')}")


def _attribute_leaves(a, path=''):
    p = f"{path}{a.get('name') or ''}"
    yield from _value_leaves(a.get('value'), p)
    if a.get('returnTo'):  # an attribute's own result: a loop iterator's variable, or its column-to-variable map
        yield from _value_leaves(a['returnTo'], p + '.returnTo')
    for sub in a.get('attributes') or []:
        yield from _attribute_leaves(sub, p + '/')
    for k in ('groupAttribute', 'operatorAttribute'):
        if a.get(k):
            yield from _attribute_leaves(a[k], p + '/')


def _shaped_literals(a):
    """The `string` literals under an attribute whose text has a variable's shape (`$name$`): each must be printed as a
    literal, since a reader takes a bare `$name$` for the variable's value."""
    def walk(v):
        if not isinstance(v, dict):
            return
        if v.get('type') == 'STRING' and v.get('expression') in (None, '') and isinstance(v.get('string'), str) and TOKEN_SHAPED.search(v['string']):
            yield v['string']
        for item in v.get('list') or []:
            yield from walk(item)
        for d in v.get('dictionary') or []:
            yield from walk(d.get('value'))
        for side in ('taskbotInput', 'taskbotOutput'):
            for d in (v.get(side) or {}).get('dictionary') or []:
                yield from walk(d.get('value'))
    yield from walk(a.get('value'))
    for sub in a.get('attributes') or []:
        yield from _shaped_literals(sub)
    for k in ('groupAttribute', 'operatorAttribute'):
        if a.get(k):
            yield from _shaped_literals(a[k])


# ----------------------------------------------------------------------------------------------------------------------
# Schema coverage. The renderers read the structures they know; a key a new product version adds, or one no corpus
# has exercised yet, would be skipped in silence. Every key path at which a bot document or the manifest holds a
# value must therefore be documented in the pack's schema.md, with what it means and whether a rendering shows it.
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'schema.md')
# (type of an object, key) -> type of the object(s) under the key: paths restart at each typed object, so a value
# nested in a list, a dictionary, a table or a sub-bot binding has the same path as a value anywhere else
SCHEMA_TYPES = {('DOC', 'nodes'): 'NODE', ('NODE', 'children'): 'NODE', ('NODE', 'branches'): 'NODE', ('NODE', 'attributes'): 'ATTRIBUTE',
                ('ATTRIBUTE', 'attributes'): 'ATTRIBUTE', ('ATTRIBUTE', 'groupAttribute'): 'ATTRIBUTE', ('ATTRIBUTE', 'operatorAttribute'): 'ATTRIBUTE',
                ('ATTRIBUTE', 'value'): 'VALUE', ('ATTRIBUTE', 'returnTo'): 'VALUE', ('NODE', 'returnTo'): 'VALUE', ('DOC', 'variables'): 'VARIABLE',
                ('VARIABLE', 'defaultValue'): 'VALUE', ('VALUE', 'list'): 'VALUE', ('VALUE', 'uiObjectWindow'): 'VALUE', ('VALUE', 'taskbotFile'): 'VALUE',
                ('VALUE', 'taskbotInput'): 'VALUE', ('VALUE', 'taskbotOutput'): 'VALUE', ('VALUE', 'sessionName'): 'VALUE',
                ('VALUE', 'dictionary'): 'DICTENTRY', ('DICTENTRY', 'value'): 'VALUE', ('CRITERION', 'value'): 'VALUE'}
SCHEMA_MAPS = {('NODE', 'returns'): 'VALUE', ('VALUE.uiObject', 'criteria'): 'CRITERION', ('VALUE', 'region'): 'VALUE',
               ('VALUE', 'coordinate'): 'VALUE'}  # keys are data, not schema


def schema_paths(o, t):
    """(typed key path, value) for every non-empty scalar under `o`, an object of type `t`; list items as `[]`. The
    capture blob is one value (decoded into the target catalog); a table's cells are VALUEs."""
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{t}.{k}"
            if (t, k) in SCHEMA_TYPES:
                for item in (v if isinstance(v, list) else [v]):
                    yield from schema_paths(item, SCHEMA_TYPES[(t, k)])
            elif (t, k) in SCHEMA_MAPS and isinstance(v, dict):
                for kk, item in v.items():
                    # a region's or a point's recorder capture is its own object beside the x, y (width, height) values
                    yield from schema_paths(item, f"{p}.{kk}" if kk == 'capture' and (t, k) in (('VALUE', 'region'), ('VALUE', 'coordinate'))
                                            else SCHEMA_MAPS[(t, k)])
            elif t == 'VALUE' and k == 'table' and isinstance(v, dict):
                for c in v.get('schema') or []:
                    yield from schema_paths(c, 'VALUE.table.schema[]')
                for r in v.get('rows') or []:
                    for c in r.get('values') or []:
                        yield from schema_paths(c, 'VALUE')
            elif t == 'VALUE' and k == 'record' and isinstance(v, dict):
                for c in v.get('schema') or []:
                    yield from schema_paths(c, 'VALUE.record.schema[]')
                for c in v.get('values') or []:
                    yield from schema_paths(c, 'VALUE')
            elif k == 'blob':
                if v:
                    yield p, '<blob>'
            elif isinstance(v, (dict, list)):
                yield from schema_paths(v, p + ('[]' if isinstance(v, list) else ''))
            elif v not in ('', None):
                yield p, v
    elif isinstance(o, list):
        for item in o:
            yield from schema_paths(item, t)
    elif o not in ('', None):
        yield t, o


def schema_rows(path=SCHEMA_FILE):
    """The documented key paths as [(glob pattern, status, allowed values or None)], in table order, from every table
    row of schema.md whose first cell holds backticked paths. `ignored while `a` `b` - …` allows only those values."""
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip())[1:-1]]
        if len(cells) < 3 or not cells[0].startswith('`'):
            continue
        status = cells[-1]
        m = re.match(r'ignored while ((?:`[^`]*`\s*)+)', status)
        allowed = {a.lower() for a in re.findall(r'`([^`]*)`', m.group(1))} if m else None
        for pat in re.findall(r'`([^`]+)`', cells[0]):
            rows.append((pat, status.split(' ')[0], allowed))
    return rows


def schema_status(rows, path):
    """The status of the first row whose pattern matches the path - only `*` is special (paths hold `[]`)."""
    for pat, status, allowed in rows:
        if re.fullmatch('.*'.join(re.escape(part) for part in pat.split('*')), path):
            return status, allowed
    return None, None


def data_recorded(pk, cmd, attrs):
    """The attributes whose values the data catalog records for a data-bearing command - each must reach the source
    entry whole (a query cut at a fixed length, a recipient list dropped, is a data catalog that misreads the bot)."""
    fixed = {('Database', 'sqlQuery'): ('query',), ('Database', 'insertUpdateDelete'): ('query',), ('Database', 'exportToDataTable'): ('query',),
             ('Database', 'store'): ('query',), ('Database', 'connect'): ('connectionURL',), ('DLL', 'Open'): ('file',),
             ('Excel_MS', 'OpenSpreadsheet'): ('filePath', 'sheetName'), ('Excel_MS', 'CreateSpreadsheet'): ('filePath', 'sheetName'),
             ('XML', 'startSession'): ('filePath',), ('XML', 'getSingleNodeV2'): ('xPath',), ('LegacyAutomation', 'RunScript'): ('scriptPath',),
             ('Email', 'saveAttachment'): ('folderPath',), ('Email', 'moveEmail'): ('sourceFolder', 'destinationfolderPath', 'subject', 'from'),
             ('Email', 'emailConnect'): ('serverHost', 'ewsUsernameInteractive', 'username'),
             ('LogToFile', 'logToFile'): ('filePath',), ('Credential', 'assign'): ('credentialVariable',), ('Credential', 'assignToCV'): ('stringVar',),
             **{shot: ('filePath',) for shot in SCREENSHOTS}}
    if (pk, cmd) in fixed:
        return fixed[(pk, cmd)]
    if pk == 'Rest' and cmd.startswith('rest'):
        return ('uri',)
    if (pk, cmd) in DATA_COMMANDS:
        return ('filePath', 'outputFile', 'url')
    if pk == 'Email' and cmd in MAIL_SENDS:
        return ('toAddress', 'cc', 'subject', 'attachmentsFilePath', 'fileList')
    if cmd == 'runApp' and is_script_launch(attrs):
        return ('filePath', 'parameters')
    if pk in ('File', 'Folder') and cmd in ('createFolder', 'copyFiles', 'deleteFiles', 'renameFiles', 'createFile'):
        return ('folderPath', 'sourceFilePath', 'destinationPath', 'filePath')
    if set(attrs) & (RECIPIENT_ATTRS | MAILBOX_ATTRS | REMOTE_STORE_ATTRS):
        return tuple(sorted(set(attrs) & (RECIPIENT_ATTRS | MAILBOX_ATTRS | REMOTE_STORE_ATTRS | MAIL_DETAIL_ATTRS)))
    return ()


def _flat(o):
    """Every key and scalar of a catalog entry as text, one per line - what a reader of the JSON sees, unescaped."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for k, val in v.items():
                out.append(str(k))
                walk(val)
        elif isinstance(v, list):
            for val in v:
                walk(val)
        elif v is not None:
            out.append(str(v).lower() if isinstance(v, bool) else str(v))
    walk(o)
    return '\n'.join(out)


FREE_TEXT = ('comment', 'description')  # the leaves the renderings print as free text, line breaks as ` / `
JSON_SURFACES = ('targets', 'data')  # the JSON catalogs, read back decoded


def _present(x, s, text, leaf='', as_json=False):
    """Is value `s` in `text` - as the text renderings write it (invisible characters as \\uXXXX, the line breaks of
    free text as ` / `), URL-decoded, or masked as a secret? `as_json`: `text` is a JSON catalog read back decoded,
    which holds a text as written too. Never re-escaped: a value found only repr- or JSON-escaped is a miss, since the
    reader copies the escapes. A candidate of four characters or fewer must stand alone, so that `1` is not found
    inside a line number."""
    path = urllib.parse.unquote(re.sub(r'^file://', '', s))  # a FILE value renders without its scheme
    forms = {s, path}
    if leaf in FREE_TEXT:  # as written or trimmed: a comment prints trimmed
        forms |= {t.replace('\r\n', ' / ').replace('\n', ' / ') for t in (s, s.strip())}
    forms |= {x.safe(leaf, c) for c in list(forms)} | {x.safe(leaf, c, literal=True) for c in list(forms)} | {x.safe('', c) for c in list(forms)}
    cands = {visible(c) for c in forms} | (forms if as_json else set())
    for c in cands:
        if not c:
            continue
        if len(c) <= 4:
            if re.search(r'(?<![A-Za-z0-9_])' + re.escape(c) + r'(?![A-Za-z0-9_])', text):
                return True
        elif c in text:
            return True
    return False


def check(x: Export):
    """Completeness self-test of every rendering the extractor and the executor read. Every non-empty scalar a node's
    attributes carry - the channel value of a STRING / NUMBER / BOOLEAN / FILE / DATETIME value, a variable name, a
    dictionary key, a list or table item, a nested option, a condition operand, a sub-bot binding - must appear as the
    text renderings write it (never repr- or JSON-escaped; `_present`) or masked as a secret, in:
      - the node's `dump` line, and its `dump --compact` line when the node is enabled (the compact fold of a
        same-named pass-through binding and of a logger bot's non-message bindings excepted, as documented), a
        capture's enabled criteria included; a line labelled with a package-qualified name carries the node's own
        command;
      - the bot's VARIABLES legend: every variable's type, interface flags, default and description;
      - its `cards` item when the card lists the node in full (conditions, loops, database, API / DLL, keystrokes,
        launches), every real binding of a sub-bot call on the card's real-parameters line, every caller and callee,
        every window variable's title on the card's windows line;
      - its `targets` entry: a capture's enabled criteria and every other setting, a keystroke command's and a window
        command's every setting, every window variable's title, class, executable and case flag;
      - its `data` entries: every binding of a sub-bot call in the bot inventory, and the recorded attributes of a
        data-bearing command in the data source that names the node, in the JSON and in its `.md` line.
    Every data source's name is citable in a step table (no `|`, backtick or invisible character) and names no other
    source of its kind. The JSON catalogs are read back decoded: JSON's own escapes never reach the test, a renderer's
    own do. The bot inventory's call list of each bot must equal the Run Task nodes of that bot's own tree (bots named
    alike must not share edges). A miss is a rendering defect: a reading of that value taken from the rendering is suspect. Returns
    the report text and the miss count."""
    misses = collections.defaultdict(list)
    total = collections.Counter()

    def need(surface, key, s, text, where, leaf=''):
        total[surface] += 1
        if not _present(x, s, text, leaf, surface in JSON_SURFACES):  # the example masked like any rendering: a report is read and pasted too
            misses[(surface,) + key].append(f"{where} = {lit(x.safe(leaf, x.safe('', s), literal=True)[:60])}")

    def node_leaves(n, tags=True, skip=None, outputs=False):
        """(path, leaf name, value) of the node's attributes - and, with `outputs`, of its `returnTo` and `returns`
        (what the dump prints after `->`); `tags`: the rendering states matchCase and the rarely set flags as tags
        (dump, cards) rather than as raw values (catalog settings); `skip(path, value)` drops a documented fold."""
        leaves = [lf for a in n.get('attributes', []) if (a.get('value') or {}).get('type') != 'UIOBJECT' for lf in _attribute_leaves(a)]
        if outputs:
            leaves += list(_value_leaves(n.get('returnTo') or {}, 'returnTo'))
            for k, val in (n.get('returns') or {}).items():
                leaves += [('returns.key', k)] + list(_value_leaves(val, f"returns.{k}"))
        for path, s in leaves:
            leaf = path.split('/')[-1].split('.')[-1]
            # structure, not values: the callee path (rendered as the callee's name and id), the field labels of an
            # entry list and the channel selector of a DLL parameter (its formatter renders the fields)
            if leaf == 'taskbotFile' or path.endswith('[].key') or leaf == 'numberValueSelect' or (skip and skip(path, s)):
                continue
            if tags and leaf in ('isIgnoreCarriage', 'runFunctionInBackground') and s == 'false':
                continue  # flags rendered only when set
            if tags and leaf == 'enabled' and '[]' in path:
                continue  # an entry's switch: a disabled header / form field is rendered with `[off]`
            yield path, leaf, s

    def dump_like(surface, n, text, where, skip=None, outputs=True):
        for path, leaf, s in node_leaves(n, True, skip, outputs):
            cmdkey = (f"{n.get('packageName')}.{n.get('commandName')}", path)
            if leaf == 'matchCase' and s in ('true', 'false'):  # a literal case rule is stated as a tag, both ways
                total[surface] += 1
                if (' [matchCase]' if s == 'true' else ' [ignoreCase]') not in text:
                    misses[(surface,) + cmdkey].append(f"{where} = {lit(s)}")
                continue
            if leaf == 'isIgnoreCarriage' and '[ignoreCarriage]' in text:
                total[surface] += 1
                continue
            need(surface, cmdkey, s, text, where, leaf)

    def literals_marked(surface, n, text, where):
        for a in n.get('attributes', []):
            if (a.get('value') or {}).get('type') == 'UIOBJECT':
                continue
            for s in _shaped_literals(a):
                need(surface, (f"{n.get('packageName')}.{n.get('commandName')}", 'literal with a variable\'s shape'),
                     f'literal"{s}"', text, where)

    def label_is_command(surface, n, text, where):
        """A line labelled with a package-qualified name carries the node's own command (`Database.store`), the name
        the source guide and a search of the export use - never a curated one."""
        m = re.match(r'\s*(?:\[off\]\s*)?([A-Za-z_][\w-]*\.[A-Za-z_][\w.]*)(?=\s|$)', text)
        if not m:
            return
        own = f"{n.get('packageName')}.{n.get('commandName')}"
        total[surface] += 1
        if m.group(1) != own:
            misses[(surface, own, 'label')].append(f"{where} = {lit(m.group(1))}")

    def ui_values(n):
        """The UIOBJECT values of a capture: its own `uiObject`, or the one under a condition's options."""
        own = flat_attrs(n).get('uiObject')
        return [own] if own else [s.get('value') for a in n.get('attributes', []) for s in a.get('attributes', []) or []
                                  if (s.get('value') or {}).get('type') == 'UIOBJECT']

    def criteria_shown(surface, n, text, where):
        """A capture's enabled criteria, name and value, which every rendering of it prints with the control."""
        for uv in ui_values(n):
            for k, c in (((uv or {}).get('uiObject') or {}).get('criteria') or {}).items():
                if c.get('enabled'):
                    need(surface, ('capture', 'criterion'), k, text, where)
                    for _, s in _value_leaves(c.get('value') or {}, ''):
                        need(surface, ('capture', f"criterion {k}"), s, text, where, k)

    cat ={e['botId']: e for e in targets(x)[0]}
    pd, sources, md = data(x)
    pd = {p['id']: p for p in pd}
    md_line = {int(m.group(1)): line for line in md.splitlines() if (m := re.match(r'- \((\d+)\) ', line))}
    by_use, by_use_md = collections.defaultdict(list), collections.defaultdict(list)  # a node's sources: JSON entries, .md lines
    for s in sources:
        for u in s['usedBy']:
            by_use[u].append(_flat(s))
            by_use_md[u].append(md_line.get(s['id'], ''))
    for b in x.bots:
        # dump: every node's full line; the compact line of every enabled node; the VARIABLES legend
        full = dump(x, str(b.id))
        legend = {}
        if '\nVARIABLES (' in full:
            for line in full.split('\nVARIABLES (', 1)[1].split('\n')[1:]:
                m = re.match(r'\s+\$([^$]+)\$ ', line)
                if m:
                    legend[m.group(1)] = line
        # the compact dump as printed, by editor line: a line its fold swallows is not shown, whatever its own rendering
        compact = {int(m.group(1)): m.group(2) for m in re.finditer(r'(?m)^\s*(\d+) (.*)$', dump(x, str(b.id), compact=True).split('\nVARIABLES (', 1)[0])}
        for ln, _, n, is_branch in b.lines:
            where = at(b, ln)
            text = x.render(n, b, is_branch)
            if text is not None:
                dump_like('dump', n, text, where)
                literals_marked('dump', n, text, where)
                criteria_shown('dump', n, text, where)
                label_is_command('dump', n, text, where)
            if n.get('disabled'):
                continue
            if x.compact_line(n, b, is_branch) is None:
                continue
            ctext = compact.get(ln)
            total['dump --compact'] += 1
            if ctext is None:
                misses[('dump --compact', f"{n.get('packageName')}.{n.get('commandName')}", 'enabled line folded')].append(where)
                continue
            # the documented compact folds: a binding or an output pair that passes a same-named variable through is a
            # count; a logger bot's call is its message
            folded = collections.defaultdict(set)  # side -> keys folded
            ret = n.get('returnTo') or {}
            if ret.get('type') == 'DICTIONARY':
                folded['returnTo'] = {d.get('key') for d in ret.get('dictionary') or [] if x.V(d.get('value'), b, d.get('key')) == f"${d.get('key')}$"}
            logger_call = False
            if n.get('commandName') == 'runTask':
                tb = (flat_attrs(n).get('taskbot') or {})
                callee = x.resolve((tb.get('taskbotFile') or {}).get('string') or (tb.get('taskbotFile') or {}).get('expression') or '')
                logger_call = bool(callee and callee.id in x.loggers)
                callee_stores = {var_key(nm) for nm in ((callee._var_uses() or ({}, {}))[0] if callee else {})}
                bound_back = {var_key(d.get('key') or '') for d in ((tb.get('taskbotOutput') or {}).get('dictionary') or []) + (ret.get('dictionary') or [])}
                if logger_call and (bound_back & callee_stores or ret.get('variableName')):
                    # a log line returns nothing: a call that binds back what the callee stores is a step, and its fold
                    # hides the results (a binding of what the callee never assigns hands the caller its own value back)
                    total['dump --compact'] += 1
                    misses[('dump --compact', 'TaskBot.runTask', 'logger call returning results')].append(where)
                for side in ('taskbotInput', 'taskbotOutput'):
                    folded[side] = {d.get('key') for d in (tb.get(side) or {}).get('dictionary', [])
                                    if x.V(d.get('value'), b, d.get('key')) == json.dumps(f"${d.get('key')}$")}

            def skip(path, s, folded=folded, logger_call=logger_call):
                side = next((sd for sd in folded if f".{sd}." in f".{path}"), None)
                if logger_call:  # the call is its message: its other bindings and its call settings fold away
                    return side not in ('taskbotInput', 'taskbotOutput') or path.endswith('.key') or not re.search(r'message|msg|text', path.rsplit('.', 1)[-1], re.I)
                if side is None:
                    return False
                key = s if path.endswith('.key') else path.split(f"{side}.", 1)[1].split('.', 1)[0].split('[', 1)[0]
                return key in folded[side]
            dump_like('dump --compact', n, ctext, where, skip)
            if not logger_call:
                literals_marked('dump --compact', n, ctext, where)
                criteria_shown('dump --compact', n, ctext, where)
                label_is_command('dump --compact', n, ctext, where)
        header =full.split('\n', 2)
        props = b.doc.get('properties') or {}
        for k, form in (('automationPriority', 'priority={}'), ('botCodeVersion', 'botCodeVersion={}'), ('timeout', 'timeout={}'),
                        ('runInChildWindow', 'runInChildWindow={}'), ('runInChildWindowMode', 'runInChildWindowMode={}'),
                        ('improvedNumberSupport', 'improvedNumberSupport={}')):
            if props.get(k) not in (None, ''):
                need('dump', ('header', k), form.format(props[k]), header[0], f"`{b.name}` ({b.id})")
        for pkg in b.doc.get('packages', []):
            need('dump', ('header', 'package'), f"{pkg.get('name')} {pkg.get('version')}", header[1] if len(header) > 1 else '', f"`{b.name}` ({b.id})")
        for v in b.doc.get('variables', []):
            where = f"`{b.name}` ({b.id}) ${v.get('name')}$"
            line = legend.get(v.get('name'), '')
            need('dump', ('VARIABLES', 'name'), f"${v.get('name')}$", line, where)
            need('dump', ('VARIABLES', 'type'), v.get('type') or '?', line, where)
            if v.get('sessionType'):
                need('dump', ('VARIABLES', 'sessionType'), v['sessionType'], line, where)
            for flag in ('input', 'output', 'readOnly'):
                if v.get(flag):
                    need('dump', ('VARIABLES', flag), {'input': 'in', 'output': 'out'}.get(flag, flag), line, where)
            for path, s in _value_leaves(v.get('defaultValue') or {}, 'default'):
                if s != '':
                    need('dump', ('VARIABLES', path), s, line, where, v.get('name') or '')
            if v.get('description'):
                need('dump', ('VARIABLES', 'description'), v['description'], line, where, 'description')
        # cards
        lines, rendered = bot_card(x, b)
        card = '\n'.join(lines)
        for key, it in rendered.items():  # what is tested is what is written: an item re-escaped on the way out is a miss
            total['cards'] += 1
            if it not in card:
                misses[('cards', 'printed as rendered')].append(f"`{b.name}` ({b.id}) {key}: {lit(it[:60])}")
        windows = next((t for t in lines if t.startswith('  windows: ')), '')
        for v in b.doc.get('variables', []):
            w = ((v.get('defaultValue') or {}).get('window') or {}) if v.get('type') == 'WINDOW' else {}
            for title in [w.get('name')] + [(c or {}).get('string') or (c or {}).get('expression') for c in (w.get('nameRegex') or {}).get('list') or []]:
                if title:
                    need('cards', ('windows',), title, windows, f"`{b.name}` ({b.id}) ${v.get('name')}$")
        for ln, _, n, is_branch in b.lines:
            if ln in rendered:
                dump_like('cards', n, rendered[ln], at(b, ln))
        for e in x.edges:
            if e['disabled']:
                continue
            if e['calleeId'] == b.id:
                need('cards', ('callers',), f"({e['callerId']})", card, at(b, e['line']))
            if e['callerId'] != b.id or e['calleeId'] in x.loggers:
                continue
            need('cards', ('calls',), f"({e['calleeId']})" if e['calleeId'] else e['calleePath'], card, at(b, e['line']))
            if not e['callee']:
                continue
            rp = rendered.get('real parameters', '')
            for k, _ in real_bindings(x, e, b):
                need('cards', ('real parameters', 'key'), k, rp, at(b, e['line']))
                for path, s in _value_leaves(e['inputs'][k], k):
                    if s != '':
                        need('cards', ('real parameters', 'value'), s, rp, at(b, e['line']), k)
            # a result stored under another name, read from the node's own returnTo map
            node = next(m for ln, _, m, _ in b.lines if ln == e['line'])
            for d in (node.get('returnTo') or {}).get('dictionary') or []:
                if (d.get('value') or {}).get('variableName') not in (None, '', d.get('key')):
                    need('cards', ('real parameters', 'result'), f"{d.get('key')} -> ${d['value']['variableName']}$", rp, at(b, e['line']))
        # targets
        entry = cat.get(b.id) or {}
        by_line = collections.defaultdict(list)
        for kind in ('controls', 'keystrokes', 'windowOps'):
            for it in entry.get(kind) or []:
                if it.get('line') is not None:
                    by_line[it['line']].append(_flat(it))
        for ln, _, n, _ in b.lines:
            cmd = n.get('commandName')
            is_capture = cmd == 'capture' or any((a.get('value') or {}).get('conditionalName') == 'capture' for a in n.get('attributes', []))
            # the catalog's window commands named here, not through its own list: a command dropped from that list
            # must still be looked for
            if not (is_capture or cmd == 'Keystrokes' or n.get('packageName') in ('Window', 'Wait', 'Mouse', 'ImageRecognition') or cmd in ('windowExists', 'windowTitleExists')):
                continue
            text, where = '\n'.join(by_line.get(ln, [])), at(b, ln)
            if is_capture:
                for uv in ui_values(n):
                    uo = (uv or {}).get('uiObject') or {}
                    for k in ('technologyType', 'controlType', 'browserType'):
                        if uo.get(k):
                            need('targets', ('capture', k), uo[k], text, where)
                    if uo.get('isElevated') is True:
                        need('targets', ('capture', 'isElevated'), 'elevated', text, where)
                    # a vision capture's recognised control and anchors, read from the decoded blob itself
                    blob = decode_blob(uo.get('blob'))
                    if isinstance(blob, dict) and 'mapping' in blob and 'objNode' not in blob:
                        stack = [blob['mapping']]
                        while stack:
                            o = stack.pop()
                            if isinstance(o, dict):
                                stack.extend(o.values())
                            elif isinstance(o, list):
                                stack.extend(o)
                            elif isinstance(o, str) and o:
                                need('targets', ('capture', 'vision'), o, text, where)
                criteria_shown('targets', n, text, where)
            for path, leaf, s in node_leaves(n, tags=False):
                need('targets', (f"{n.get('packageName')}.{cmd}", path), s, text, where, leaf)
        wtext = _flat(entry.get('windows') or [])
        for v in b.doc.get('variables', []):
            if v.get('type') != 'WINDOW':
                continue
            w = (v.get('defaultValue') or {}).get('window') or {}
            where = f"`{b.name}` ({b.id}) ${v.get('name')}$"
            for k, s in (('title', w.get('name')), ('class', w.get('class')), ('exe', os.path.basename((w.get('path') or '').replace('\\', '/'))),
                         ('nameCaseInsensitive', 'nameCaseInsensitive' if w.get('nameCaseInsensitive') is True else ''),
                         ('windowType', w.get('windowType') if w.get('windowType') != 'NATIVE' else ''), ('browserTabUrl', w.get('browserTabUrl')),
                         *(('nameRegex', (c or {}).get('string') or (c or {}).get('expression')) for c in (w.get('nameRegex') or {}).get('list') or [])):
                if s:
                    need('targets', ('window variable', k), s, wtext, where)
        # data: the bot inventory's call bindings, and the recorded attributes of every data-bearing command
        calls = {c['line']: _flat(c) for c in (pd.get(b.id) or {}).get('calls', [])}
        for ln, _, n, _ in b.lines:
            if n.get('commandName') != 'runTask':
                continue
            # the call's bindings read from the node: its taskbot value's input and output maps, and the node's own
            # returnTo - a map of results, or the one variable that receives them
            tb, ret = flat_attrs(n).get('taskbot') or {}, n.get('returnTo') or {}
            sides = (('inputs', (tb.get('taskbotInput') or {}).get('dictionary') or []),
                     ('outputs', ((tb.get('taskbotOutput') or {}).get('dictionary') or []) + (ret.get('dictionary') or [])))
            for side, entries in sides:
                for d in entries:
                    need('data', ('bot inventory', side), d.get('key') or '', calls.get(ln, ''), at(b, ln))
                    for path, s in _value_leaves(d.get('value'), d.get('key') or ''):
                        if s != '':
                            need('data', ('bot inventory', side), s, calls.get(ln, ''), at(b, ln), d.get('key') or '')
            if ret.get('type') == 'VARIABLE' and ret.get('variableName'):
                need('data', ('bot inventory', 'outputsTo'), ret['variableName'], calls.get(ln, ''), at(b, ln))
        for ln, _, n, _ in b.lines:
            if n.get('disabled'):
                continue
            attrs = flat_attrs(n)
            blob, md_blob = '\n'.join(by_use.get(at(b, ln), [])), '\n'.join(by_use_md.get(at(b, ln), []))
            names = set(data_recorded(n.get('packageName'), n.get('commandName'), attrs))
            for a in n.get('attributes', []):
                v = a.get('value') or {}
                if v.get('type') == 'ITERATOR' and re.search(r'email|files|folder', v.get('iteratorName') or ''):
                    names |= {'folder', 'from', 'subject', 'folderPath'}
            for nm in names:
                for a in [a for a in n.get('attributes', []) if a.get('name') == nm] + [s for a in n.get('attributes', []) for s in a.get('attributes') or []
                                                                                       if s.get('name') == nm]:
                    for path, s in _value_leaves(a.get('value'), nm):
                        if s != '':
                            need('data', (f"{n.get('packageName')}.{n.get('commandName')}", nm), s, blob, at(b, ln), nm)
                            need('data .md', (f"{n.get('packageName')}.{n.get('commandName')}", nm), s, md_blob, at(b, ln), nm)
    # A data source's name is what a step table cites, `Name` (id) in a markdown cell: no `|`, no backtick, no line break
    # or other invisible character, and no other source of its kind named alike
    named = collections.defaultdict(list)
    for s in sources:
        total['data'] += 2
        named[(s['kind'], s['name'])].append(s['id'])
        if not s['name'] or any(c in '|`' or (c != ' ' and unicodedata.category(c)[0] in 'CZ') for c in s['name']):
            misses[('data', 'source name', 'not citable')].append(f"({s['id']}) {lit(s['name'][:60])}")
    for (kind, nm), ids in named.items():
        if len(ids) > 1:
            misses[('data', 'source name', 'shared by sources of one kind')].append(f"{kind} {lit(nm[:60])} {py_text(ids)}")
    # Schema coverage: every key path holding a value is documented in schema.md; a path documented as ignored while it
    # holds a default carries nothing else
    rows = schema_rows()
    if rows is None:
        misses[('schema', 'schema.md')].append(f"{SCHEMA_FILE} not found")
    else:
        docs = [(f"`{b.name}` ({b.id})", b.doc, 'DOC') for b in x.bots]
        if x.manifest and x.manifest.get('raw'):
            docs.append(('manifest.json', x.manifest['raw'], 'MANIFEST'))
        for where, doc, root in docs:
            for path, v in schema_paths(doc, root):
                total['schema'] += 1
                status, allowed = schema_status(rows, path)
                if status is None:
                    misses[('schema', 'undocumented key path', path)].append(f"{where} = {lit(str(v)[:60])}")
                elif allowed is not None and str(v).lower() not in allowed:
                    misses[('schema', 'value outside its documented default', path)].append(f"{where} = {lit(str(v)[:60])}")
    # Graph consistency: the calls the bot inventory lists for a bot are exactly the Run Task nodes in that bot's own
    # tree - bots named alike in different folders must not share edges
    own = {b.id: sum(1 for _, _, n, _ in b.lines if n.get('packageName') == 'TaskBot' and n.get('commandName') == 'runTask') for b in x.bots}
    for p in pd.values():
        total['data'] += 1
        if len(p['calls']) != own.get(p['id'], 0):
            misses[('data', 'bot inventory', 'calls')].append(f"{p['name']} ({p['id']}) lists {len(p['calls'])}, its tree holds {own.get(p['id'], 0)}")
    n_miss = sum(len(v) for v in misses.values())
    L = [f"# Renderer completeness check — {len(x.bots)} bots, " + ', '.join(f"{s} {total[s]}" for s in ('dump', 'dump --compact', 'cards', 'targets', 'data', 'data .md', 'schema'))
         + f" values checked, {n_miss} not rendered", '']
    for key, items in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        L.append(f"- {' '.join(f'`{k}`' if i else k for i, k in enumerate(key))}: {len(items)} — e.g. {'; '.join(items[:3])}")
    return '\n'.join(L), n_miss


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['profile', 'cards', 'dump', 'raw', 'targets', 'data', 'check'])
    ap.add_argument('export_dir')
    ap.add_argument('bot', nargs='?', help='dump, raw: bot id, repository path, or a bot name that occurs once in the export')
    ap.add_argument('--out', default='.')
    ap.add_argument('--compact', action='store_true', help='dump: fold disabled subtrees, shorten log/delay lines')
    ap.add_argument('--lines', help='raw: FROM[-TO], editor lines as dump numbers them')
    ap.add_argument('--find', help='raw: the lines whose command or raw JSON holds this text')
    ap.add_argument('--var', help='raw: the declaration of this variable')
    a = ap.parse_args()
    span = None
    if a.command == 'raw':
        if not a.bot or not (a.lines or a.find or a.var):
            ap.error('raw requires BOT and at least one of --lines FROM[-TO], --find TEXT, --var NAME')
        if a.lines:
            m = re.fullmatch(r'(\d+)(?:-(\d+))?', a.lines.strip())
            if not m:
                ap.error(f"--lines takes FROM or FROM-TO, not {a.lines!r}")
            span = (int(m.group(1)), int(m.group(2) or m.group(1)))
    x = Export(a.export_dir)
    if a.command == 'raw':
        text, code = raw(x, a.bot, span, a.find, a.var)
        print(text)
        return code
    if a.command == 'dump':
        if not a.bot:
            ap.error('dump requires BOT (bot id, repository path, or a bot name that occurs once)')
        text = dump(x, a.bot, a.compact)
        print(text)
        return 2 if text.startswith(('no bot with', 'bot name ')) else 0
    if a.command == 'check':
        report, missed = check(x)
        print(report)
        return 1 if missed else 0
    os.makedirs(a.out, exist_ok=True)

    def W(name, content):
        p = os.path.join(a.out, name)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(content)
        print(f"written {p}")
    if a.command == 'targets':
        cat, md = targets(x)
        W('a360-targets.json', json.dumps(cat, indent=1, ensure_ascii=False))
        W('a360-targets.md', md)
        return 0
    if a.command == 'data':
        pd, rs, md = data(x)
        W('a360-process-data.json', json.dumps(pd, indent=1, ensure_ascii=False))
        W('a360-data-sources.json', json.dumps(rs, indent=1, ensure_ascii=False))
        W('a360-data-sources.md', md)
        return 0
    text = profile(x) if a.command == 'profile' else cards(x)
    W(f'a360-{a.command}.txt', text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
