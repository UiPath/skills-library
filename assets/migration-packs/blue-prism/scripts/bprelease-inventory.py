#!/usr/bin/env python3
"""Inventory a Blue Prism release (.bprelease) or a folder of exported processes and objects (.bpprocess / .bpobject) -
for reading it at genome extraction and for deriving the migration catalogs at genome execution.

Usage:
  bprelease-inventory.py check   <EXPORT>                     # renderer completeness self-test over dump, cards, targets and data, and schema coverage; exit 1 on a miss
  bprelease-inventory.py profile <EXPORT> [--out DIR]          # identity, contents, vocabulary, call graph, roots, what runs, queues, credentials, calendars, app models, evidence
  bprelease-inventory.py cards   <EXPORT> [--out DIR]          # one compact card per process and per business object
  bprelease-inventory.py dump    <EXPORT> <ID|NAME>[::<PAGE>] [--full] # one process or object, page by page, in execution order (--full prints code bodies);
                                                                #   a name is accepted only when it occurs once - a repeated name lists the copies
  bprelease-inventory.py raw     <EXPORT> <NAME|ID>[::<PAGE>] [--stage NAME|STAGEID] [--find TEXT] [--element NAME|ID]
                                                                # the export's own XML: the stages named (a data item is a stage) or holding a text,
                                                                #   application-model elements - every element and attribute, secrets masked - to check a
                                                                #   reading against the source, not against a rendering; a lookup, never a reading of a process
  bprelease-inventory.py targets <EXPORT> [--out DIR]          # UI target catalog: application models (elements with their matched attributes) + the actions applied to each element
  bprelease-inventory.py data    <EXPORT> [--out DIR]          # process inventory (call edges, groups) + data sources (work queues, environment variables, credentials, calendars, collection schemas, paths)
                                                                #   + page shapes (each page block-structured or a graph: its nodes, links and recovery regions)

<EXPORT> is a `.bprelease` file, a `.bpprocess` / `.bpobject` file, or a folder holding one or more of them (every file is
read as part of one export). The release is one XML document: processes and business objects are `<process>` trees of
`<stage>` elements linked by stage ids; a business object also carries its application model (`<appdef>`). Everything
else in a release (work queues, environment variables, credentials, groups, schedules, calendars, tiles, fonts) is a
short element the script lists by kind.

Which mode runs which command, and where the output goes: blue-prism/source-guide.md in this repository (command block
at the top); why nothing is written beside the genome: the uipath-planner skill's Source Map contract;
execution-time derivation: the skill's migration preflight.
Writes bp-profile.txt / bp-cards.txt / bp-targets.{json,md} / bp-process-data.json / bp-data-sources.{json,md} /
bp-page-shapes.{json,md} into --out (default: current directory); dump and raw print to stdout.
Never prints a secret: the initial value of a `password` data item (Blue Prism stores it encrypted anyway), the
initial value of a text data item or the value of an environment variable whose name says password / pwd / mot de
passe / secret / token, and a literal assigned into such a data item are masked. A flag or a number named like one
(`Password Expired`) holds a state and is printed, and `True` / `False` are never masked. Credential *names* are identity, not secrets,
and are kept (the skill's Platform Dependencies rules).

Identity: Blue Prism identifies processes, objects and stages by GUID. The script derives a stable six-digit id from
the GUID (CRC-32, folded into 100000-999999) so Source Map rows can carry `Name` (id) and the skill's check-genome.mjs
can resolve the step tables' citations against the files `data` writes; the same export always yields the same ids. Work queues carry their numeric release id; other data
sources get a CRC-derived id from `kind|name`. A GUID several files hold is one definition, read from the release created
last; `profile` and `dump` name the other copies and whether each differs, and `raw` prints every copy.
"""
import argparse
import collections
import copy
import functools
import json
import os
import re
import sys
import unicodedata
import zlib
import xml.etree.ElementTree as ET

SENSITIVE = re.compile(r'passw|pwd|secret|token|mot ?de ?passe|motpasse|\bmdp\b|api[_-]?key', re.I)
# Blue Prism internal business objects (resource object names of Action stages) -> readable family
INTERNAL = {
    'Blueprism.Automate.clsWorkQueuesActions': 'Work Queues',
    'Blueprism.Automate.clsCredentialsActions': 'Credentials',
    'Blueprism.AutomateProcessCore.clsCollectionActions': 'Collections',
    'BluePrism.AutomateAppCore.clsEnvironmentLockingBusinessObject': 'Environment Locking',
    'Blueprism.Automate.clsCalendarsBusinessObject': 'Calendars',
    'Blueprism.Automate.clsEncryptionBusinessObject': 'Encryption',
    'Blueprism.Automate.clsDataGatewaysBusinessObject': 'Data Gateways',
    'Blueprism.Automate.clsDocumentProcessingActions': 'Document Processing',
}
FLOW_TYPES = {'Start', 'End', 'Action', 'Decision', 'Calculation', 'MultipleCalculation', 'ChoiceStart', 'ChoiceEnd',
              'LoopStart', 'LoopEnd', 'SubSheet', 'Process', 'Exception', 'Recover', 'Resume', 'Navigate', 'Read', 'Write',
              'WaitStart', 'WaitEnd', 'Code', 'Anchor', 'Note', 'Alert', 'Skill'}
NOT_FLOW = {'Data', 'Collection', 'Block', 'SubSheetInfo', 'ProcessInfo'}
UI_TYPES = {'Navigate', 'Read', 'Write', 'WaitStart'}
# a flag's literal is never a secret: harvested as one, every `True` / `False` of the export would be masked
FLAG_LITERALS = ('True', 'False')
# a token literal anywhere in a string (a header, a code stage, a start argument); an expression (`Bearer [Token]`) stays
JWT_LITERAL = re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}')
BEARER_LITERAL = re.compile(r'(?i)\b(bearer|basic)\s+(?![\["])([A-Za-z0-9._~+/=\-]{16,})')
# element type prefix -> technology (the spy mode Blue Prism used); the source guide § UI Target Locators explains each
TECH_PREFIX = (('SAP', 'sapgui'), ('Web', 'web'), ('UIA', 'uia'), ('AA', 'aa'), ('HTML', 'html-ie'), ('Java', 'java'),
               ('Terminal', 'terminal'), ('Mainframe', 'terminal'), ('DD', 'dde'), ('Region', 'region'), ('Citrix', 'citrix'))
WIN32_TYPES = {'Window', 'Edit', 'Button', 'Label', 'CheckBox', 'RadioButton', 'ComboBox', 'ListBox', 'ListView', 'TreeView',
               'TabControl', 'Tab', 'StatusBar', 'ToolBar', 'Toolbar', 'Menu', 'MenuItem', 'ScrollBar', 'TrackBar', 'UpDown',
               'DateTimePicker', 'MonthCalView', 'Hyperlink', 'Grid', 'Splitter', 'Element', 'ChildWindow', 'ContainerWindow',
               'NetLinkLabel', 'NetPictureBox', 'NetDataGridView', 'NetListView'}
POSITIONAL_ATTRS = {'X', 'Y', 'Width', 'Height', 'ScreenBounds', 'Ordinal', 'ChildCount', 'AncestorCount', 'Style', 'wX', 'wY', 'wWidth',
                    'wHeight', 'wClientX', 'wClientY', 'wClientWidth', 'wClientHeight', 'wOffsetX', 'wOffsetY', 'wOffsetWidth', 'wOffsetHeight',
                    'wScrollX', 'wScrollY', 'wScrollWidth', 'wScrollHeight', 'aX', 'aY', 'aWidth', 'aHeight', 'uX', 'uY', 'uWidth', 'uHeight',
                    'pX', 'pY', 'pWidth', 'pHeight', 'paX', 'paY', 'paWidth', 'paHeight'}
STATE_ATTRS = {'Visible', 'ScreenVisible', 'Enabled', 'Active', 'pVisible', 'pScreenVisible', 'pEnabled', 'pActive', 'pChecked', 'Checked',
               'Focused', 'Selected', 'Pressed', 'aChecked', 'Unavailable', 'Offscreen', 'Invisible', 'uEnabled', 'uOffscreen', 'uHasKeyboardFocus',
               'uItemStatus', 'wIsEditable', 'Busy', 'Default', 'Expanded', 'Collapsed'}
# A Java element's role, its action list and its layout and state flags say what kind of control it is, never which one.
JAVA_KIND_ATTRS = {'Role', 'AllowedActions', 'KeyBindings', 'Horizontal', 'Vertical', 'SingleLine', 'MultipleLine', 'Modal', 'Showing',
                   'Opaque', 'Focusable', 'Editable', 'Selectable', 'Multiselectable', 'Transient'}
DATA_INPUT_NAMES = re.compile(r'file|path|folder|directory|workbook|worksheet|url|link|command', re.I)
KEY_TOKEN = re.compile(r'\{[A-Z0-9]+\}|<\{[A-Z]+\}[^<>]*\{[A-Z]+\}>')
COLL_FIELD = re.compile(r'\[([^\[\].]+?)\.([^\[\]]+)\]')
DATA_REF = re.compile(r'\[([^\[\]]+)\]')
FUNC = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')
SAP_ID_IN_CODE = re.compile(r'findById\(\s*"([^"]+)"')
INVISIBLE = re.compile(r'[^\x20-\x7e]|\\(?=u[0-9A-Fa-f]{4})')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def stable_id(key):
    return 100000 + zlib.crc32(key.encode('utf-8')) % 900000


INTERNAL_BY_CLASS = {k.rsplit('.', 1)[-1].lower(): v for k, v in INTERNAL.items()}


def internal_family(obj):
    """The readable family of a Blue Prism internal business object (`Work Queues`), `internal` for another object
    inside the product, None for a business object of the release. The product spells its own prefix `Blueprism.`
    and `BluePrism.` and also writes the class name alone (`clsCalendarsBusinessObject`), so a name is matched by its
    class, case-insensitively."""
    o = obj or ''
    return INTERNAL_BY_CLASS.get(o.rsplit('.', 1)[-1].lower()) or ('internal' if re.match(r'(?i)blue ?prism\.', o) else None)


def sensitive_item(datatype, name):
    """A data item that can hold a secret: a password item, or a text item named like one. A flag or a number named
    like one (`Password Expired`, `Password Age`) holds a state, not a secret."""
    return datatype == 'password' or (datatype in (None, 'text') and bool(SENSITIVE.search(name or '')))


def local(tag):
    return tag.split('}', 1)[1] if '}' in tag else tag


def strip_ns(root):
    for el in root.iter():
        el.tag = local(el.tag)
    return root


def prolog_end(raw):
    """The offset of a document's first element - past the byte-order mark, the XML declaration, processing
    instructions, comments and white space, however long: where a DTD would stand."""
    i = 3 if raw.startswith(b'\xef\xbb\xbf') else 0
    while True:
        while raw[i:i + 1].isspace():
            i += 1
        end = {b'<?': b'?>', b'<!--': b'-->'}.get(raw[i:i + 2] if raw.startswith(b'<?', i) else raw[i:i + 4])
        if end is None:
            return i
        j = raw.find(end, i)
        i = len(raw) if j < 0 else j + len(end)


def parse_xml(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw.startswith((b'<!DOCTYPE', b'<!ENTITY'), prolog_end(raw)):
        sys.exit(f"{path}: the document carries a DTD or entity declaration; a Blue Prism export never does - not read")
    return strip_ns(ET.fromstring(raw))


def text(el, tag, default=None):
    if el is None:
        return default
    x = el.find(tag)
    return (x.text if x is not None and x.text is not None else default)


def attrs_of(el):
    return dict(el.attrib) if el is not None else {}


def geometry(el):
    """(x, y, w, h) of a stage on its page's diagram, x and y None when it has none: Blue Prism 7 writes
    `<display x y w h>`, Blue Prism 6 `<displayx>`, `<displayy>`, `<displaywidth>`, `<displayheight>` elements. A
    stage's x, y is its centre; a Block's is its top-left corner, with w, h its extent."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    d = el.find('display')
    if d is not None and d.get('x') is not None:
        return num(d.get('x')), num(d.get('y')), num(d.get('w')) or 0.0, num(d.get('h')) or 0.0
    return num(el.findtext('displayx')), num(el.findtext('displayy')), num(el.findtext('displaywidth')) or 0.0, num(el.findtext('displayheight')) or 0.0


def block_holding(p, b):
    """The stages a Recover's Block holds, as its scope line names them: every stage of the page whose centre lies in
    the Block (an inner Block's stages too), except Anchors, Notes, Recovers and data, top to bottom, then left to right.
    Block names repeat on a page, so the stages are what tells two Blocks apart."""
    held = sorted((s for s in p.stages.values() if b in s.blocks and s.type not in NOT_FLOW | {'Anchor', 'Note', 'Recover'}),
                  key=lambda s: (s.y, s.x, s.name or ''))
    return 'holding ' + (', '.join(f"‹{s.name}› [{s.type}]" for s in held) if held else 'no stage')


def web_api_service(el):
    """A Web API service definition (a business object Blue Prism generates from a REST description): base URL, common
    headers, parameters and code, and every action's request and output parameters. A parameter or header named like a
    secret has its literal masked here, before anything renders it; an expression (`Bearer [AccessToken]`) stays."""
    cfg = el.find('configuration')

    def params(parent):
        out = []
        for ap in (parent.findall('actionparameter') if parent is not None else []):
            iv = ap.findtext('initialvalue') or ''
            out.append({'name': ap.get('name'), 'datatype': ap.get('datatype'), 'expose': ap.get('expose'),
                        'initialValue': '***' if iv and SENSITIVE.search(ap.get('name') or '') else iv, 'description': (ap.findtext('description') or '').strip()})
        return out

    def headers(parent):
        return [{'name': h.get('name'), 'value': '***' if SENSITIVE.search(h.get('name') or '') and '[' not in (h.get('value') or '') else h.get('value')}
                for h in (parent.findall('httpheader') if parent is not None else [])]
    actions = []
    for a in (cfg.findall('actions/action') if cfg is not None else []):
        req = a.find('request')
        body = req.find('bodycontent') if req is not None else None
        actions.append({'name': a.get('name'), 'id': a.get('id'), 'enabled': a.get('enabled'), 'description': (a.findtext('description') or '').strip(),
                        'method': req.get('httpmethod') if req is not None else None, 'urlPath': req.get('urlpath') if req is not None else None,
                        'headers': headers(req.find('headers') if req is not None else None),
                        'body': {'type': body.get('type'), 'template': body.findtext('template'), 'code': body.findtext('code'),
                                 'fileParameter': body.get('fileinputparametername')} if body is not None else None,
                        'parameters': params(a.find('parameters')),
                        'outputs': [{'name': o.get('name'), 'type': o.get('type'), 'path': o.get('path'), 'datatype': o.get('datatype'), 'description': o.get('description')}
                                    for o in a.findall('outputparameters/parameters/customoutputparameter')],
                        'outputCode': a.findtext('outputparameters/code'),
                        'requestOutputParameter': a.get('enableRequestOutputParameter'), 'sendingDisabled': a.get('disableSendingOfRequest')})
    code = cfg.find('commoncode/codeproperties') if cfg is not None else None
    settings = cfg.find('configurationsettings/configurationsettings') if cfg is not None else None
    return {'baseUrl': cfg.get('baseurl') if cfg is not None else None, 'enabled': el.get('enabled'),
            'settings': dict(settings.attrib) if settings is not None else {},
            'authentication': [dict(au.attrib, **{k.tag: dict(k.attrib) for k in au}) for au in cfg.findall('commonauthentications/authentication')]  # the credential it signs in with
            if cfg is not None else [],
            'commonHeaders': headers(cfg.find('commonheaders') if cfg is not None else None),
            'commonParameters': params(cfg.find('commonparameters') if cfg is not None else None),
            'code': {'language': code.get('language'), 'code': code.get('code'), 'namespaces': [n.text for n in code.findall('namespaces/namespace')],
                     'references': [r.text for r in code.findall('references/reference')]} if code is not None else None,
            'actions': actions}


def calendar(el):
    """A calendar item's working-day rules, one entry per schedule calendar: its attributes as written (the working
    week, the public holiday group), the group's public holidays it overrides by id, and its other holidays."""
    return [{'attrs': {k: v for k, v in sc.attrib.items() if k not in ('id', 'name')},
             'publicHolidayOverrides': [ph.get('id') for ph in sc.findall('public-holiday-overrides/public-holiday')],
             'otherHolidays': [d.get('value') for d in sc.findall('other-holidays/other-date')]} for sc in el.findall('schedule-calendar')]


class Element:
    """One node of an object's application model: a spied element, a surface-automation region (a rectangle of a
    window, declared in a region container), or a region container that is that window."""

    def __init__(self, el, path, group=None, region=False):
        self.region = region
        self.name = el.get('name') or ''
        self.guid = text(el, 'id', '')
        self.type = text(el, 'type')
        self.basetype = text(el, 'basetype')
        self.datatype = text(el, 'datatype')
        self.description = text(el, 'description')
        self.narrative = text(el, 'narrative')
        self.path = path
        self.group = group
        self.attributes = []
        for a in el.findall('attributes/attribute'):
            pv = a.find('ProcessValue')
            self.attributes.append({'name': a.get('name'), 'inuse': a.get('inuse') == 'True', 'comparisontype': a.get('comparisontype'),
                                    'datatype': pv.get('datatype') if pv is not None else None, 'value': pv.get('value') if pv is not None else None})
        self.children = []

    @property
    def technology(self):
        t = self.type or ''
        if self.region:
            return 'region'
        if t == 'Application' or not t:
            return 'application' if t else None
        for prefix, tech in TECH_PREFIX:
            if t.startswith(prefix):
                return tech
        if t in WIN32_TYPES or t.startswith('Net'):
            return 'win32'
        return 'other'

    def matched(self):
        return [a for a in self.attributes if a['inuse']]

    def info(self):
        """Every attribute the spy recorded with a value that is not used for matching - the candidates a rebuilt
        selector may use; kept by no list of attributes worth showing."""
        return [a for a in self.attributes if not a['inuse'] and a['value'] not in (None, '')]

    def is_positional_only(self):
        kind = JAVA_KIND_ATTRS if self.technology == 'java' else set()
        m = [a['name'] for a in self.matched() if a['name'] not in STATE_ATTRS and a['name'] not in kind]
        return bool(m) and all(a in POSITIONAL_ATTRS or a.lower() in ('matchindex', 'matchreverse') for a in m)

    def is_dynamic(self):
        return any(a['comparisontype'] == 'dynamic' for a in self.matched())


class Stage:
    def __init__(self, el, proc):
        self.el = el
        self.id = el.get('stageid')
        self.name = el.get('name') or ''
        self.type = el.get('type')
        self.page = text(el, 'subsheetid')  # None = main page
        self.narrative = (text(el, 'narrative') or '').strip()
        self.x, self.y, self.w, self.h = geometry(el)
        self.onsuccess = text(el, 'onsuccess')
        # stage logging: no element = every run logged, `onsuccess="true"` = errors only, a bare element = never logged
        li = el.find('loginhibit')
        self.logging = 'all' if li is None else ('errors' if li.get('onsuccess') == 'true' else 'none')
        self.inputs = [{'type': i.get('type'), 'name': i.get('name'), 'expr': i.get('expr') or '', 'friendly': i.get('friendlyname'), 'stage': i.get('stage') or '',
                        'narrative': (i.get('narrative') or '').strip()} for i in el.findall('inputs/input')]
        self.outputs = [{'type': o.get('type'), 'name': o.get('name'), 'stage': o.get('stage') or '', 'narrative': (o.get('narrative') or '').strip()}
                        for o in el.findall('outputs/output')]
        r = el.find('resource')
        self.resource = (r.get('object'), r.get('action')) if r is not None else None
        c = el.find('calculation')
        self.calc = (c.get('expression'), c.get('stage')) if c is not None else None
        self.calcs = [(c.get('expression'), c.get('stage')) for c in el.findall('steps/calculation')]
        dec = el.find('decision')
        self.decision = dec.get('expression') if dec is not None else None
        self.ontrue = text(el, 'ontrue')
        self.onfalse = text(el, 'onfalse')
        self.groupid = text(el, 'groupid')
        self.timeout = text(el, 'timeout')
        self.looptype = text(el, 'looptype')
        # the collection a loop walks: `<loopdata>` text, or a `<collection name>` child of it
        self.loopdata = (text(el, 'loopdata') or '').strip() or (el.find('loopdata/collection').get('name') if el.find('loopdata/collection') is not None else None)
        self.processid = text(el, 'processid')
        ex = el.find('exception')
        self.exception = attrs_of(ex) if ex is not None else None
        self.datatype = text(el, 'datatype')
        iv = el.find('initialvalue')
        self.initialvalue = iv.text if iv is not None else None
        # a password item's encrypted initial value; an empty `<initialvalueenc />` means it has none
        self.encrypted = bool((text(el, 'initialvalueenc') or '').strip())
        self.private = el.find('private') is not None
        self.alwaysinit = el.find('alwaysinit') is not None
        self.exposure = text(el, 'exposure')
        self.fields = [(f.get('name'), f.get('type')) for f in el.findall('collectioninfo/field')]
        self.field_tree = field_tree(el.find('collectioninfo'))
        self.singlerow = el.find('collectioninfo/singlerow') is not None or el.find('singlerow') is not None
        # a Collection stage's initial rows (<initialvalue><collection><row><field name value/>…): lookup tables the author typed in
        self.rows = []
        iv = el.find('initialvalue')
        if self.type == 'Collection' and iv is not None:
            for row in iv.iter('row'):
                # a password cell holds `encvalue`, its encrypted value, instead of `value`
                self.rows.append({f.get('name'): '***' if f.get('encvalue') else f.get('value') for f in row.findall('field')})
            if self.rows and not self.fields:
                self.fields = [(k, None) for k in self.rows[0]]
        self.code = text(el, 'code')
        self.language = text(el, 'language')
        self.references = [r.text for r in el.findall('references/reference')]
        self.imports = [r.text for r in el.findall('imports/import')]
        self.globalcode = text(el, 'globalcode')
        self.interval = el.get('interval')
        self.attempts = text(el, 'attempts')
        self.preconditions = [c.get('narrative') or (c.text or '') for c in el.findall('preconditions/condition')]
        self.postconditions = [c.get('narrative') or (c.text or '') for c in el.findall('postconditions/condition')]
        self.choices = []
        for ch in el.findall('choices/choice'):
            cond = ch.find('condition')
            elx = ch.find('element')
            self.choices.append({'name': text(ch, 'name', ''), 'expression': ch.get('expression'), 'ontrue': text(ch, 'ontrue'), 'reply': ch.get('reply'),
                                 'element': elx.get('id') if elx is not None else None, 'elementparameters': self._eparams(elx),
                                 'condition': text(cond, 'id') if cond is not None else None,
                                 'args': {text(a, 'id'): (text(a, 'value') or '') for a in cond.findall('arguments/argument')} if cond is not None else {},
                                 'comparetype': text(ch, 'comparetype')})
        self.steps = []
        for stp in el.findall('step'):
            elx = stp.find('element')
            act = stp.find('action')
            self.steps.append({'element': elx.get('id') if elx is not None else None, 'elementparameters': self._eparams(elx),
                               'action': text(act, 'id') if act is not None else None,
                               'args': {text(a, 'id'): (text(a, 'value') or '') for a in act.findall('arguments/argument')} if act is not None else {},
                               'outputs': {text(o, 'id'): (text(o, 'value') or '') for o in act.findall('outputs/output')} if act is not None else {},
                               'stage': stp.get('stage'), 'expr': stp.get('expr')})
        self.block, self.blocks = None, []  # the innermost Block enclosing this stage, and every one (geometry), set by the owner
        self.proc = proc

    @staticmethod
    def _eparams(elx):
        if elx is None:
            return {}
        return {text(p, 'name'): {'expression': text(p, 'expression'), 'comparisontype': text(p, 'comparisontype')} for p in elx.findall('elementparameter')}

    def is_sensitive_target(self):
        return sensitive_item(self.datatype, self.name)


def field_tree(parent):
    """A collection's fields as [(name, type, nested fields)]: a field of type collection declares its own fields."""
    return [(f.get('name'), f.get('type'), field_tree(f)) for f in parent.findall('field')] if parent is not None else []


def unique(items):
    """The items that differ, each once, in their order."""
    seen, out = set(), []
    for it in items:
        k = json.dumps(it, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def definition(p):
    """A process or object as written, for telling two copies of one GUID apart: its wrapper's attributes (the published
    flag) and its own element, the whitespace after it aside."""
    tail, p.inner.tail = p.inner.tail, None
    try:
        own = ET.tostring(p.inner, encoding='unicode')
    finally:
        p.inner.tail = tail
    return sorted(p.wrapper.attrib.items()) if p.wrapper is not p.inner else [], own


class Proc:
    """A process or a business object."""

    def __init__(self, wrapper, inner, export, path=''):
        self.export = export
        self.guid = wrapper.get('id') or inner.get('id') or ''
        self.name = wrapper.get('name') or inner.get('name') or ''
        self.published = wrapper.get('published')
        self.kind = 'object' if (local(wrapper.tag) == 'object' or inner.get('type') == 'object') else 'process'
        # a single-file export carries no GUID: its file tells two same-named copies apart
        self.path = path
        rel = os.path.relpath(path, export.given) if path and os.path.isdir(export.given) else os.path.basename(path)
        self.id = stable_id(self.guid or f"{rel.replace(os.sep, '/')}::{self.name}")
        self.shared = inner.get('shared')
        self.wrapper, self.inner = wrapper, inner  # the file's own elements: `check` and `raw` read them, the renderers read what is parsed below
        self.groups = []  # [{path, default}]: the release's groups that list it, set by the export; none in a single-file export
        self.copies = []  # the other files holding the same GUID, set by the export (`Export._collapse_copies`)
        self.version = inner.get('version')
        self.bpversion = inner.get('bpversion')
        self.narrative = (inner.get('narrative') or '').strip()
        self.runmode = inner.get('runmode')
        self.preconditions = [c.get('narrative') or (c.text or '') for c in inner.findall('preconditions/condition')]
        ep = inner.find('endpoint')
        self.endpoint = (ep.get('narrative') or '').strip() if ep is not None else ''
        self.sheets_xml = inner.findall('subsheet')
        self.sheets = []  # in tab order
        for s in inner.findall('subsheet'):
            self.sheets.append({'id': s.get('subsheetid'), 'name': text(s, 'name', ''), 'type': s.get('type'), 'published': s.get('published')})
        self.sheet_by_id = {s['id']: s for s in self.sheets}
        self.stages = collections.OrderedDict()
        for st in inner.findall('stage'):
            s = Stage(st, self)
            self.stages[s.id] = s
        self.info = next((s for s in self.stages.values() if s.type == 'ProcessInfo'), None)
        self.appdef = inner.find('appdef')
        self.apptype, self.appparams, self.elements, self.inherits = None, {}, [], None
        if self.appdef is not None:
            ati = self.appdef.find('apptypeinfo')
            if ati is not None:
                self.apptype = text(ati, 'id')
                self.appparams = {text(p, 'name'): (text(p, 'value') or '') for p in ati.findall('parameters/parameter')}
            inh = self.appdef.find('inherits')
            self.inherits = inh.text if inh is not None else None
            self._walk_model(self.appdef, [], None)
        self.element_by_id = {e.guid: e for e in self.elements}
        self.element_tags = element_tags(self.elements)
        self._assign_blocks()

    def _walk_model(self, node, path, group):
        """Every element and region below `node`, in document order: elements nest in elements and in groups, groups
        nest at any depth (also directly under the model), and regions sit in a region container. A region container
        with a type is the window a region spy captured, an element itself that stages act on, holding regions,
        elements, groups and other containers; one without is a name only, read as a group. `path` is the ancestor
        element names, `group` the groups (or the untyped region container) holding the element, outermost first."""
        for ch in node:
            if ch.tag in ('element', 'region') or is_window_container(ch):
                e = Element(ch, path, group, region=ch.tag == 'region')
                self.elements.append(e)
                self._walk_model(ch, path + [e.name], None)
            elif ch.tag in ('group', 'region-container'):
                gname = ch.get('name') or text(ch, 'name') or ''
                self._walk_model(ch, path, f"{group}/{gname}" if group else gname)

    def _assign_blocks(self):
        """The Blocks whose rectangle holds each stage's centre, on the same page, innermost (smallest) first."""
        blocks = [s for s in self.stages.values() if s.type == 'Block' and s.x is not None and s.y is not None]
        for s in self.stages.values():
            if s.type in ('Block', 'SubSheetInfo', 'ProcessInfo') or s.x is None or s.y is None:
                continue
            around = []
            for b in blocks:
                if b.page != s.page:
                    continue
                if b.x <= s.x <= b.x + b.w and b.y <= s.y <= b.y + b.h:
                    around.append(b)
            s.blocks = sorted(around, key=lambda b: b.w * b.h)
            s.block = s.blocks[0] if s.blocks else None

    # ----- structure -----
    def page_name(self, page_id):
        if page_id is None:
            return 'Main Page' if self.kind == 'process' else 'Initialise'
        s = self.sheet_by_id.get(page_id)
        return s['name'] if s else f'<page {page_id}>'

    def pages(self):
        """[(page id or None, name, type)] main page first, then the tabs in order."""
        out = [(None, self.page_name(None), 'Main')]
        for s in self.sheets:
            out.append((s['id'], s['name'], s['type']))
        return out

    def page_stages(self, page_id):
        return [s for s in self.stages.values() if s.page == page_id]

    def start_of(self, page_id):
        return next((s for s in self.page_stages(page_id) if s.type == 'Start'), None)

    def end_outputs(self, page_id):
        out = []
        for s in self.page_stages(page_id):
            if s.type == 'End':
                for o in s.outputs:
                    if o['name'] not in [x['name'] for x in out]:
                        out.append(o)
        return out

    def actions(self):
        """Object actions = the Normal pages (the Initialise page and Clean Up are lifecycle)."""
        return [s for s in self.sheets if s['type'] not in ('CleanUp',)]

    def data_items(self, page_id='*'):
        return [s for s in self.stages.values() if s.type in ('Data', 'Collection') and (page_id == '*' or s.page == page_id)]

    def literal_of(self, name):
        """The literal initial value of a data item named `name`, when it has one (used to resolve `[queue]` expressions)."""
        for s in self.stages.values():
            if s.type == 'Data' and s.name == name and s.initialvalue not in (None, '') and not s.is_sensitive_target():
                return s.initialvalue
        return None

    def resolve(self, expr):
        """`[workQueue1]` -> its literal initial value when the data item has one; anything else stays an expression."""
        if not expr:
            return expr
        m = re.fullmatch(r'\s*\[([^\]]+)\]\s*', expr)
        if m:
            lit = self.literal_of(m.group(1))
            if lit is not None:
                return lit
        return expr

    def name_of(self, expr):
        """The name an input gives a queue, a credential or a calendar: a text literal's text (`""` a quote inside it),
        a data item's literal initial value, any other expression as written - a name built at run time
        (`"<prefix>_" & [machine]`) is known only when it runs."""
        m = re.fullmatch(r'\s*"((?:[^"]|"")*)"\s*', expr or '')
        return m.group(1).replace('""', '"') if m else self.resolve(expr)

    def expressions(self):
        for s in self.stages.values():
            if s.decision:
                yield s, s.decision
            if s.calc:
                yield s, s.calc[0]
            for e, _ in s.calcs:
                yield s, e
            for i in s.inputs:
                if i['expr']:
                    yield s, i['expr']
            for ch in s.choices:
                if ch['expression']:
                    yield s, ch['expression']
                if ch['reply']:
                    yield s, ch['reply']
                for v in list(ch['args'].values()) + [p['expression'] for p in ch['elementparameters'].values()]:
                    if v:
                        yield s, v
            for stp in s.steps:
                for v in stp['args'].values():
                    if v:
                        yield s, v
                if stp['expr']:
                    yield s, stp['expr']
                for p in stp['elementparameters'].values():
                    if p['expression']:
                        yield s, p['expression']
            if s.exception and s.exception.get('detail'):
                yield s, s.exception['detail']
            if s.timeout:
                yield s, s.timeout

    def collection_fields(self):
        """Per collection name, the fields it declares and those `[Collection.Field]` reads, as one set for every
        collection of that name: names compare case-insensitively, the first spelling declared names the set."""
        colls, lower = {}, {}
        for s in self.stages.values():
            if s.type == 'Collection':
                k = lower.setdefault(s.name.lower(), s.name)
                colls.setdefault(k, set()).update(f for f, _ in s.fields)
        for _, e in self.expressions():
            for coll, field in COLL_FIELD.findall(e):
                k = lower.get(coll.strip().lower())
                if k:
                    colls[k].add(field.strip())
        return colls

    def env_items(self):
        return [s for s in self.stages.values() if s.exposure]


class Export:
    def __init__(self, given):
        self.given = os.path.abspath(given)
        self.files = self._files(self.given)
        self.releases = []   # release headers
        self.procs = []
        self.items = collections.defaultdict(list)  # other content kinds
        for f in self.files:
            root = parse_xml(f)
            if root.tag == 'release':
                contents = root.find('contents')
                self.releases.append({'file': f, 'name': text(root, 'name'), 'created': text(root, 'created'), 'packageId': text(root, 'package-id'),
                                      'packageName': text(root, 'package-name'), 'createdBy': text(root, 'user-created-by'),
                                      'notes': (text(root, 'release-notes') or '').strip(), 'count': contents.get('count') if contents is not None else None})
                mark = len(self.items.get('work-queue', []))
                for child in (contents if contents is not None else []):
                    self._add(child, f)
                self._place_members(contents if contents is not None else [], f, self.items.get('work-queue', [])[mark:])
            elif root.tag == 'process':
                # a single .bpprocess / .bpobject: the root is the inner process element
                self.procs.append(Proc(root, root, self, f))
            else:
                self.items[root.tag].append({'name': root.get('name'), 'attrs': dict(root.attrib), 'file': f})
        self._collapse_copies()
        for kind, its in self.items.items():  # a release exported with another repeats the queues, variables and groups they share
            self.items[kind] = unique(its)
        self.procs.sort(key=lambda p: (p.kind != 'process', p.name.lower()))
        self.by_guid = {p.guid: p for p in self.procs}
        self.by_name = {}
        for p in self.procs:
            self.by_name.setdefault(p.name, []).append(p)
        self.by_name_ci = {p.name.lower(): p for p in self.procs}
        for p in self.procs:  # a call by the name an older copy carried reaches the definition kept
            for c in p.copies:
                self.by_name_ci.setdefault(c['name'].lower(), p)
        # the vendor's description of an internal action is copied into every stage that calls it; a text only one
        # call carries is the author's note
        texts = collections.Counter(((s.resource[0] or '').lower(), s.resource[1], s.narrative) for p in self.procs for s in p.stages.values()
                                    if s.type == 'Action' and s.resource and s.narrative and internal_family(s.resource[0]))
        self.stock_narratives = {k for k, n in texts.items() if n > 1}
        self.secrets = self._secrets()
        self.tags = {}  # (process or object, page) -> {stage id: ` #<short id>`}: `stage_tag`
        self.flows = {}  # (process or object, page) -> `page_flow`
        self.edges = self._edges()
        self.queues = self._queues()

    def flow(self, p, page_id):
        if (p, page_id) not in self.flows:
            self.flows[(p, page_id)] = page_flow(p, page_id)
        return self.flows[(p, page_id)]

    @functools.cached_property
    def reach(self):
        return Reach(self)

    def _collapse_copies(self):
        """One definition per GUID. A folder of releases repeats the processes and objects they share; the copy kept is
        the one of the release created last (on a tie, or without release headers, the one read last), and every other
        copy is listed on it under `copies`: its file, its release's creation date, its name, and whether it is the same
        definition - a copy that differs is another version, which `raw` prints beside the kept one."""
        self.created = {r['file']: r['created'] for r in self.releases}
        by_guid = collections.defaultdict(list)
        for n, p in enumerate(self.procs):
            by_guid[p.guid or n].append(p)
        kept = []
        for ps in by_guid.values():
            keep = max(enumerate(ps), key=lambda t: (self.created.get(t[1].path) or '', t[0]))[1]
            mine = definition(keep)
            keep.copies = [{'file': c.path, 'created': self.created.get(c.path), 'name': c.name, 'same': definition(c) == mine} for c in ps if c is not keep]
            kept.append(keep)
        self.procs = kept

    def rel(self, path):
        """A file of the export as `profile` and `dump` name it: relative to the folder given."""
        return (os.path.relpath(path, self.given) if os.path.isdir(self.given) else os.path.basename(path)).replace(os.sep, '/')

    @staticmethod
    def _files(given):
        exts = ('.bprelease', '.bpprocess', '.bpobject')
        if os.path.isfile(given):
            return [given]
        hits = []
        for dp, dns, fns in os.walk(given):
            for f in sorted(fns):
                if f.lower().endswith(exts):
                    hits.append(os.path.join(dp, f))
            if dp[len(given):].count(os.sep) > 2:
                dns[:] = []
        if not hits:
            sys.exit(f"{given}: no .bprelease / .bpprocess / .bpobject file found")
        return hits

    def _add(self, child, path=''):
        kind = child.tag
        if kind in ('process', 'object'):
            inner = next((x for x in child if x.tag in ('process', 'object')), None)
            if inner is None:
                inner = child
            self.procs.append(Proc(child, inner, self, path))
            return
        entry = {'name': child.get('name'), 'id': child.get('id'), 'attrs': {k: v for k, v in child.attrib.items() if k not in ('id', 'name')}}
        if kind == 'webapiservice':
            entry['service'] = web_api_service(child)
            self.items[kind].append(entry)
            return
        if kind == 'calendar':
            entry['calendar'] = calendar(child)
            self.items[kind].append(entry)
            return
        for sub in child:
            if sub.tag == 'members':
                entry['members'] = [(m.tag, m.get('id')) for m in sub]
            elif len(sub) == 0:
                entry[sub.tag] = (sub.text or '').strip()
            else:
                entry[sub.tag] = re.sub(r'\s+', ' ', ET.tostring(sub, encoding='unicode'))
        if kind == 'environment-variable' and (SENSITIVE.search(entry.get('name') or '') or entry['attrs'].get('type') == 'password'):
            entry['attrs']['value'] = '***'
        if kind == 'credential':
            for k in list(entry.keys()):
                if SENSITIVE.search(k):
                    entry[k] = '***'
        self.items[kind].append(entry)

    def _place_members(self, contents, f, queues):
        """The groups of release `f` set on the processes, objects and work queues they list: `{path, default}` in the
        groups' order, `path` the group's name as written (its full path, `/` between levels) and `default` its
        `isDefaultGroup` flag. A member names its item by id only - a GUID, a work queue's release id - so it is
        resolved by id among the items of the same release; `queues` are the work-queue entries `f` added."""
        procs = {p.guid: p for p in self.procs if p.path == f and p.guid}
        by_queue = {q['id']: q for q in queues if q.get('id')}
        for q in queues:
            q['groups'] = []
        for g in contents:
            if not g.tag.endswith('-group'):
                continue
            ms = {'path': g.get('name'), 'default': (g.get('isDefaultGroup') or '').lower() == 'true'}
            for m in g.findall('members/*'):
                if m.tag == 'work-queue':
                    if m.get('id') in by_queue:
                        by_queue[m.get('id')]['groups'].append(dict(ms))
                elif m.get('id') in procs:
                    procs[m.get('id')].groups.append(dict(ms))

    def _secrets(self):
        """Literal values that are secrets: a literal assigned into a password-typed or password-named data item, a
        literal initial value of such an item. Blue Prism encrypts password initial values, so this is a safety net."""
        out = set()
        for p in self.procs:
            sens = {s.name for s in p.stages.values() if s.type == 'Data' and s.is_sensitive_target()}
            for s in p.stages.values():
                if s.type == 'Data' and s.is_sensitive_target() and s.initialvalue and len(s.initialvalue) > 3 and s.initialvalue not in FLAG_LITERALS:
                    out.add(s.initialvalue)
                for e, t in ([s.calc] if s.calc else []) + s.calcs:
                    if t in sens:
                        for lit in re.findall(r'"([^"]{4,})"', e or ''):
                            if lit not in FLAG_LITERALS:
                                out.add(lit)
            for e in p.items_env() if hasattr(p, 'items_env') else []:
                pass
        for ev in self.items.get('environment-variable', []):
            pass
        return out

    def safe(self, s, name=''):
        if not isinstance(s, str):
            return s
        if SENSITIVE.search(name or '') and s and not s.startswith('[') and s.strip().strip('"') not in FLAG_LITERALS:
            return '***'
        for sec in self.secrets:
            if sec in s:
                s = s.replace(sec, '***')
        s = JWT_LITERAL.sub('***', s)
        return BEARER_LITERAL.sub(lambda m: f"{m.group(1)} ***", s)

    def object_by_name(self, name):
        return self.by_name_ci.get((name or '').lower())

    def _edges(self):
        out = []
        web_apis = {(w['name'] or '').lower() for w in self.items.get('webapiservice', [])}
        for p in self.procs:
            for s in p.stages.values():
                if s.type == 'Action' and s.resource:
                    obj, act = s.resource
                    callee = self.object_by_name(obj)
                    out.append({'caller': p, 'stage': s, 'kind': 'object', 'object': obj, 'action': act, 'callee': callee,
                                'internal': internal_family(obj)
                                or ('Web API' if (obj or '').lower() in web_apis else None)})
                elif s.type == 'Process' and s.processid:
                    callee = self.by_guid.get(s.processid)
                    out.append({'caller': p, 'stage': s, 'kind': 'process', 'object': callee.name if callee else s.processid, 'action': None, 'callee': callee, 'internal': None})
                elif s.type == 'SubSheet' and s.processid:
                    out.append({'caller': p, 'stage': s, 'kind': 'page', 'object': p.page_name(s.processid), 'action': None, 'callee': p, 'internal': None})
        return out

    def _queues(self):
        """Work queues: the release's definitions plus every queue name a process names in a queue action. Blue Prism
        queue names are not case-sensitive, so a process spelling the queue differently from its definition is the same
        queue (recorded under `spellings`)."""
        q = collections.OrderedDict()
        for wq in self.items.get('work-queue', []):
            q[wq['name'].lower()] = {'id': int(wq['id']) if (wq.get('id') or '').isdigit() else stable_id('work-queue|' + wq['name'].lower()), 'name': wq['name'],
                                     'keyField': wq['attrs'].get('key-field'), 'maxAttempts': wq['attrs'].get('max-attempts'), 'defined': True, 'usedBy': [],
                                     'actions': collections.Counter(), 'spellings': [wq['name']], 'groups': wq.get('groups', [])}
        for e in self.edges:
            if e['internal'] == 'Work Queues':
                for i in e['stage'].inputs:
                    if i['name'] == 'Queue Name' and i['expr']:
                        name = e['caller'].name_of(i['expr'])
                        key = name.lower()
                        if key not in q:
                            q[key] = {'id': stable_id('work-queue|' + key), 'name': name, 'keyField': None, 'maxAttempts': None, 'defined': False, 'usedBy': [],
                                      'actions': collections.Counter(), 'spellings': [name], 'groups': []}
                        if name not in q[key]['spellings']:
                            q[key]['spellings'].append(name)
                        u = f"{e['caller'].name} ({e['caller'].id})"
                        if u not in q[key]['usedBy']:
                            q[key]['usedBy'].append(u)
                        q[key]['actions'][e['action']] += 1
        return q

    # ----- rendering helpers -----
    def stage_tag(self, s):
        """` #<short id>` after the name of a stage whose name another flow stage of its page carries, so that a goto
        names the stage it reaches; nothing for a name the page holds once."""
        key = (id(s.proc), s.page)
        if key not in self.tags:
            stages = s.proc.page_stages(s.page)
            rep, sid = repeated_names(stages), short_ids([t.id for t in stages])
            self.tags[key] = {t.id: f" #{sid[t.id]}" for t in stages if t.name in rep}
        return self.tags[key].get(s.id, '')

    def resource_label(self, obj, act):
        fam = internal_family(obj)
        if fam and fam != 'internal':
            return f"{fam}.{act}"
        if fam:
            return f"{obj.split('.')[-1]}.{act}"
        return f"{obj}.{act}"

    def fmt_inputs(self, s, p):
        """`name=value` per input given, then `blank: <names>` for the inputs the call leaves blank - whether the
        callee's item is blanked or keeps its value is undocumented, so a blank is shown, not dropped."""
        parts = [f"{i['name']}={expr(self.safe(i['expr'], i['name']))}" for i in s.inputs if i['expr']]
        blank = [i['name'] for i in s.inputs if not i['expr']]
        return ', '.join(parts) + (('; ' if parts else '') + 'blank: ' + ', '.join(blank) if blank else '')

    def fmt_outputs(self, s):
        return ', '.join(f"{o['name']}→[{o['stage']}]" for o in s.outputs if o['stage'])

    def fmt_element(self, p, eid, eparams=None):
        e = p.element_by_id.get(eid) if eid else None
        name = (e.name + p.element_tags.get(eid, '')) if e else (f"<element {eid}>" if eid else '<no element>')
        tech = f"/{e.type}" if e and e.type else ''
        ep = ''
        if eparams:
            ep = ' {' + ', '.join(f"{k}{'~' + v['comparisontype'] if v['comparisontype'] not in (None, '', 'Equal') else ''}={expr(self.safe(v['expression'] or ''))}"
                                  for k, v in eparams.items()) + '}'
        return f"«{name}»{tech}{ep}"

    def render(self, s, p, full=False):
        t = s.type
        tag = self.stage_tag(s)
        nar = f"  # {oneline(self.safe(s.narrative))}" if s.narrative else ''
        if t == 'Start':
            ins = ', '.join(fmt_param(self, i, '→') for i in s.inputs)
            pre = [oneline(self.safe(c)) for c in s.preconditions if c]
            post = [oneline(self.safe(c)) for c in s.postconditions if c]
            return f"START({ins})" + (f" pre[{'; '.join(pre)}]" if pre else '') + (f" post[{'; '.join(post)}]" if post else '') + f"{fmt_name(s, 'Start')}{tag}{nar}"
        if t == 'End':
            outs = ', '.join(fmt_param(self, o, '←') for o in s.outputs)
            return f"END({outs}){fmt_name(s, 'End')}{tag}{nar}"
        if t == 'Action':
            obj, act = s.resource or ('?', '?')
            if ((obj or '').lower(), act, s.narrative) in self.stock_narratives:
                nar = ''  # the vendor's description of an internal action, copied into every call, not the author's note
            return f"ACTION {self.resource_label(obj, act)}({self.fmt_inputs(s, p)}) → {self.fmt_outputs(s) or '-'}  ‹{s.name}›{tag}{nar}"
        if t == 'Calculation':
            e, tgt = s.calc or ('', '')
            return f"CALC [{tgt}] = {expr(self.safe(e, tgt))}  ‹{s.name}›{tag}{nar}"
        if t == 'MultipleCalculation':
            body = '; '.join(f"[{tgt}] = {expr(self.safe(e, tgt))}" for e, tgt in s.calcs)
            return f"MULTICALC {body}  ‹{s.name}›{tag}{nar}"
        if t == 'Decision':
            return f"DECISION {expr(self.safe(s.decision or ''))}  ‹{s.name}›{tag}{nar}"
        if t == 'ChoiceStart':
            return f"CHOICE ‹{s.name}›{tag}{nar}"
        if t == 'ChoiceEnd':
            return f"otherwise ‹{s.name}›{tag}"
        if t == 'LoopStart':
            return f"LOOP {s.looptype or 'ForEach'} [{s.loopdata}]  ‹{s.name}›{tag}{nar}"
        if t == 'LoopEnd':
            return f"END LOOP ‹{s.name}›{tag}"
        if t == 'SubSheet':
            return f"PAGE «{p.page_name(s.processid)}»({self.fmt_inputs(s, p)}) → {self.fmt_outputs(s) or '-'}  ‹{s.name}›{tag}{nar}"
        if t == 'Process':
            callee = self.by_guid.get(s.processid)
            head = f"`{callee.name}` ({callee.id})" if callee else f"UNRESOLVED process {s.processid}"
            return f"PROCESS {head}({self.fmt_inputs(s, p)}) → {self.fmt_outputs(s) or '-'}  ‹{s.name}›{tag}{nar}"
        if t == 'Exception':
            ex = s.exception or {}
            shot = ' [screen capture]' if ex.get('savescreencapture') == 'yes' else ''
            if ex.get('usecurrent') == 'yes':
                return f"RETHROW{shot}  ‹{s.name}›{tag}{nar}"
            return f"THROW type={written(ex.get('type') or '(none)', py=True)} detail={expr(self.safe(ex.get('detail') or ''))}{shot}  ‹{s.name}›{tag}{nar}"
        if t == 'Recover':
            scope = f"block «{s.block.name}» {block_holding(p, s.block)}" if s.block else 'whole page'
            return f"RECOVER (scope: {scope})" + (f" attempts={s.attempts}" if s.attempts else '') + f"  ‹{s.name}›{tag}{nar}"
        if t == 'Resume':
            return f"RESUME ‹{s.name}›{tag}"
        if t == 'Note':
            return f"# {oneline(self.safe(s.narrative))}"
        if t == 'Anchor':
            return None
        if t == 'Navigate':
            parts = []
            for stp in s.steps:
                args = ', '.join(f"{k}={expr(self.safe(v, k))}" for k, v in stp['args'].items() if v)
                outs = ', '.join(f"{k}→[{visible(v)}]" for k, v in stp['outputs'].items() if v)
                parts.append(f"{self.fmt_element(p, stp['element'], stp['elementparameters'])}.{stp['action']}({args})" + (f" → {outs}" if outs else ''))
            iv = f" interval={s.interval}" if s.interval else ''
            return f"NAVIGATE {' ; '.join(parts)}{iv}  ‹{s.name}›{tag}{nar}"
        if t == 'Read':
            parts = []
            for stp in s.steps:
                args = ', '.join(f"{k}={expr(self.safe(v, k))}" for k, v in stp['args'].items() if v)
                parts.append(f"{self.fmt_element(p, stp['element'], stp['elementparameters'])}.{stp['action']}({args}) → [{stp['stage']}]")
            return f"READ {' ; '.join(parts)}  ‹{s.name}›{tag}{nar}"
        if t == 'Write':
            parts = []
            for stp in s.steps:
                e = p.element_by_id.get(stp['element'])
                args = ', '.join(f"{k}={expr(self.safe(v, k))}" for k, v in stp['args'].items() if v)
                parts.append(f"{self.fmt_element(p, stp['element'], stp['elementparameters'])} := {expr(self.safe(stp['expr'] or '', (e.name if e else '') + (e.type or '' if e else '')))}"
                             + (f" via {stp['action']}({args})" if stp['action'] else ''))
            return f"WRITE {' ; '.join(parts)}  ‹{s.name}›{tag}{nar}"
        if t == 'WaitStart':
            return f"WAIT up to {s.timeout or '?'} s  ‹{s.name}›{tag}{nar}"
        if t == 'WaitEnd':
            return f"timeout ‹{s.name}›{tag}"
        if t == 'Code':
            # a code stage's parameters are its own definition: their types are part of it
            ins = ', '.join(f"{i['name']}:{i['type']}" + (f"={expr(self.safe(i['expr'], i['name']))}" if i['expr'] else '') for i in s.inputs)
            outs = ', '.join(f"{o['name']}:{o['type']}" + (f"→[{o['stage']}]" if o['stage'] else '') for o in s.outputs)
            lines = (s.code or '').count('\n') + 1 if s.code else 0
            lang = p.info.language if p.info and p.info.language else '?'
            head = f"CODE ({lang}, {lines} lines)({ins}) → {outs or '-'}  ‹{s.name}›{tag}{nar}"
            if full and s.code:
                body = '\n'.join('        | ' + self.safe(ln) for ln in s.code.splitlines())
                return head + '\n' + body
            ids = SAP_ID_IN_CODE.findall(s.code or '')
            if ids:
                head += f"  [SAP scripting ids in code: {len(ids)}]"
            return head
        # an Alert, or a stage type this renderer does not know: every value its XML holds, by its path in the stage
        return f"{t.upper()} ‹{s.name}›{tag} {', '.join(f'{k}={written(v)}' for k, v in stage_values(self, p, s.el))}{nar}"


# ----------------------------------------------------------------------------------------------------------------------
def oneline(s):
    """Free text (a narrative, a description) on one line: each line break as ` / `."""
    return (s or '').replace('\r\n', '\n').replace('\n', ' / ')


def expr(s):
    """An expression on one line: each line break as ` ⏎ ` (` / ` would read as a division)."""
    return (s or '').replace('\r\n', '\n').replace('\n', ' ⏎ ')


def visible(s):
    """`s` with every character a reader cannot see - line breaks, tabs, format characters, spaces other than U+0020 -
    written as \\uXXXX: each is part of the value, and a line break printed as it is would split the rendering's line.
    A backslash before `u` and four hex digits is written \\u005C, so that every \\uXXXX shown is one character."""
    return INVISIBLE.sub(lambda m: f"\\u{ord(m.group()):04X}" if m.group() == '\\' or unicodedata.category(m.group())[0] in 'CZ' else m.group(), s)


def written(o, py=False):
    """A value as the text renderings print it: a text in quotes as written, `visible` - Python's repr and JSON escape
    every backslash and quote (`C:\\A` reads `C:\\\\A`), and a reader copies the escaped form - and the base64 bytes
    it carries by their size (`sized`); a list or a mapping laid out as JSON prints it (`py`: as Python prints it, texts in
    single quotes), each item written alike. The JSON outputs keep JSON's escapes and every value whole: a JSON reader
    decodes them."""
    if isinstance(o, str):
        q = "'" if py else '"'
        return f"{q}{visible(sized(o))}{q}"
    if isinstance(o, dict):
        return '{' + ', '.join(f"{written(k, py)}: {written(v, py)}" for k, v in o.items()) + '}'
    if isinstance(o, list):
        return '[' + ', '.join(written(v, py) for v in o) + ']'
    if o is None or isinstance(o, bool):
        return repr(o) if py else json.dumps(o)
    return str(o)


def fmt_param(x: Export, prm, arrow):
    """A page parameter as its Start or End stage defines it, or a call binds it: `name:type`, `→[data item]` (`←` for
    an output), and the author's description."""
    t = f"{prm['name']}:{prm['type']}"
    if prm['stage']:
        t += f"{arrow}[{prm['stage']}]"
    if prm['narrative']:
        t += ' ' + written(oneline(x.safe(prm['narrative'])))
    return t


def fmt_name(s, default):
    """A Start or End stage's name, when the author changed it from the default."""
    return '' if s.name == default else f"  ‹{s.name}›"


def scope_flags(s):
    """A data item's scope as the stage sets it: `[shared]` - visible to every page (no `private`); `[no reset]` - keeps
    its value when the page runs again (no `alwaysinit`)."""
    return ('' if s.private else ' [shared]') + ('' if s.alwaysinit else ' [no reset]')


def data_item_text(x: Export, d):
    iv = '***' if (d.is_sensitive_target() and (d.initialvalue or d.encrypted)) else x.safe(d.initialvalue or '', d.name)
    return f"{d.name}:{d.datatype}" + (f"={written(iv)}" if iv else '') + (f" <{d.exposure} variable>" if d.exposure else '') + scope_flags(d)


def fields_text(tree):
    """`name:type`, a nested collection's own fields after it in brackets."""
    return ', '.join((f"{n}:{t}" if t else n) + (f"[{fields_text(kids)}]" if kids else '') for n, t, kids in tree)


def collection_text(c):
    tree = c.field_tree or [(f, t, []) for f, t in c.fields]
    return f"{c.name}" + (f"[{fields_text(tree)}]" if tree else '') + (' [single row]' if c.singlerow else '') \
        + (f" <{c.exposure} variable>" if c.exposure else '') + scope_flags(c)


def case_line(x: Export, p: Proc, s, ch):
    """The head of one branch of a Choice (`case «name» when <expression>`) or a Wait (`when «name»: <element>.<condition>(<arguments>) <comparison> <reply>`).
    A branch name is printed `visible`: an author can type a line break into one."""
    if s.type == 'ChoiceStart':
        return f"case «{visible(ch['name'])}» when {expr(x.safe(ch['expression'] or ''))}:"
    cond = f"{x.fmt_element(p, ch['element'], ch['elementparameters'])}.{ch['condition']}"
    args = ', '.join(f"{k}={expr(x.safe(v, k))}" for k, v in ch['args'].items() if v)
    cmp_ = f" {ch['comparetype'] or 'Equal'} {expr(x.safe(ch['reply'] or ''))}" if ch['reply'] not in (None, '', 'True') or (ch['comparetype'] not in (None, 'Equal')) else ''
    return f"when «{visible(ch['name'])}»: {cond}({args}){cmp_}:"


def dump_page(x: Export, p: Proc, page_id, full=False):
    L = []
    pr = L.append
    stages = p.page_stages(page_id)
    name = p.page_name(page_id)
    sheet = p.sheet_by_id.get(page_id) if page_id else None
    info = next((s for s in stages if s.type == 'SubSheetInfo'), None)
    pr(f"\n=== PAGE «{name}»" + (f" [{sheet['type']}]" if sheet and sheet['type'] != 'Normal' else '') + (f" published={sheet['published']}" if sheet and sheet.get('published') not in (None, 'False') else '')
       + REACH_MARK[x.reach.page(p, page_id)])
    if info and info.narrative:
        pr("  description: " + oneline(x.safe(info.narrative)))
    data = [s for s in stages if s.type == 'Data']
    colls = [s for s in stages if s.type == 'Collection']
    if data:
        pr(f"  data items ({len(data)}): " + '; '.join(data_item_text(x, d) for d in data))
    if colls:
        pr(f"  collections ({len(colls)}): " + '; '.join(collection_text(c) for c in colls))
        for c in colls:  # every row, a blank one as {}: the count is behaviour (a loop's turns, a blank read instead of an error)
            if c.rows:
                pr(f"  initial rows of [{c.name}] ({len(c.rows)}): " + '; '.join(written({k: x.safe(v, k) for k, v in r.items() if v not in (None, '')}) for r in c.rows))
    floating = [s for s in stages if s.type == 'Note' and not s.onsuccess]
    for n in floating:
        pr(f"  {x.render(n, p)}")
    visited = set()
    by_group = collections.defaultdict(list)
    for s in stages:
        if s.groupid:
            by_group[s.groupid].append(s)

    def partner(s, want):
        return next((o for o in by_group.get(s.groupid, []) if o.type == want and o is not s), None)

    def walk(sid, depth, stop_at=None):
        ind = '  ' * depth
        while sid:
            s = p.stages.get(sid)
            if s is None:
                pr(f"{ind}→ (link to a missing stage {sid})")
                return
            if s.type == 'Anchor':
                sid = s.onsuccess
                continue
            if s.id in visited:
                pr(f"{ind}→ goto ‹{s.name}› [{s.type}]{x.stage_tag(s)}")
                return
            visited.add(s.id)
            if s.type == 'LoopEnd':
                pr(f"{ind}END LOOP ‹{s.name}›{x.stage_tag(s)}" + ('' if stop_at == s.groupid else ' (loop end reached from outside its loop)'))
                return
            line = x.render(s, p, full)
            if line is not None:
                pr(f"{ind}{line}")
            t = s.type
            if t == 'Decision':
                pr(f"{ind}  true:")
                walk(s.ontrue, depth + 2, stop_at)
                pr(f"{ind}  false:")
                walk(s.onfalse, depth + 2, stop_at)
                return
            if t == 'ChoiceStart':
                for ch in s.choices:
                    pr(f"{ind}  {case_line(x, p, s, ch)}")
                    walk(ch['ontrue'], depth + 2, stop_at)
                end = partner(s, 'ChoiceEnd')
                pr(f"{ind}  {x.render(end, p) if end else 'otherwise'}:")
                if end:
                    visited.add(end.id)
                    walk(end.onsuccess, depth + 2, stop_at)
                return
            if t == 'WaitStart':
                for ch in s.choices:
                    pr(f"{ind}  {case_line(x, p, s, ch)}")
                    walk(ch['ontrue'], depth + 2, stop_at)
                end = partner(s, 'WaitEnd')
                pr(f"{ind}  {x.render(end, p) if end else 'timeout'}:")
                if end:
                    visited.add(end.id)
                    walk(end.onsuccess, depth + 2, stop_at)
                return
            if t == 'LoopStart':
                pr(f"{ind}  body:")
                walk(s.onsuccess, depth + 2, s.groupid)
                end = partner(s, 'LoopEnd')
                if end and end.id not in visited:  # a body that ends or leaves before it loops
                    pr(f"{ind}  END LOOP ‹{end.name}›{x.stage_tag(end)} (not reached from the loop body)")
                if end:
                    visited.add(end.id)
                    sid = end.onsuccess
                    continue
                return
            if t == 'ChoiceEnd':
                sid = s.onsuccess
                continue
            if t == 'WaitEnd':
                sid = s.onsuccess
                continue
            if t in ('End', 'Exception'):
                return
            sid = s.onsuccess

    start = p.start_of(page_id)
    if start:
        walk(start.id, 1)
    else:
        pr("  (no Start stage on this page)")
    def entered(r):
        """A Recover runs when a stage it covers raises - a stage in its Block, or with no Block any stage of the page -
        so it runs only once such a stage is reached."""
        return any(s.id in visited and s.type not in ('Note', 'Anchor', 'Recover') and s.type in FLOW_TYPES and (r.block is None or r.block in s.blocks)
                   for s in stages)
    while True:
        r = next((s for s in stages if s.type == 'Recover' and s.id not in visited and entered(s)), None)
        if r is None:
            break
        pr("  --- recovery ---")
        walk(r.id, 1)
    dead = [s for s in stages if s.id not in visited and s.type in FLOW_TYPES and s.type not in ('Anchor',) and not (s.type == 'Note' and not s.onsuccess)]
    if dead:  # never run, but read like a disabled block: a superseded flow often explains a value the live one carries
        pr(f"  unlinked stages ({len(dead)}, reached by no link from Start nor from a Recover a reached stage can enter - never run):")
        for s in dead:
            line = x.render(s, p, full)
            pr(f"    [{s.type}] {line}" if line is not None else f"    [{s.type}] ‹{s.name}›")
            if s.type in ('ChoiceStart', 'WaitStart'):
                for ch in s.choices:
                    pr(f"      {case_line(x, p, s, ch)}")
    return '\n'.join(L)


def dump(x: Export, which, full=False):
    page = None
    if '::' in which:
        which, page = which.split('::', 1)
    p, err = find_proc(x, which)
    if err:
        return err
    L = []
    pr = L.append
    pr(f"# `{p.name}` ({p.id}) {p.kind}  guid={p.guid} version={p.version} bpversion={p.bpversion} published={p.published} runmode={p.runmode or '-'}"
       + (f" shared={p.shared}" if p.shared else '') + f" pages={len(p.sheets) + 1} stages={len(p.stages)} groups={written(p.groups)}")
    if p.copies:
        pr(f"COPIES: read from {x.rel(p.path)} created {x.created.get(p.path)}, the release created last; also in " + '; '.join(copy_text(x, p, c) for c in p.copies))
    if p.narrative:
        pr(f"DESCRIPTION: {oneline(x.safe(p.narrative))}")
    pre = [oneline(x.safe(c)) for c in p.preconditions if c]
    if pre:
        pr(f"PRECONDITIONS: {'; '.join(pre)}")
    if p.endpoint:
        pr(f"ENDPOINT: {oneline(x.safe(p.endpoint))}")
    if p.info is not None and (p.info.language or p.info.references):
        # the code is on the main page's ProcessInfo stage: a dump of another page points there instead of repeating it
        main = p.page_name(None)
        own = not page or page.lower() == main.lower()
        pr(f"CODE: language={p.info.language} references={', '.join(p.info.references)} imports={', '.join(p.info.imports)} globalcode={len(p.info.globalcode or '')} chars"
           f" object-code={len(p.info.code or '')} chars" + ('' if full and own else ' (--full prints them)' if own else f' (dump "{p.id}::{main}" --full prints them)'))
        for label, body in (('global code', p.info.globalcode), ('object code', p.info.code)):
            if full and own and body:
                pr(f"  {label}:")
                pr('  | ' + '\n  | '.join(x.safe(ln) for ln in body.splitlines()))
    if p.apptype:
        pr(f"APPLICATION MODEL: {p.apptype} {written({k: v for k, v in p.appparams.items() if v})} elements={len(p.elements)}" + (f" inherits={p.inherits}" if p.inherits else ''))
    env = p.env_items()
    if env:
        pr("ENVIRONMENT / SESSION VARIABLES: " + ', '.join(f"{s.name} ({s.exposure})" for s in env))
    for pid, name, ptype in p.pages():
        if page and name.lower() != page.lower():
            continue
        L.append(dump_page(x, p, pid, full))
    return '\n'.join(L)


# ----------------------------------------------------------------------------------------------------------------------
# raw: the release's own XML, for checking a reading against the source rather than against a rendering. Every
# rendering above is this script's reading of the stages; a finding that rests on one (a link that goes nowhere, an
# exception that is not rethrown, an input never passed) is confirmed here, where no renderer chose what to show.
IMAGE_VALUE = re.compile(r'^(\d+,\d+,\d+,)([A-Za-z0-9+/=\s]{300,})$')  # `<width>,<height>,<size>,` then the image's bytes
# one line, or lines wrapped at one width (a certificate, a MIME part); `=` only as the closing padding: `key=<bytes>` keeps its `key=`
BASE64_RUN = re.compile(r'(?:[A-Za-z0-9+/]{60,120}\r?\n)+[A-Za-z0-9+/]{1,120}={0,2}(?![A-Za-z0-9+/])|[A-Za-z0-9+/]{300,}={0,2}')
PATH_START = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://|[A-Za-z]:[\\/]|[\\/]{2}')  # a URL, a drive path, a UNC path
TOKEN_EDGE = re.compile(r'[\s"\'()<>=,]')
BASE64_MIN_DISTINCT = 40  # of the 64: a random run of 300 holds about 63; a path, an identifier or a hexadecimal key about 20


def sized(s):
    """`s` with the base64 bytes it carries as their size: an image value after its head, and every run of 300 or more
    base64 characters that reads as encoded bytes (`base64_size`) - the whole value, a data URI's bytes, a field of a
    body template. A long text, path, identifier or list is a value, printed whole."""
    m = IMAGE_VALUE.match(s)
    if m:
        return f"{m.group(1)}<{len(m.group(2))} base64 characters>"
    return BASE64_RUN.sub(lambda r: base64_size(r.group(), TOKEN_EDGE.split(s[:r.start()])[-1]) or r.group(), s)


def base64_size(run, before):
    """`<N base64 characters>` for a run that reads as encoded bytes, else None: 300 characters or more, its lines but
    the last of one width and the last no longer, `BASE64_MIN_DISTINCT` of the 64 characters or more, and no part of a
    URL or a path - the run with the text before it back to a space, quote, bracket, `=` or `,` (`before`) does not
    start with a scheme's `://`, a drive or `//`."""
    lines = re.split(r'\r?\n', run)
    chars = ''.join(lines)
    if len(chars) < 300 or len({len(ln) for ln in lines[:-1]}) > 1 or len(lines[-1]) > len(lines[0]):
        return None
    if len(set(chars) - {'='}) < BASE64_MIN_DISTINCT or PATH_START.match(before + run):
        return None
    return f"<{len(chars)} base64 characters>"


STAGE_LAYOUT = {'display', 'displayx', 'displayy', 'displaywidth', 'displayheight', 'font', 'loginhibit', 'subsheetid', 'onsuccess', 'narrative'}


def stage_values(x: 'Export', p: 'Proc', el):
    """(path, value) for every attribute and text inside a stage's XML but its layout, links and narrative - the
    rendering of a stage type no renderer reads field by field - masked as `raw` masks it."""
    c = raw_mask(x, p, copy.deepcopy(el))

    def walk(e, path):
        for k, v in e.attrib.items():
            if path and v not in (None, ''):
                yield f"{path}@{k}", v
        if path and (e.text or '').strip():
            yield path, e.text.strip()
        for ch in e:
            if not path and ch.tag in STAGE_LAYOUT:
                continue
            yield from walk(ch, f"{path}.{ch.tag}" if path else ch.tag)
    return list(walk(c, ''))


def copy_text(x: Export, p: Proc, c):
    """Another file's copy of `p`: where, its release's creation date, its name when it differs, same or not."""
    return f"{x.rel(c['file'])} created {c['created']}" + (f" as `{c['name']}`" if c['name'] != p.name else '') + (': same' if c['same'] else ': differs')


def find_proc(x: Export, which):
    """(process or object, None) for a six-digit id, a GUID, or a name that occurs once; (None, message) otherwise - a
    repeated name lists the copies instead of taking one."""
    w = str(which).strip()
    hit = next((p for p in x.procs if str(p.id) == w or p.guid == w), None)
    if hit:
        return hit, None
    hits = x.by_name.get(w) or [p for p in x.procs if p.name.lower() == w.lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"no process or object with id, GUID or name {which!r}; names: {written([f'{q.name} ({q.id})' for q in x.procs], py=True)}"
    return None, f"name {which!r} occurs {len(hits)} times - look it up by id: " + '; '.join(f"{q.id} {q.kind} guid={q.guid}" for q in hits)


def raw_mask(x: Export, p: Proc, el):
    """Mask a copy of a stage or element in place, as the renderings mask: the initial value of a password data item,
    a value under a password-named input, target, argument, field or attribute, and every harvested secret anywhere.
    The base64 bytes a value carries are replaced by their size (`sized`) first, so the rest of the value is masked like
    any other."""
    def txt(s, name=''):
        if not s:
            return s
        return x.safe(sized(s), name)
    dtype = el.findtext('datatype') if el.tag == 'stage' else None
    for v in el.iter('initialvalueenc'):  # a password's encrypted initial value
        if (v.text or '').strip():
            v.text = '***'
    for v in el.iter('field'):  # a password cell's encrypted value in a collection's initial row
        if v.get('encvalue'):
            v.set('encvalue', '***')
    if el.tag == 'stage' and el.get('type') == 'Data' and sensitive_item(dtype, el.get('name')):
        for v in el.iter('initialvalue'):
            if (v.text or '').strip():
                v.text = '***'
    for e in el.iter():
        name = {'input': e.get('name'), 'calculation': e.get('stage'), 'field': e.get('name')}.get(e.tag, '')
        if e.tag == 'step' and e.get('expr'):  # a Write: the value is named by the element it is written into
            target = e.find('element')
            ref = p.element_by_id.get(target.get('id') if target is not None else '')
            name = (ref.name + (ref.type or '')) if ref else ''
        for k, v in list(e.attrib.items()):
            e.set(k, txt(v, name if k in ('expr', 'expression', 'value') else ''))
        if e.tag == 'argument' and e.find('value') is not None:  # before the loop reaches the value itself
            e.find('value').text = txt(e.find('value').text, e.findtext('id') or '')
        e.text = txt(e.text)
    for att in el.iter('attribute'):  # an application-model attribute: its value is named by the attribute
        for pv in att.findall('ProcessValue'):
            if pv.get('value'):
                pv.set('value', x.safe(pv.get('value'), att.get('name') or ''))
    return el


def is_window_container(el):
    """A region container that is the window a region spy captured: it has a type, attributes and an id stages name."""
    return el.tag == 'region-container' and el.find('type') is not None


def raw_elements(el, path=()):
    """(element, region or window container, path) for every application-model declaration below `el` (itself
    included when it is one): children directly and through groups at any depth and untyped region containers - the
    tree `Proc._walk_model` reads, walked afresh."""
    if el is None:
        return
    if el.tag in ('element', 'region') or is_window_container(el):
        path = path + (el.get('name') or '',)
        yield el, list(path)
    for k in el:
        if k.tag in ('element', 'region') or is_window_container(k):
            yield from raw_elements(k, path)
        elif k.tag in ('group', 'region-container'):
            yield from raw_elements(k, path + (k.get('name') or k.findtext('name') or '',))


def raw_children(el):
    """The elements, regions and window containers directly below `el`, through its groups and untyped region
    containers too."""
    out = []
    for k in el:
        if k.tag in ('element', 'region') or is_window_container(k):
            out.append(k)
        elif k.tag in ('group', 'region-container'):
            out += raw_children(k)
    return out


def raw_xml(x: Export, p: Proc, el, drop=()):
    c = copy.deepcopy(el)
    for tag in drop:  # nested model elements are objects of their own, looked up by their id
        for sub in c.findall(tag):
            c.remove(sub)
    raw_mask(x, p, c)
    ET.indent(c, space='  ')
    xml = re.sub(r'&lt;(\d+ base64 characters)&gt;', r'<\1>', ET.tostring(c, encoding='unicode'))  # the size marker is the script's, printed as written
    return '\n'.join('    ' + ln for ln in xml.splitlines())


def raw(x: Export, which, stage=None, find=None, element=None):
    """(text, exit code): the stages of one process or object (a page with `<name>::<page>`) named or holding a text,
    and application-model elements, as the export holds them - read afresh from the file, namespaces left out,
    secrets masked. A targeted lookup, never a reading of a whole process."""
    page = None
    if '::' in which:
        which, page = which.split('::', 1)
    p, err = find_proc(x, which)
    if err:
        return err, 2
    copies = []
    names = {p.name, *(c['name'] for c in p.copies)}
    for f in [p.path, *(c['file'] for c in p.copies)] if p.path else x.files:
        root = parse_xml(f)
        for w in root.iter():
            if w.tag in ('process', 'object') and w.get('id') == p.guid and (w.get('name') or '') in names:
                inner = next((c for c in w if c.tag in ('process', 'object')), w)
                if not any(inner is c for _, c in copies):
                    copies.append((f, inner))
        if root.tag == 'process' and not root.get('id') and root.get('name') == p.name:
            copies.append((f, root))
    if not copies:
        return f"`{p.name}` ({p.id}) not found afresh in {x.files}", 2
    L = [f"# raw `{p.name}` ({p.id}) {p.kind}  guid={p.guid}  (the export's own XML, every element and attribute; namespaces left out, secrets masked)"]
    code, shown = 0, 0
    for f, inner in copies:
        if len(copies) > 1:
            L.append(f"## copy in {x.rel(f)}" + (f" (release created {x.created[f]}{', the copy read' if f == p.path else ''})" if x.created.get(f) else ''))
        pages = {None: 'Main Page' if p.kind == 'process' else 'Initialise'}
        pages.update({s.get('subsheetid'): s.findtext('name') or '' for s in inner.findall('subsheet')})
        head = {k: x.safe(v) for k, v in inner.attrib.items()}
        L.append(f"{inner.tag.upper()} {written(head)}")
        L.append('PAGES ' + '; '.join(f"«{n}»" + (f" subsheetid={i}" if i else '') for i, n in pages.items()))
        if page and page.lower() not in (n.lower() for n in pages.values()):
            L.append(f"no page «{page}»")
            code = 2
            continue
        if stage or find:
            for st in inner.findall('stage'):
                pname = pages.get(st.findtext('subsheetid'), f"<page {st.findtext('subsheetid')}>")
                if page and pname.lower() != page.lower():
                    continue
                if stage and stage not in (st.get('name'), st.get('stageid')) and (st.get('name') or '').lower() != stage.lower():
                    continue
                body = raw_xml(x, p, st)
                if find and find not in body:
                    continue
                L.append(f"«{pname}» ‹{st.get('name')}› [{st.get('type')}] stageid={st.get('stageid')}")
                L.append(body)
                shown += 1
        if element:
            appdef = inner.find('appdef')
            hits = [(e, path) for e, path in raw_elements(appdef) if element in (e.get('name'), e.findtext('id'))
                    or (e.get('name') or '').lower() == element.lower()]
            for e, path in hits:
                kids = raw_children(e)
                L.append(f"{e.tag.upper()} ‹{e.get('name')}› id={e.findtext('id')} path={' > '.join(path)}"
                         + ('  children: ' + ', '.join(f"{k.get('name')} ({k.findtext('id')})" for k in kids) if kids else ''))
                L.append(raw_xml(x, p, e, drop=('element', 'region', 'group', 'region-container')))
            if not hits:
                L.append(f"no application-model element named or numbered {element!r}")
                code = 2
    if stage and not shown:
        L.append(f"no stage named or numbered {stage!r}" + (f" holding {find!r}" if find else ''))
        code = 2
    elif find and not stage and not shown:
        L.append(f"no stage holding {find!r}")
    return '\n'.join(L), code


# ----------------------------------------------------------------------------------------------------------------------
def callees_in_order(x: Export, p: Proc):
    """Distinct callees (object.action, page, process) in the order the pages meet them (page order, then stage document
    order), each with the reach of the stages calling it: it runs when one of them does (`best`)."""
    out = collections.OrderedDict()
    for pid, _, _ in p.pages():
        for s in p.page_stages(pid):
            key = None
            if s.type == 'Action' and s.resource:
                key = ('action', x.resource_label(*s.resource))
            elif s.type == 'SubSheet':
                key = ('page', p.page_name(s.processid))
            elif s.type == 'Process':
                c = x.by_guid.get(s.processid)
                key = ('process', f"`{c.name}` ({c.id})" if c else f"UNRESOLVED {s.processid}")
            if key:
                out.setdefault(key, []).append(x.reach.stage(s))
    return [(k, v, best(st)) for (k, v), st in out.items()]


def internal_refs(x: Export, p: Proc, family, inputs, status=None):
    """(action, name) of each call of `p` to the internal object of that family, the name read from the inputs named:
    a credential and a calendar are named by text. `status`: only the stages with that reach (`Reach`)."""
    out = []
    for s in p.stages.values():
        if s.type == 'Action' and s.resource and internal_family(s.resource[0]) == family and (status is None or x.reach.stage(s) == status):
            for i in s.inputs:
                if i['name'] in inputs and i['expr']:
                    out.append((s.resource[1], p.name_of(i['expr'])))
    return out


def credential_refs(x: Export, p: Proc, status=None):
    return internal_refs(x, p, 'Credentials', ('Credentials Name', 'Name'), status)


def calendar_refs(x: Export, p: Proc, status=None):
    return internal_refs(x, p, 'Calendars', ('Calendar Name',), status)


def member_names(x: Export, members):
    """The names of a release item's members - a group's, a credential's - by the id each member gives: a process's
    or object's GUID, a work queue's release id; `kind:id` for an id the export does not hold."""
    out = []
    for mk, mid in members:
        pp = x.by_guid.get(mid)
        q = next((qq for qq in x.queues.values() if str(qq['id']) == str(mid)), None) if mk == 'work-queue' else None
        out.append(pp.name if pp else (q['name'] if q else f"{mk}:{mid}"))
    return out


# the Work Queues actions that name no queue: they address an item by its Item ID, and the item belongs to the queue it
# was taken, listed or added from. Those that change an item (tag and untag, set data, status, defer, complete, mark an
# exception, unlock) are told from the ones that only read it
ITEM_CHANGES = ('Tag Item', 'Untag Item', 'Set Data', 'Update Status', 'Defer', 'Mark Completed', 'Mark Exception', 'Unlock Item')
ITEM_ADDS = ('Add To Queue',)
ITEM_TAKES = ('Get Next Item',)
BY_ITEM_ID = '(by item id)'


def is_queue_stage(s):
    return s.type == 'Action' and bool(s.resource) and internal_family(s.resource[0]) == 'Work Queues'


def names_queue(s):
    return any(i['name'] == 'Queue Name' and i['expr'] for i in s.inputs)


def item_origins(p: Proc):
    """`origins(page, expression)`: where the value of a data item expression comes from, followed back through the
    outputs, calculations and page inputs that write it: a set of the stages that write it with an output, and of texts
    for what a trace cannot follow (`a caller's expression`, `a page input`, `a process input`, a calculation's type).
    A name is the page's own data item, else a shared one (pitfall 14); a page input is followed to what each caller of
    the page binds, within the process. An expression that does not start with a data item ends the trace at the
    Calculation that computes it, or at "a caller's expression" for a caller that binds it. An End stage's outputs name
    the data items it returns, which it does not write."""
    own = {(s.page, s.name): s.id for s in p.stages.values() if s.type in ('Data', 'Collection')}
    shared = {s.name: s.id for s in p.stages.values() if s.type in ('Data', 'Collection') and not s.private}

    def scope(page, name):
        return own.get((page, name)) or shared.get(name) or (page, name)
    writers = collections.defaultdict(list)  # data item -> (stage, how): None an output, a calculation's expression, a Start's input name
    for s in p.stages.values():
        for o in s.outputs if s.type != 'End' else []:
            if o['stage']:
                writers[scope(s.page, o['stage'])].append((s, None))
        for i in s.inputs if s.type == 'Start' else []:
            if i['stage']:
                writers[scope(s.page, i['stage'])].append((s, i['name']))
        for e, target in s.calcs + ([s.calc] if s.calc else []):
            if target:
                writers[scope(s.page, target)].append((s, e))

    def origins(page, ex, seen=frozenset()):
        m = re.match(r'\s*\[([^\].]+)', ex or '')
        key = scope(page, m.group(1)) if m else None
        if key is None or key in seen:
            return set()
        out = set()
        for s, how in writers.get(key, ()):
            if s.type == 'Start':
                bound = [(c.page, i['expr']) for c in p.stages.values() if c.type == 'SubSheet' and s.page and c.processid == s.page
                         for i in c.inputs if i['name'] == how and i['expr']]
                out |= set().union(*(origins(pg, e, seen | {key}) or {"a caller's expression"} for pg, e in bound)) or {'a page input' if s.page else 'a process input'}
            elif how is not None:
                out |= origins(s.page, how, seen | {key}) or {s.type}
            else:
                out.add(s)
        return out
    return origins


def queue_stage_queues(p: Proc):
    """For each Work Queues stage of `p` (by stage id), the names of the queues it acts on: the queue it names, else the
    queues of the stages its Item ID is followed back to (a Get Next Item, a listing, an Add To Queue, or an item-keyed
    stage that was itself followed). An empty list: the trace reaches no queue stage."""
    origins, memo = item_origins(p), {}

    def queues_of(s, active):
        if s.id in memo:
            return memo[s.id]
        if names_queue(s):
            found = [p.name_of(next(i['expr'] for i in s.inputs if i['name'] == 'Queue Name' and i['expr']))]
        else:
            item = next((i['expr'] for i in s.inputs if i['name'] == 'Item ID' and i['expr']), '')
            found = []
            for o in sorted((o for o in origins(s.page, item) if not isinstance(o, str) and is_queue_stage(o) and o.id not in active), key=lambda o: o.id):
                found += [n for n in queues_of(o, active | {s.id}) if n not in found]
        if not active:  # an answer cut short by a cycle is not kept
            memo[s.id] = found
        return found
    return {s.id: queues_of(s, frozenset()) for s in p.stages.values() if is_queue_stage(s)}


def queue_refs(x: Export, p: Proc, status=None, queues=None):
    """Queue actions per process, keyed by the resolved queue name. Get Next Item, Add To Queue, Get Pending Items ...
    name their queue; Update Status, Mark Completed, Mark Exception, Defer, Tag Item, Set Data ... address the item by
    its Item ID and belong to the queue the item was taken, listed or added from, which the Item ID is followed back to
    (`item_origins`) - one count under each queue it comes from. An Item ID the process cannot follow to a queue stage
    (it arrives as an input of the process, or from a calculation) is listed under `(by item id)`, never given to a
    queue by guess. `status`: only the stages with that reach (`Reach`); `queues`: `queue_stage_queues`, when the caller has it."""
    named, untraced = collections.OrderedDict(), collections.Counter()
    queues = queue_stage_queues(p) if queues is None else queues
    stages = [s for s in p.stages.values() if status is None or x.reach.stage(s) == status]
    for s in stages:  # the queues the actions name first, in the order the stages come
        if is_queue_stage(s) and names_queue(s):
            named.setdefault(queues[s.id][0], collections.Counter())[s.resource[1]] += 1
    for s in stages:
        if is_queue_stage(s) and not names_queue(s):
            for qn in queues[s.id]:
                named.setdefault(qn, collections.Counter())[s.resource[1]] += 1
            if not queues[s.id]:
                untraced[s.resource[1]] += 1
    if untraced:
        named.setdefault(BY_ITEM_ID, collections.Counter()).update(untraced)
    return named


def queue_roles(x: Export):
    """What each process does to the items of each queue: {queue key: (name, {role: {(process, reach): Counter(action)}})}
    with the roles `adds`, `takes`, `changes` and `reads` (every other action: an item read or listed), and the reach of
    the stages taking them (`Reach`). A queue is the release's definition, else the first spelling a process uses;
    names compare case-insensitively. `(by item id)` holds the actions whose queue is not traced."""
    roles = collections.OrderedDict()
    for q in x.queues.values():
        roles[q['name'].lower()] = (q['name'], collections.OrderedDict())
    for p in x.procs:
        traced = queue_stage_queues(p)
        for status in REACH_ORDER:
            for name, acts in queue_refs(x, p, status, traced).items():
                _, by_role = roles.setdefault(name.lower(), (name, collections.OrderedDict()))
                for a, n in acts.items():
                    role = 'adds' if a in ITEM_ADDS else 'takes' if a in ITEM_TAKES else 'changes' if a in ITEM_CHANGES else 'reads'
                    by_role.setdefault(role, collections.OrderedDict()).setdefault((p, status), collections.Counter())[a] += n
    return roles


def unwritten_items(p: Proc):
    """The data items and collections of `p` that an expression reads and nothing writes: [(item, the stages that read
    it)]. Written means an output of a stage other than End, a Start input, the target of a calculation, a Read step's
    target, or a collection a Collections or Work Queues action names in `Collection Name`. An item exposed as an
    environment or session variable is filled by the environment and is left out. A name is the page's own item, else a
    shared one, and compares case-insensitively; `[Collection.Field]` reads the collection."""
    own = {(s.page, s.name.lower()): s for s in p.stages.values() if s.type in ('Data', 'Collection')}
    shared = {s.name.lower(): s for s in p.stages.values() if s.type in ('Data', 'Collection') and not s.private}

    def find(page, name):
        n = (name or '').strip().lower()
        for cand in (n, n.split('.', 1)[0].strip()):
            hit = own.get((page, cand)) or shared.get(cand)
            if hit:
                return hit
        return None
    written, readers = set(), collections.OrderedDict()

    def write(page, name):
        d = find(page, name)
        if d:
            written.add(d.id)

    def read(s, name):
        d = find(s.page, name)
        if d and s not in readers.setdefault(d.id, []):
            readers[d.id].append(s)
    for s in p.stages.values():
        for o in s.outputs if s.type != 'End' else []:
            write(s.page, o['stage'])
        for i in s.inputs if s.type == 'Start' else []:
            write(s.page, i['stage'])
        for _, target in s.calcs + ([s.calc] if s.calc else []):
            write(s.page, target)
        for stp in s.steps:
            write(s.page, stp['stage'])
            for v in stp['outputs'].values():
                write(s.page, v)
        if s.type == 'Action' and s.resource and internal_family(s.resource[0]) in ('Collections', 'Work Queues'):
            for i in s.inputs:
                if i['name'] == 'Collection Name' and i['expr']:
                    write(s.page, p.name_of(i['expr']))
        if s.type == 'LoopStart' and s.loopdata:
            read(s, s.loopdata)
    for s, ex in p.expressions():
        for ref in DATA_REF.findall(ex or ''):
            read(s, ref)
    return [(d, readers[d.id]) for d in p.stages.values() if d.id in readers and d.id not in written and d.exposure not in ('Environment', 'Session')]


# the Work Queues inputs that carry tags: Add To Queue's Tags, Tag Item's and Untag Item's Tag, every listing's filter
TAG_INPUTS = ('Tags', 'Tag', 'Tag Filter')


def tag_uses(p: Proc):
    """The Work Queues stages of `p` that add, remove, filter on or read item tags: (stage, tag inputs, tag outputs,
    the Item ID expression, the actions its value comes from). An Item ID from Get Next Item is an item the session
    holds; one from a listing (Get Report Data, Get Completed Items ...) may be finished. The value is followed back as
    `item_origins` does."""
    origins = item_origins(p)

    def label(s):
        if s.type == 'Action' and s.resource:
            return s.resource[1] if internal_family(s.resource[0]) == 'Work Queues' else f"{s.resource[0]}.{s.resource[1]}"
        return s.type
    uses = []
    for s in p.stages.values():
        if not is_queue_stage(s):
            continue
        terms = [i for i in s.inputs if i['name'] in TAG_INPUTS and i['expr']]
        reads = [o for o in s.outputs if 'tag' in (o['name'] or '').lower()]
        if not (terms or reads or s.resource[1] in ('Tag Item', 'Untag Item')):
            continue
        item = next((i['expr'] for i in s.inputs if i['name'] == 'Item ID' and i['expr']), '')
        uses.append((s, terms, reads, item, sorted({o if isinstance(o, str) else label(o) for o in origins(s.page, item)})))
    return uses


REACH_MARK = {'runs': '', 'unused': ' [unused]', 'dead': ' [dead]'}
REACH_ORDER = ('runs', 'unused', 'dead')


class Reach:
    """What runs in the export. From the roots' main pages - in an export of objects only, every published action - a
    page's stages that its links reach (`page_flow`) run, and each running call runs its callee: a Page stage its page,
    a Process stage the callee's main page, an Action stage the action's page of an object of the export and that
    object's Initialise and Clean Up pages, which the product runs for every instance. A page nothing that runs
    reaches is `unused` when no stage but its own calls it, or such a page runs it, and `dead` otherwise: only unlinked
    stages and dead pages lead to it - a page calling itself behind an unlinked stage included. A stage is `dead` when
    its page's links do not reach it, else its page's status; a process or an object has its main or Initialise
    page's status."""

    def __init__(self, x: Export):
        self.on_page = collections.defaultdict(list)
        for p in x.procs:
            for s in p.stages.values():
                self.on_page[(p, s.page)].append(s)
        self.linked = {}
        for p in x.procs:
            for pid, _, _ in p.pages():
                got = x.flow(p, pid)['reached']
                self.linked.update({s: s.id in got for s in self.on_page[(p, pid)]})
        self.calls = {}  # stage -> the pages it runs: (process or object, page id)
        for p in x.procs:
            pages = {sh['id'] for sh in p.sheets}
            for s in p.stages.values():
                if s.type == 'SubSheet' and s.processid in pages:
                    self.calls[s] = [(p, s.processid)]
                elif s.type == 'Process' and x.by_guid.get(s.processid) is not None:
                    self.calls[s] = [(x.by_guid[s.processid], None)]
                elif s.type == 'Action' and s.resource:
                    o = x.object_by_name(s.resource[0])
                    if o is not None and o.kind == 'object':
                        self.calls[s] = [(o, sh['id']) for sh in o.sheets if sh['name'] == s.resource[1] or sh['type'] == 'CleanUp'] + [(o, None)]
        self.callers = collections.defaultdict(list)
        for s, units in self.calls.items():
            for u in units:
                self.callers[u].append(s)
        procs = [p for p in x.procs if p.kind == 'process']
        self.roots = [p for p in procs if all(c.proc is p for c in self.callers.get((p, None), []))]  # a call to itself aside
        if procs:
            todo = [(p, None) for p in self.roots]
        else:  # objects only: their callers are outside the export
            todo = [(o, sh['id']) for o in x.procs for sh in o.sheets if (sh.get('published') or '').lower() == 'true']
            todo += [(o, pid) for o in {o for o, _ in todo} for pid in [None] + [sh['id'] for sh in o.sheets if sh['type'] == 'CleanUp']]
        runs = self.closure(todo)
        units = [(p, pid) for p in x.procs for pid, _, _ in p.pages()]
        uncalled = [u for u in units if u not in runs and all((c.proc, c.page) == u for c in self.callers.get(u, []))]  # a call to itself aside
        unused = self.closure(uncalled, runs)
        self.pages = {u: 'runs' if u in runs else 'unused' if u in unused else 'dead' for u in units}

    def closure(self, todo, stop=frozenset()):
        """The pages `todo` names and every page their reached stages call, through any number of calls, but `stop`."""
        seen = set()
        while todo:
            u = todo.pop()
            if u not in seen and u not in stop:
                seen.add(u)
                todo += [v for s in self.on_page[u] if self.linked.get(s) for v in self.calls.get(s, [])]
        return seen

    def page(self, p, page_id):
        return self.pages.get((p, page_id), 'dead')

    def stage(self, s):
        return self.page(s.proc, s.page) if self.linked.get(s) else 'dead'

    def item(self, p):
        return self.page(p, None)


def best(statuses):
    """The status of a use made by several stages: it runs when one of them does, it is dead when all of them are."""
    return min(statuses, key=REACH_ORDER.index, default='dead')


def sleep_seconds(p: Proc):
    total, n = 0.0, 0
    for s in p.stages.values():
        if s.type == 'Action' and s.resource and s.resource[1] in ('Sleep', 'Wait', 'Delay'):
            n += 1
            for i in s.inputs:
                m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*', p.resolve(i['expr'] or '') or '')
                if m:
                    total += float(m.group(1))
    return n, total


def screenshot_actions(p: Proc):
    return sum(1 for s in p.stages.values() if s.type == 'Action' and s.resource and re.search(r'screen ?shot|print ?screen|capture', s.resource[1] or '', re.I)) \
        + sum(1 for s in p.stages.values() if s.type == 'Exception' and (s.exception or {}).get('savescreencapture') == 'yes')


def profile(x: Export):
    L = []
    pr = L.append
    procs = [p for p in x.procs if p.kind == 'process']
    objs = [p for p in x.procs if p.kind == 'object']
    pr(f"## EXPORT {x.given}")
    for r in x.releases:
        pr(f"  release `{r['name']}` created {r['created']} package-id={r['packageId']} package `{r['packageName']}` created-by={r['createdBy']} contents={r['count']}"
           + (f" notes={written(oneline(r['notes']))}" if r['notes'] else ''))
    if not x.releases:
        pr(f"  {len(x.files)} standalone process/object export file(s), no release header (identity is the file set)")
    vers = collections.Counter(p.bpversion for p in x.procs)
    pr(f"  Blue Prism versions of the definitions: {written(dict(vers), py=True)}")
    kinds = collections.Counter()
    for k, v in x.items.items():
        kinds[k] = len(v)
    pr(f"  contents: processes={len(procs)} objects={len(objs)} " + ' '.join(f"{k}={v}" for k, v in sorted(kinds.items())))
    several = [p for p in x.procs if p.copies]
    if several:
        pr("\n## DEFINITIONS IN SEVERAL FILES (name (id) | the copy read: file, release created | the other copies: file, release created, same or differs)"
           " - one GUID is one definition, read from the release created last; a copy that differs is another version, `raw` prints each")
        for p in several:
            pr(f"  `{p.name}` ({p.id}) {p.kind} | {x.rel(p.path)} created {x.created.get(p.path)} | " + '; '.join(copy_text(x, p, c) for c in p.copies))

    pr("\n## PROCESSES (name (id) | published | pages | stages | inputs | outputs | queues | credentials | calls processes | uses objects)")
    for p in procs:
        st = p.start_of(None)
        ins = [i['name'] for i in st.inputs] if st else []
        outs = [o['name'] for o in p.end_outputs(None)]
        q = list(queue_refs(x, p).keys())
        creds = sorted({c for _, c in credential_refs(x, p)})
        pcalls = sorted({e['object'] for e in x.edges if e['caller'] is p and e['kind'] == 'process'})
        objects = sorted({e['object'] for e in x.edges if e['caller'] is p and e['kind'] == 'object' and not e['internal']})
        pr(f"  `{p.name}` ({p.id}) | published={p.published} | pages={len(p.sheets) + 1} | stages={len(p.stages)} | in={written(ins, py=True)} | out={written(outs, py=True)} "
           f"| queues={written(q, py=True)} | credentials={written(creds, py=True)} | processes={written(pcalls, py=True)} | objects={len(objects)}")
        if p.narrative:
            pr(f"      description: {oneline(x.safe(p.narrative))}")

    pr("\n## OBJECTS (name (id) | run mode, shared when set | application model | elements | actions (pages) | code | called by, a caller whose calls do not run marked as "
       "under CALL GRAPH)")
    callers = collections.defaultdict(lambda: collections.defaultdict(list))  # object name -> caller name -> the statuses of its calls
    for e in x.edges:
        if e['kind'] == 'object' and e['callee']:
            callers[e['callee'].name][e['caller'].name].append(x.reach.stage(e['stage']))
    for o in objs:
        launch = {k: v for k, v in o.appparams.items() if v and k in ('Path', 'WindowTitle', 'CommandLineParams', 'ProcessName', 'WorkingDirectory')}
        ncode = sum(1 for s in o.stages.values() if s.type == 'Code')
        techs = collections.Counter(e.technology for e in o.elements if e.type and e.type != 'Application')
        pr(f"  `{o.name}` ({o.id}) | {o.runmode}{f' shared={o.shared}' if o.shared else ''} | {o.apptype or '-'} {written(launch) if launch else ''} | elements={len(o.elements)} {dict(techs) if techs else ''} "
           f"| actions={len(o.actions())} | code={ncode} {o.info.language if o.info and ncode else ''} | callers={written(sorted(c + REACH_MARK[best(st)] for c, st in callers[o.name].items()), py=True)}")
        if o.narrative:
            pr(f"      description: {oneline(x.safe(o.narrative))}")

    pr("\n## WORK QUEUES (name (id) | key field | max attempts | defined in release | used by (actions)) - names are case-insensitive; other spellings listed")
    for q in x.queues.values():
        sp = f" | also spelled {written(q['spellings'][1:], py=True)}" if len(q['spellings']) > 1 else ''
        pr(f"  `{q['name']}` ({q['id']}) | key={q['keyField']} | attempts={q['maxAttempts']} | defined={q['defined']} | {written(q['usedBy'], py=True)} {written(dict(q['actions']), py=True)}{sp}")
    pr("\n## QUEUE ROLES (per queue: the processes that add, take, change and read its items, with the actions and their counts; then per process: the roles it has on each queue) - "
       "an adds, takes or changes role no process has in the release is `none` (reads are listed only when a process has them): a queue nothing adds to is filled from outside the release, one nothing takes from is worked outside it. An action that names no queue "
       "(Update Status, Mark Completed, Tag Item ...) belongs to the queue its Item ID is followed back to; `(by item id)` holds the ones a process cannot follow. "
       "A process marked [unused] or [dead] has the role only on stages that do not run (CALL GRAPH)")
    by_process = collections.OrderedDict()
    for key, (name, by_role) in queue_roles(x).items():
        known = x.queues.get(key)
        label = f"`{visible(name)}`" + (f" ({known['id']})" if known else '')
        pr(f"  {label}")
        for role in ('adds', 'takes', 'changes', 'reads'):
            procs_of = by_role.get(role, {})
            if procs_of or role != 'reads':
                pr(f"    {role}: " + ('; '.join(f"`{p.name}` ({p.id}){REACH_MARK[st]} " + ', '.join(f"{a}×{n}" for a, n in acts.items()) for (p, st), acts in procs_of.items()) or 'none'))
            for (p, st), acts in procs_of.items():
                by_process.setdefault(p, collections.OrderedDict()).setdefault(label, []).append(role + REACH_MARK[st])
    if by_process:
        pr('  by process:')
    for p, qs in by_process.items():
        pr(f"  `{p.name}` ({p.id}): " + '; '.join(f"{lab} {', '.join(roles_)}" for lab, roles_ in qs.items()))
    pr("\n## QUEUE TAGS (process «page» ‹stage› action | tag inputs | tag outputs | the Item ID and the actions its value comes from, through calculations and page inputs) - "
       "each tag's lifecycle: what sets it, what removes it, what reads it, and whether the item addressed is held (Get Next Item) or listed (may be finished); "
       "a stage that does not run is marked as under CALL GRAPH")
    for p in x.procs:
        for s, terms, reads, item, src in tag_uses(p):
            pr(f"  `{p.name}` ({p.id}) «{p.page_name(s.page)}» ‹{s.name}› {s.resource[1]}{REACH_MARK[x.reach.stage(s)]}"
               + ''.join(f" | {i['name']}={visible(expr(x.safe(i['expr'])))}" for i in terms)
               + ''.join(f" | {o['name']}→[{o['stage']}]" for o in reads)
               + (f" | item {visible(expr(item))} from {', '.join(src) or '(nothing in the process)'}" if item else ''))
    pr("\n## ENVIRONMENT VARIABLES (name | type | value | description) - secrets masked; the exposing processes are under DATA ITEMS")
    for ev in x.items.get('environment-variable', []):
        pr(f"  {ev['name']} | {ev['attrs'].get('type')} | {written(ev['attrs'].get('value'))} | {ev.get('description', '')}")
    pr("\n## CREDENTIALS referenced (name expression -> processes, a read by stages that do not run marked as under CALL GRAPH), then the release's definitions (type | access) - "
       "Credentials.Get / Set inputs; names are identity, secrets never travel")
    cr = collections.defaultdict(set)
    for p in x.procs:
        for st in REACH_ORDER:
            for act, c in credential_refs(x, p, st):
                cr[c].add(f"{p.name} ({act}){REACH_MARK[st]}")
    for c in sorted(cr):
        pr(f"  {c}: {written(sorted(cr[c]), py=True)}")
    for cd in x.items.get('credential', []):  # the access rights: the processes and objects the credential names
        extra = {k: v for k, v in cd.items() if k not in ('name', 'id', 'attrs', 'members')}
        pr(f"  release credential `{cd['name']}`" + (f" {written(cd['attrs'], py=True)}" if cd['attrs'] else '') + (f" {x.safe(written(extra))}" if extra else '')
           + f" | access={written(member_names(x, cd.get('members', [])), py=True)}")
    if x.items.get('calendar'):
        pr("\n## CALENDARS (name (id) | working week and public holiday group | public holidays overridden, by id | other holidays | read by) - "
           "the Calendars internal object reads a calendar by its name; a read by stages that do not run is marked as under CALL GRAPH")
        for c in x.items['calendar']:
            readers = sorted({f"{p.name} ({act}){REACH_MARK[st]}" for p in x.procs for st in REACH_ORDER for act, n in calendar_refs(x, p, st) if n == c['name']})
            for sc in c['calendar']:
                pr(f"  `{c['name']}` ({c['id']}) | {written(sc['attrs'], py=True)} | overrides={written(sc['publicHolidayOverrides'], py=True)} "
                   f"| other-holidays={written(sc['otherHolidays'], py=True)} | read by {written(readers, py=True)}")
    for kind in sorted(x.items):
        if kind in ('work-queue', 'environment-variable', 'credential', 'calendar', 'webapiservice'):
            continue
        pr(f"\n## {kind.upper()} ({len(x.items[kind])})")
        for it in x.items[kind]:
            members = it.get('members')
            mem = f" members={written(member_names(x, members), py=True)}" if members else ''
            extra = {k: v for k, v in it.items() if k not in ('name', 'id', 'attrs', 'members')}
            pr(f"  {it['name']} {written(it['attrs'], py=True) if it['attrs'] else ''}{mem}{(' ' + x.safe(written(extra))) if extra else ''}")
    if x.items.get('webapiservice'):
        pr(f"\n## WEB API SERVICES ({len(x.items['webapiservice'])}) - base URL, common headers and parameters, actions (method, path, parameters, outputs); "
           "every detail in bp-data-sources.json")
        for it in x.items['webapiservice']:
            sv = it['service']
            headers = ', '.join(f"{h['name']}: {x.safe(h['value'] or '')}" for h in sv['commonHeaders'])
            pr(f"  `{it['name']}` ({it['id']}) enabled={sv['enabled']} base={sv['baseUrl']} settings={written(sv['settings'])}"
               + (f" headers={headers}" if headers else '') + (f" parameters={', '.join(ap['name'] for ap in sv['commonParameters'])}" if sv['commonParameters'] else ''))
            for a in sv['actions']:
                outs = ', '.join(f"{o['name']}={o['path']}" for o in a['outputs'])
                pr(f"    {a['name']} ({a['id']}): {a['method']} {a['urlPath']}" + (f" in={', '.join(ap['name'] for ap in a['parameters'])}" if a['parameters'] else '')
                   + (f" out={outs}" if outs else ''))

    pr("\n## STAGE VOCABULARY (type: processes / objects) - most used first, a tie by name")
    sv_p = collections.Counter(s.type for p in procs for s in p.stages.values())
    sv_o = collections.Counter(s.type for p in objs for s in p.stages.values())
    for t in sorted(set(sv_p) | set(sv_o), key=lambda k: (-(sv_p[k] + sv_o[k]), k)):
        pr(f"  {t}: {sv_p[t]} / {sv_o[t]}")

    pr("\n## ACTION VOCABULARY (object.action: uses by processes / by objects) - Blue Prism internal objects rendered by family")
    av = collections.defaultdict(lambda: [0, 0])
    for e in x.edges:
        if e['kind'] == 'object':
            av[x.resource_label(e['object'], e['action'])][0 if e['caller'].kind == 'process' else 1] += 1
    for k in sorted(av):
        pr(f"  {k}: {av[k][0]} / {av[k][1]}")

    R = x.reach
    pr("\n## CALL GRAPH - what runs is reached from the roots' main pages through the stages their links reach; a call marked [unused] is made on a page "
       "nothing that runs reaches, one marked [dead] by an unlinked stage or on a page only such calls reach (dead code)")
    pr("  processes -> processes (Process stage):")
    for e in x.edges:
        if e['kind'] == 'process':
            pr(f"    `{e['caller'].name}` ({e['caller'].id}) -> {('`' + e['callee'].name + '` (' + str(e['callee'].id) + ')') if e['callee'] else 'UNRESOLVED ' + e['object']}  at ‹{e['stage'].name}›"
               f" on page «{e['caller'].page_name(e['stage'].page)}»{REACH_MARK[R.stage(e['stage'])]}")
    pr("  callers -> objects (Action stages, distinct actions):")
    agg = collections.defaultdict(lambda: collections.Counter())
    for e in x.edges:
        if e['kind'] == 'object' and not e['internal']:
            agg[(e['caller'], e['object'], e['callee'], R.stage(e['stage']))][e['action']] += 1
    for (c, obj, callee, st), acts in sorted(agg.items(), key=lambda kv: (kv[0][0].kind != 'process', kv[0][0].name, kv[0][1], REACH_ORDER.index(kv[0][3]))):
        head = (f"`{obj}` ({callee.id})" if callee else f"UNRESOLVED `{obj}`") + REACH_MARK[st]
        pr(f"    `{c.name}` ({c.id}) -> {head}: {written(dict(acts), py=True)}")
    roots = R.roots
    pr(f"  ROOTS (processes no other process calls): {written([f'`{p.name}` ({p.id}) published={p.published}' for p in roots], py=True)}"
       + ('' if procs else " - an export of objects only: every published action is an entry point, its callers are outside the export"))
    sub = [p for p in procs if p not in roots]
    pr(f"  SUB-PROCESSES (called by another process): {written([f'`{p.name}` ({p.id})' + REACH_MARK[R.item(p)] for p in sub], py=True)}")
    pr(f"  UNUSED OBJECTS (in the release, reached by no root: called by nobody in it, or only on pages nothing that runs reaches): "
       f"{written([f'`{o.name}` ({o.id})' for o in objs if R.item(o) == 'unused'], py=True)}")
    pr("  DEAD PROCESSES AND OBJECTS (every call to them is [dead]; a published process may still be started on its own) and the stages calling them:")
    for p in x.procs:
        if R.item(p) == 'dead':
            pr(f"    `{p.name}` ({p.id}) {p.kind}" + (f" published={p.published}" if p.kind == 'process' else '') + ': '
               + '; '.join(f"`{c.proc.name}` ({c.proc.id}) «{c.proc.page_name(c.page)}» ‹{c.name}›{x.stage_tag(c)}" for c in R.callers[(p, None)]))
    pr(f"  UNRESOLVED objects (named by an Action stage, absent from the release): "
       f"{written(sorted({e['object'] for e in x.edges if e['kind'] == 'object' and not e['callee'] and not e['internal']}), py=True)}")
    pr("  PAGES THAT DO NOT RUN per running process (unused: no page that runs calls them; dead: only dead calls reach them):")
    for p in procs:
        off = {st: [n for pid, n, _ in p.pages() if R.page(p, pid) == st] for st in ('unused', 'dead')}
        if R.item(p) == 'runs' and (off['unused'] or off['dead']):
            pr(f"    `{p.name}` ({p.id}): unused {written(off['unused'], py=True)}; dead {written(off['dead'], py=True)}")
    pr("  UNUSED ACTIONS per running object (its pages nothing that runs reaches, directly or through a page it runs; dead: only dead calls reach them):")
    for o in objs:
        if R.item(o) != 'runs':
            continue
        by = {st: [a['name'] for a in o.actions() if R.page(o, a['id']) == st] for st in REACH_ORDER}
        unused = by['unused']
        pr(f"    `{o.name}` ({o.id}): {len(by['runs'])} used, {len(unused)} unused" + (f": {written(unused, py=True)}" if unused else '')
           + (f", {len(by['dead'])} dead: {written(by['dead'], py=True)}" if by['dead'] else ''))

    pr("\n## APPLICATION MODELS (object: launch/attach type, parameters, element types, dynamic elements, positional-only elements)")
    for o in objs:
        if not o.elements or len(o.elements) <= 1:
            continue
        types = collections.Counter(e.type for e in o.elements if e.type and e.type != 'Application')
        dyn = sum(1 for e in o.elements if e.is_dynamic())
        pos = sum(1 for e in o.elements if e.is_positional_only())
        params = {k: v for k, v in o.appparams.items() if v}
        pr(f"  `{o.name}` ({o.id}): {o.apptype} {written(params)} | {written(dict(types), py=True)} | dynamic={dyn} positional-only={pos}" + (f" | inherits {o.inherits}" if o.inherits else ''))
    pr("\n## UI ACTION VOCABULARY (stage type / action or condition id: uses)")
    ui = collections.Counter()
    for o in objs:
        for s in o.stages.values():
            if s.type in ('Navigate', 'Read', 'Write'):
                for stp in s.steps:
                    ui[(s.type, stp['action'] or 'Write')] += 1
            elif s.type == 'WaitStart':
                for ch in s.choices:
                    ui[('Wait', ch['condition'])] += 1
    for (t, a), n in sorted(ui.items(), key=lambda kv: (kv[0][0], -kv[1])):
        pr(f"  {t}/{a}: {n}")
    pr("\n## SAP SCRIPTING IDS INSIDE CODE STAGES (object: stage -> count) - locators hidden in code, see the selectors guide")
    for o in objs:
        for s in o.stages.values():
            if s.type == 'Code' and s.code:
                ids = SAP_ID_IN_CODE.findall(s.code)
                if ids:
                    pr(f"  `{o.name}`: ‹{s.name}› {len(ids)} ids, e.g. {ids[0]}")

    pr("\n## EXPRESSION VOCABULARY (functions: uses) and OPERATORS")
    funcs, ops = collections.Counter(), collections.Counter()
    for p in x.procs:
        for _, e in p.expressions():
            for f in FUNC.findall(e):
                funcs[f] += 1
            for o in re.findall(r'(<>|<=|>=|=|<|>|&|\bAND\b|\bOR\b|\bNOT\b)', e):
                ops[o] += 1
    pr(f"  functions: {dict(funcs.most_common())}")
    pr(f"  operators: {dict(ops.most_common())}")

    pr("\n## DATA ITEMS (per process: exposed environment/session variables; password items; encrypted initial values)")
    for p in x.procs:
        env = [f"{s.name}({s.exposure})" for s in p.env_items()]
        pw = sum(1 for s in p.stages.values() if s.type == 'Data' and s.datatype == 'password')
        enc = sum(1 for s in p.stages.values() if s.type == 'Data' and s.encrypted)
        if env or pw:
            pr(f"  `{p.name}`: exposed={written(env, py=True)} password-items={pw} encrypted-initial-values={enc}")
    pr("\n## DATA ITEMS READ AND NEVER WRITTEN (process, then each item with its initial value and the stages that read it) - nothing writes the item (no output of a stage, calculation, Start input, "
       "Read step or Collections action targets it; an environment or session variable is filled from outside), so every read sees its initial value and an item with none is always empty; "
       "those with none come first")
    for p in x.procs:
        found = sorted(unwritten_items(p), key=lambda t: bool(t[0].initialvalue or t[0].encrypted or t[0].rows))
        if found:
            pr(f"  `{p.name}` ({p.id})")
        for d, readers in found:
            text_ = data_item_text(x, d) if d.type == 'Data' else collection_text(d) + (f" rows={len(d.rows)}" if d.rows else '')
            empty = not (d.initialvalue or d.encrypted or d.rows)
            pr(f"    {text_} | " + ('no initial value' if empty else 'has an initial value') + ' | read by '
               + '; '.join(f"«{visible(p.page_name(r.page))}» ‹{visible(r.name)}›{x.stage_tag(r)}" for r in readers))
    pr("\n## COLLECTION FIELDS referenced per process (collection: fields) - the schema of workbooks, queue items and configuration the process reads")
    for p in procs:
        for coll, fields in sorted(p.collection_fields().items()):
            if fields:
                pr(f"  `{p.name}` [{coll}]: {written(sorted(fields), py=True)}")

    pr("\n## EXCEPTIONS (per process: throw types, recover stages, blocks with a recover, rethrows)")
    for p in x.procs:
        throws = collections.Counter((s.exception or {}).get('type') or '(none)' for s in p.stages.values() if s.type == 'Exception' and (s.exception or {}).get('usecurrent') != 'yes')
        rethrow = sum(1 for s in p.stages.values() if s.type == 'Exception' and (s.exception or {}).get('usecurrent') == 'yes')
        recs = [s for s in p.stages.values() if s.type == 'Recover']
        blocks = sorted(s.block.name for s in recs if s.block)
        pagewide = [p.page_name(s.page) for s in recs if not s.block]
        if throws or recs:
            pr(f"  `{p.name}`: throws={written(dict(throws), py=True)} rethrows={rethrow} recovers={len(recs)} block-scoped={written(blocks, py=True)} "
               f"page-scoped={written(pagewide, py=True)}")

    pr("\n## EVIDENCE AND PLUMBING (per process: notes, sleeps (count, fixed seconds), screenshots, stage logging (all / errors only / none), IsStopRequested, environment locks, resolution/login utilities)")
    for p in x.procs:
        notes = sum(1 for s in p.stages.values() if s.type == 'Note')
        n, secs = sleep_seconds(p)
        shots = screenshot_actions(p)
        log = collections.Counter(s.logging for s in p.stages.values() if s.type in FLOW_TYPES)
        stop = sum(1 for _, e in p.expressions() if 'IsStopRequested' in e)
        locks = sum(1 for s in p.stages.values() if s.type == 'Action' and s.resource and internal_family(s.resource[0]) == 'Environment Locking')
        pr(f"  `{p.name}`: notes={notes} sleeps={n} (~{secs:.0f}s) screenshots={shots} logging: all={log['all']} errors-only={log['errors']} none={log['none']} stop-checks={stop} env-locks={locks}")
    return '\n'.join(L)


def cards(x: Export):
    L = []
    pr = L.append
    callers = collections.defaultdict(lambda: collections.defaultdict(list))  # callee id -> caller -> the statuses of its calls
    for e in x.edges:
        if e['kind'] in ('object', 'process') and e['callee'] and e['callee'] is not e['caller']:
            callers[e['callee'].id][f"`{e['caller'].name}` ({e['caller'].id})"].append(x.reach.stage(e['stage']))

    def section(label, its):
        """A card item list: one item per line, each printed whole."""
        if its:
            pr(f"  {label} ({len(its)}):")
            for it in its:
                pr(f"    {it}")
    for p in x.procs:
        flow = [s for s in p.stages.values() if s.type in FLOW_TYPES and s.type not in ('Anchor', 'Note')]
        pr(f"### `{p.name}` ({p.id}) {p.kind}  published={p.published} runmode={p.runmode or '-'}{f' shared={p.shared}' if p.shared else ''} pages={len(p.sheets) + 1} stages={len(p.stages)} flow-stages={len(flow)}"
           + (f" bpversion={p.bpversion}" if p.bpversion else '') + f" groups={written(p.groups)}")
        if p.narrative:
            pr(f"  description: {oneline(x.safe(p.narrative))}")
        st = p.start_of(None)
        pr(f"  inputs: {', '.join(fmt_param(x, i, '→') for i in st.inputs) if st else ''}")
        pr(f"  outputs: {', '.join(fmt_param(x, o, '←') for o in p.end_outputs(None))}")
        pr(f"  callers: {', '.join(sorted(c + REACH_MARK[best(st)] for c, st in callers[p.id].items()))}")
        if p.apptype:
            techs = collections.Counter(e.technology for e in p.elements if e.type and e.type != 'Application')
            pr(f"  application model: {p.apptype} {written({k: v for k, v in p.appparams.items() if v})} elements={len(p.elements)} "
               + ', '.join(f"{k}×{v}" for k, v in techs.items()))
        pr("  pages:")
        for pid, name, ptype in p.pages():
            stages = p.page_stages(pid)
            info = next((s for s in stages if s.type == 'SubSheetInfo'), None)
            s0 = p.start_of(pid)
            ins = [i['name'] for i in s0.inputs] if s0 else []
            outs = [o['name'] for o in p.end_outputs(pid)]
            nflow = sum(1 for s in stages if s.type in FLOW_TYPES and s.type not in ('Anchor', 'Note'))
            called = sum(1 for e in x.edges if e['kind'] == 'page' and e['caller'] is p and e['object'] == name)
            desc = oneline(x.safe(info.narrative)) if info and info.narrative else ''
            pr(f"    «{name}»{' [' + ptype + ']' if ptype not in ('Normal', 'Main') else ''}{REACH_MARK[x.reach.page(p, pid)]} in=[{', '.join(ins)}] out=[{', '.join(outs)}] stages={nflow}"
               + (f" called×{called}" if called else '') + (f" :: {desc}" if desc else ''))
        cal = callees_in_order(x, p)
        pr(f"  calls (in order, distinct): " + '; '.join((f"{k}:{v}" if k != 'action' else v) + REACH_MARK[st] for k, v, st in cal))
        q = queue_refs(x, p)
        if q:
            pr(f"  work queues: " + '; '.join(f"`{k}` " + ', '.join(f"{a}×{n}" for a, n in v.items()) for k, v in q.items()))
        creds = credential_refs(x, p)
        if creds:
            pr(f"  credentials: {', '.join(sorted({c for _, c in creds}))}")
        env = p.env_items()
        if env:
            pr(f"  environment variables: {', '.join(f'{s.name} ({s.exposure})' for s in env)}")
        section('collection fields', [f"[{k}]: {', '.join(sorted(v))}" for k, v in p.collection_fields().items() if v])
        section('decisions', [f"«{p.page_name(s.page)}» ‹{s.name}› {expr(x.safe(s.decision or ''))}" for s in p.stages.values() if s.type == 'Decision'])
        section('choices', [f"«{p.page_name(s.page)}» ‹{s.name}› " + ' | '.join(f"{visible(c['name'])}: {expr(x.safe(c['expression'] or ''))}" for c in s.choices)
                            for s in p.stages.values() if s.type == 'ChoiceStart'])
        section('loops', [f"«{p.page_name(s.page)}» ‹{s.name}› {s.looptype or 'ForEach'} [{s.loopdata}]" for s in p.stages.values() if s.type == 'LoopStart'])
        ui = collections.Counter()
        for s in p.stages.values():
            if s.type in ('Navigate', 'Read', 'Write'):
                for stp in s.steps:
                    ui[f"{s.type}/{stp['action'] or 'Write'}"] += 1
            elif s.type == 'WaitStart':
                for ch in s.choices:
                    ui[f"Wait/{ch['condition']}"] += 1
        if ui:
            pr(f"  ui actions: " + ', '.join(f"{k}×{v}" for k, v in ui.items()))
        section(f"code stages, {p.info.language if p.info and p.info.language else '?'}", [f"«{p.page_name(s.page)}» ‹{s.name}›" for s in p.stages.values() if s.type == 'Code'])
        throws = sorted({f"{(s.exception or {}).get('type') or '(none)'}: {expr(x.safe((s.exception or {}).get('detail') or ''))}" for s in p.stages.values()
                         if s.type == 'Exception' and (s.exception or {}).get('usecurrent') != 'yes'})
        rethrow = sum(1 for s in p.stages.values() if s.type == 'Exception' and (s.exception or {}).get('usecurrent') == 'yes')
        recs = [s for s in p.stages.values() if s.type == 'Recover']
        pr(f"  error handling: throws={sum(1 for s in p.stages.values() if s.type == 'Exception' and (s.exception or {}).get('usecurrent') != 'yes')} rethrows={rethrow} "
           f"recovers={len(recs)} (block-scoped {sum(1 for r in recs if r.block)}, page-scoped {sum(1 for r in recs if not r.block)})"
           + (f" recover attempts: {', '.join(f'‹{s.name}› {s.attempts}' for s in recs if s.attempts)}" if any(s.attempts for s in recs) else ''))
        section('throw kinds', throws)
        n, secs = sleep_seconds(p)
        log = collections.Counter(s.logging for s in flow)
        pr(f"  evidence: notes={sum(1 for s in p.stages.values() if s.type == 'Note')} sleeps={n} (~{secs:.0f}s fixed) screenshots={screenshot_actions(p)} "
           f"logging: all={log['all']} errors-only={log['errors']} none={log['none']}")
        pr('')
    return '\n'.join(L)


# ----------------------------------------------------------------------------------------------------------------------
def targets(x: Export):
    cat = []
    for o in x.procs:
        uses = collections.defaultdict(list)  # element guid -> actions applied
        for s in o.stages.values():
            page = o.page_name(s.page)
            if s.type in ('Navigate', 'Read', 'Write'):
                for stp in s.steps:
                    if not stp['element']:
                        continue
                    e = o.element_by_id.get(stp['element'])
                    entry = {'stage': s.name, 'page': page, 'kind': s.type.lower(), 'action': stp['action'] or ('Write' if s.type == 'Write' else None),
                             'args': {k: x.safe(v, k) for k, v in stp['args'].items() if v},
                             'elementParameters': {k: x.safe(v['expression'] or '') for k, v in stp['elementparameters'].items()}}
                    if s.type == 'Write':
                        entry['value'] = x.safe(stp['expr'] or '', (e.name if e else '') + ' ' + (e.type or '' if e else ''))
                    if s.type == 'Read':
                        entry['into'] = stp['stage']
                    if s.interval:
                        entry['interval'] = s.interval
                    uses[stp['element']].append(entry)
            elif s.type == 'WaitStart':
                for ch in s.choices:
                    if not ch['element']:
                        continue
                    uses[ch['element']].append({'stage': s.name, 'page': page, 'kind': 'wait', 'action': ch['condition'], 'args': {k: x.safe(v, k) for k, v in ch['args'].items() if v},
                                                'elementParameters': {k: x.safe(v['expression'] or '') for k, v in ch['elementparameters'].items()},
                                                'comparetype': ch['comparetype'], 'expected': x.safe(ch['reply'] or ''), 'timeout': s.timeout, 'choice': ch['name']})
        if not o.elements and not uses:
            continue
        elements = []
        for e in o.elements:
            if not e.type:
                continue  # a bare container node
            elements.append({'id': e.guid, 'name': e.name, 'path': e.path, 'group': e.group, 'type': e.type, 'basetype': e.basetype, 'datatype': e.datatype,
                             'technology': e.technology, 'description': e.description, 'narrative': e.narrative,
                             'matched': [{'name': a['name'], 'comparison': a['comparisontype'] or 'Equal', 'value': x.safe(a['value'], a['name'])} for a in e.matched()],
                             'info': [dict({'name': a['name'], 'value': x.safe(a['value'], a['name'])}, **({'comparison': a['comparisontype']} if a['comparisontype'] else {}))
                                      for a in e.info()],
                             'attributeCount': len(e.attributes), 'dynamic': e.is_dynamic(), 'positionalOnly': e.is_positional_only(),
                             'actions': uses.get(e.guid, [])})
        code_locators = []
        for s in o.stages.values():
            if s.type == 'Code' and s.code:
                for sid in SAP_ID_IN_CODE.findall(s.code):
                    code_locators.append({'stage': s.name, 'page': o.page_name(s.page), 'sapId': sid})
        # a step can name an element the object's model does not declare: no identity to migrate, reported with its actions
        undeclared = [{'id': eid, 'actions': acts} for eid, acts in uses.items() if eid not in o.element_by_id]
        cat.append({'object': o.name, 'objectId': o.id, 'guid': o.guid, 'runmode': o.runmode, 'application': {'type': o.apptype, 'parameters': {k: v for k, v in o.appparams.items() if v}, 'inherits': o.inherits},
                    'elements': elements, 'undeclared': undeclared, 'codeLocators': code_locators})
    techs = collections.Counter(e['technology'] for c in cat for e in c['elements'] if e['type'] != 'Application')
    types = collections.Counter(e['type'] for c in cat for e in c['elements'] if e['type'] != 'Application')
    md = ["# Blue Prism UI target catalog\n",
          f"{len(cat)} objects with an application model, {sum(len(c['elements']) for c in cat)} elements "
          f"({sum(1 for c in cat for e in c['elements'] if e['dynamic'])} with a dynamic attribute, {sum(1 for c in cat for e in c['elements'] if e['positionalOnly'])} positional-only, "
          f"{sum(1 for c in cat for e in c['elements'] if not e['actions'] and e['type'] != 'Application')} never acted on), {sum(len(c['undeclared']) for c in cat)} elements UI steps name that no model declares, "
          f"{sum(len(c['codeLocators']) for c in cat)} SAP scripting ids inside code stages. "
          f"Technologies: {', '.join(f'{k} ({v})' for k, v in techs.most_common())}. Element types: {', '.join(f'{k} ({v})' for k, v in types.most_common())}\n"]
    for c in cat:
        if not any(e['type'] != 'Application' or e['actions'] for e in c['elements']) and not c['codeLocators'] and not c['undeclared']:
            continue
        md.append(f"\n## `{c['object']}` ({c['objectId']}) — {c['application']['type']} {written(c['application']['parameters'])}\n")
        for e in c['elements']:
            if e['type'] == 'Application' and not e['actions']:  # the root is listed for its launch, attach and close steps
                continue
            m = ', '.join(f"{a['name']}{'~' + a['comparison'] if a['comparison'] != 'Equal' else ''}={written(a['value'])}" for a in e['matched'])
            info = ', '.join(f"{a['name']}{'~' + a['comparison'] if a.get('comparison') else ''}={written(a['value'])}" for a in e['info'])
            acts = collections.Counter(a['action'] for a in e['actions'])
            md.append(f"- {'/'.join(e['path'][1:] + [e['name']])} ({e['id']}) <{e['type']}> [{e['technology']}]{' DYNAMIC' if e['dynamic'] else ''}{' POSITIONAL' if e['positionalOnly'] else ''}"
                      f" — matched {{{m}}}" + (f" — info {{{info}}}" if info else '') + (f" — actions {written(dict(acts), py=True)}" if acts else ' — never acted on'))
        for u in c['undeclared']:
            md.append(f"- undeclared element ({u['id']}) — actions {written(dict(collections.Counter(a['action'] for a in u['actions'])), py=True)}")
        for cl in c['codeLocators']:
            md.append(f"- code ‹{cl['stage']}› on «{cl['page']}»: {cl['sapId']}")
    return cat, '\n'.join(md)


def masked(x: Export, o):
    """Every string of a nested structure through `Export.safe`."""
    if isinstance(o, dict):
        return {k: masked(x, v) for k, v in o.items()}
    if isinstance(o, list):
        return [masked(x, v) for v in o]
    return x.safe(o) if isinstance(o, str) else o


def data(x: Export):
    pd = []
    for p in x.procs:
        calls = []
        for e in x.edges:
            if e['caller'] is not p or e['kind'] == 'page':
                continue
            callee = e['callee']
            calls.append({'stage': e['stage'].name, 'page': p.page_name(e['stage'].page), 'kind': e['kind'], 'callee': e['object'], 'action': e['action'],
                          'calleeId': callee.id if callee and callee is not p else None, 'internal': e['internal'], 'recordset': None, 'reach': x.reach.stage(e['stage']),
                          'inputs': {i['name']: x.safe(i['expr'], i['name']) for i in e['stage'].inputs if i['expr']},
                          'outputs': {o['name']: o['stage'] for o in e['stage'].outputs if o['stage']}})
        q = queue_refs(x, p)
        reads = [k for k, v in q.items() if k != BY_ITEM_ID and any(a in v for a in ('Get Next Item', 'Get Item By ID', 'Get Pending Items', 'Get Completed Items', 'Get Exception Items', 'Get Report Data'))]
        st = p.start_of(None)
        pd.append({'id': p.id, 'guid': p.guid, 'name': p.name, 'kind': p.kind, 'published': p.published, 'runmode': p.runmode, 'groups': p.groups, 'reach': x.reach.item(p),
                   'recordset': reads[0] if reads else None, 'queues': {k: dict(v) for k, v in q.items()},
                   'pages': [{'name': n, 'type': t, 'inputs': [i['name'] for i in (p.start_of(pid).inputs if p.start_of(pid) else [])], 'outputs': [o['name'] for o in p.end_outputs(pid)],
                              'reach': x.reach.page(p, pid)} for pid, n, t in p.pages()],
                   'inputs': [i['name'] for i in st.inputs] if st else [], 'outputs': [o['name'] for o in p.end_outputs(None)],
                   'credentials': sorted({c for _, c in credential_refs(x, p)}), 'environmentVariables': [s.name for s in p.env_items()],
                   'screenshots': screenshot_actions(p), 'notes': sum(1 for s in p.stages.values() if s.type == 'Note'), 'calls': calls})
    # data sources
    src = collections.OrderedDict()

    def add(kind, name, drives, proc, where, **extra):
        key = f"{kind}|{name}"
        s = src.setdefault(key, {'id': stable_id(key), 'name': name, 'kind': kind, 'drives': drives, 'usedBy': [], **extra})
        u = f"{proc.name}:{where}" if proc else where
        if u not in s['usedBy']:
            s['usedBy'].append(u)
        return s
    for q in x.queues.values():
        key = f"work-queue|{q['name']}"
        src[key] = {'id': q['id'], 'name': q['name'], 'kind': 'work-queue', 'drives': True, 'keyField': q['keyField'], 'maxAttempts': q['maxAttempts'],
                    'definedInRelease': q['defined'], 'groups': q['groups'], 'spellings': q['spellings'], 'usedBy': q['usedBy'], 'actions': dict(q['actions'])}
    for ws in x.items.get('webapiservice', []):
        users = sorted({f"{e['caller'].name}:{e['action']}" for e in x.edges if e['internal'] == 'Web API' and (e['object'] or '').lower() == (ws['name'] or '').lower()})
        s = add('web-api', ws['name'], False, None, 'release', serviceId=ws['id'], service=masked(x, ws['service']))
        s['usedBy'] += [u for u in users if u not in s['usedBy']]
    for ev in x.items.get('environment-variable', []):
        add('environment-variable', ev['name'], False, None, 'release', type=ev['attrs'].get('type'), value=ev['attrs'].get('value'), description=ev.get('description', ''))
    for cd in x.items.get('credential', []):  # before the calls, which name a credential the release defines under the same key
        add('credential', cd['name'], False, None, 'release', definedInRelease=True, **{k: v for k, v in cd.items() if k not in ('name', 'id', 'attrs', 'members')},
            access=member_names(x, cd.get('members', [])))
    for c in x.items.get('calendar', []):
        add('calendar', c['name'], False, None, 'release', calendarId=c['id'], calendars=c['calendar'])
    for p in x.procs:
        for s in p.env_items():
            add('environment-variable' if s.exposure == 'Environment' else 'session-variable', s.name, False, p, f"«{p.page_name(s.page)}»", type=s.datatype)
        for act, c in credential_refs(x, p):
            add('credential', c, False, p, act)
        for act, c in calendar_refs(x, p):
            add('calendar', c, False, p, act)
        for coll, fields in p.collection_fields().items():
            if fields:
                add('collection', f"{p.name}::{coll}", False, p, 'expressions', fields=sorted(fields))
        for s in p.stages.values():
            if s.type == 'Action' and s.resource:
                obj, act = s.resource
                label = x.resource_label(obj, act)
                for i in s.inputs:
                    if not i['expr']:
                        continue
                    if DATA_INPUT_NAMES.search(i['name'] or '') and not re.fullmatch(r'\s*(True|False|\d+)\s*', i['expr']):
                        add('path', x.safe(i['expr'], i['name']), False, p, f"{label}.{i['name']}")
                    if re.search(r'\b(to|cc|bcc|recipient|subject)\b', i['name'] or '', re.I) and re.search(r'mail', act or '', re.I):
                        add('mail', f"{act} {i['name']}={x.safe(i['expr'], i['name'])}", False, p, label)
                if internal_family(obj) == 'Environment Locking':
                    tok = next((x.safe(i['expr']) for i in s.inputs if i['name'] in ('Name', 'Lock Name', 'Token') and i['expr']), '?')
                    add('environment-lock', tok, False, p, act)
                if act in ('Start Process', 'Run Process Until Ended', 'Start Process Read Stderr and Stdout', 'Kill Process', 'Launch'):
                    app = next((x.safe(i['expr']) for i in s.inputs if re.search(r'application|process|file|path|command|url', i['name'] or '', re.I) and i['expr']), '?')
                    add('external-process', f"{act} {app}", False, p, f"«{p.page_name(s.page)}» ‹{s.name}›")
            if s.type == 'Navigate':
                for stp in s.steps:
                    if stp['action'] in ('Launch', 'WebNavigateApplication', 'AttachApplication'):
                        args = {k: x.safe(v, k) for k, v in stp['args'].items() if v}
                        add('application-launch', f"{p.name}: {stp['action']} {written(args)}", False, p, f"«{p.page_name(s.page)}» ‹{s.name}›")
    rs = list(src.values())
    md = ["# Blue Prism data sources (secrets masked)\n", f"{len(rs)} sources across {len(x.procs)} processes and objects. Kinds: {dict(collections.Counter(s['kind'] for s in rs))}\n",
          "`drives: true` marks the data sets that drive steps (work queues); every other kind is provenance a Source Map row may cite.\n"]
    for kind in sorted({s['kind'] for s in rs}):
        md.append(f"\n## {kind}\n")
        for s in rs:
            if s['kind'] != kind:
                continue
            extra = {k: v for k, v in s.items() if k not in ('id', 'name', 'kind', 'usedBy', 'drives')}
            md.append(f"- ({s['id']}) `{s['name']}` — used by {', '.join(s['usedBy'])}" + (f" — {written(extra)}" if extra else ''))
    return pd, rs, '\n'.join(md)


# ----------------------------------------------------------------------------------------------------------------------
# Page shapes. A page is a graph of stages; most pages read as blocks - a sequence, branches that join, ordered choices,
# loops left at one place, a try / catch around a run of stages - and a few only as the graph. The classifier below is a
# pure function of a page's flow (kinds, links, recovery scopes), so `check` classifies its own reading of the XML with
# the same rules and compares.
SHAPE_REASONS = ('loop-multi-exit', 'jump-into-loop', 'shared-tail', 'shared-handler')
SHAPE_EXIT = '<exit>'
SCOPED_KINDS = {'step', 'decision', 'choice', 'wait', 'loop', 'loop-end', 'throw', 'rethrow'}  # the stages a Recover's scope holds


def shape_kind(stype, usecurrent=None):
    """The framework-agnostic kind of a flow stage in the shape catalog."""
    if stype == 'Exception':
        return 'rethrow' if usecurrent == 'yes' else 'throw'
    return {'Start': 'start', 'End': 'end', 'Decision': 'decision', 'ChoiceStart': 'choice', 'WaitStart': 'wait', 'LoopStart': 'loop',
            'LoopEnd': 'loop-end', 'Recover': 'recover', 'Resume': 'resume', 'Note': 'note'}.get(stype, 'step')


def short_ids(ids):
    """id -> the id as the renderings print it beside a repeated name: its first eight characters, the whole id where
    two ids of the page share them."""
    heads = collections.Counter(i[:8] for i in ids)
    return {i: i[:8] if heads[i[:8]] == 1 else i for i in ids}


def element_tags(elements):
    """element id -> ` #<short id>` for an element whose name another element of its model carries, so that a step
    names the element it acts on; nothing for a name the model holds once."""
    names = collections.Counter(e.name for e in elements)
    rep = [e.guid for e in elements if names[e.name] > 1 and e.guid]
    sid = short_ids(rep)
    return {g: f" #{sid[g]}" for g in rep}


def preds_of(succ):
    p = collections.defaultdict(set)
    for a, bs in succ.items():
        for b in bs:
            p[b].add(a)
    return p


def reduce_flow(succ, entry, boundary, sinks=frozenset(), tries=None, fold_loops=False):
    """Collapse the block-structured patterns of a flow graph until none applies: a sequence; branches of one node that
    meet at one node, a branch ending at a throw needing no meeting; a node that loops onto itself and leaves at one
    place; a try whose catch rejoins at the try's own next node or ends. `succ`: node -> successors; `boundary`: nodes
    never merged (the page's end, a scope's outside); `sinks`: the throws, which any number of branches may end at;
    `tries`: try node -> the node its failure leads to - a try's next node is never one of its branches, since a
    catch cannot reach past it. `fold_loops`: once nothing else applies, a loop entered at one node and left for one
    node is one region - for a test of where paths go, not of how a loop reads inside. Returns {region head: members},
    {region head: successors}, {open try: its failure}."""
    seen, todo = set(), [entry]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += list(succ.get(n, ()))
    S = {n: set(succ.get(n, ())) & seen for n in seen}
    P = preds_of(S)
    members = {n: {n} for n in sorted(seen, key=str) if n not in boundary}
    tries = {n: e for n, e in (tries or {}).items() if n in members and e in S[n]}

    def merge(n, m):
        """region m into region n"""
        members[n] |= members.pop(m)
        S[n].discard(m)
        for t in S.pop(m):
            P[t].discard(m)
            S[n].add(t)
            P[t].add(n)
        P.pop(m, None)
        if m in tries:
            tries[n] = tries.pop(m)

    def branch(n, b):
        """b can be taken into n as one of its branches"""
        return b in members and b not in (n, entry) and P[b] == {n} and b not in S[b] and b not in tries
    changed = True
    while changed:
        changed = False
        for n in list(members):
            if n not in members:
                continue
            if n in tries and tries[n] not in S[n]:  # its failure ends or was taken in: a closed try / catch
                del tries[n]
            if n in S[n] and len(S[n] - {n}) <= 1:  # a loop on one region, left at one place
                S[n].discard(n)
                P[n].discard(n)
                changed = True
            for t in sorted(S[n], key=str):  # a branch that ends: absorbed
                if t in members and t not in (n, entry) and not S[t] and (P[t] == {n} or t in sinks):
                    S[n].discard(t)
                    P[t].discard(n)
                    if not P[t]:
                        members[n] |= members.pop(t)
                        P.pop(t, None)
                        S.pop(t)
                        tries.pop(t, None)
                    changed = True
            if n in tries and tries[n] not in S[n]:
                del tries[n]
            if len(S[n]) == 1:
                (m,) = S[n]
                if m in members and m not in (n, entry) and P[m] == {n}:
                    tries.pop(n, None)
                    merge(n, m)
                    changed = True
                    continue
            if len(S[n]) >= 2:  # the branches that meet where the others go: taken in, the rest left for later
                if n in tries:
                    eligible = [tries[n]] if branch(n, tries[n]) else []
                else:
                    eligible = sorted((b for b in S[n] if branch(n, b)), key=str)
                direct = S[n] - set(eligible) - {n}
                if len(direct) > 1 or not eligible:
                    continue
                meets = sorted(direct) if direct else sorted({t for b in eligible for t in S[b] - {n}} | set(eligible), key=str)
                for j in meets:
                    take = [b for b in eligible if b != j and S[b] - {n} <= {j}]
                    if take:
                        if n in tries and tries[n] in take:
                            del tries[n]
                        for b in take:
                            merge(n, b)
                        changed = True
                        break
        if not changed and fold_loops:
            for c in strongly_connected(S, set(members)):
                if len(c) < 2:
                    continue
                entries = {v for v in c if v == entry or P[v] - c}
                if len(entries) != 1:
                    continue
                out, taken = loop_meets(S, P, c, boundary, sinks, tries, set())
                if len(out) <= 1:
                    (head,) = entries
                    whole = c | taken
                    for v in whole - {head}:
                        members[head] |= members.pop(v)
                        S.pop(v)
                        P.pop(v, None)
                        tries.pop(v, None)
                    tries.pop(head, None)
                    S[head] = set(out)
                    P[head] -= whole
                    for t in out:
                        P[t] = (P[t] - whole) | {head}
                    changed = True
                    break
    return members, S, tries


def strongly_connected(S, nodes):
    """The strongly connected components of the graph restricted to `nodes`, in a stable order (Tarjan, iterative)."""
    index, low, stack, on, out, n = {}, {}, [], set(), [], 0
    for root in sorted(nodes, key=str):
        if root in index:
            continue
        work = [(root, iter(sorted(set(S.get(root, ())) & nodes, key=str)))]
        index[root] = low[root] = n
        n += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(set(S.get(w, ())) & nodes, key=str))))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def own_stages(S, P, c, boundary, done):
    """(exits, own stages) of loop `c`: the nodes its links leave it for, and the nodes only the loop reaches after it
    leaves - those go into the branches of its breaks, up to the node where they meet. A loop over a collection
    continues after every break where its done link leads (`done`), so that node is never one of them."""
    exits = {t for v in c for t in S[v] if t not in c}

    def ahead(e):
        seen, todo = set(), [e]
        while todo:
            n = todo.pop()
            if n not in seen and n not in c and n not in boundary and n not in done:
                seen.add(n)
                todo += list(S.get(n, ()))
        return seen
    own, grow = set(), True
    while grow:
        grow = False
        for t in sorted(exits | {u for q in own for u in S[q]}, key=str):  # a link back from t's own loop is no other way in
            if t not in own and t not in c and t not in boundary and t not in done and P[t] - c - own <= ahead(t):
                own.add(t)
                grow = True
    return exits, own


def loop_meets(S, P, c, boundary, sinks, tries, done):
    """(where loop `c` is left for, the own stages its breaks take in): its exits, each with its own stages as a break's
    branch, reduced as far as they go - one node when the loop is left at one place."""
    exits, own = own_stages(S, P, c, boundary, done)
    if len(exits) <= 1:
        return exits, set()
    left = ('<left>',)  # the loop as one node: a name no flow node carries
    sub = {left: set(exits)}
    sub.update({q: set(S[q]) for q in own})
    edge = {t for ts in sub.values() for t in ts if t not in own}
    own_tries = {q: e for q, e in tries.items() if q in own}
    m2, s2, _ = reduce_flow(sub, left, edge, sinks, own_tries, fold_loops=True)
    if len(s2[left]) > 1:
        for j in sorted(edge | own, key=str):  # where the breaks meet: a jump to it from a break's branch is a break too
            m3, s3, _ = reduce_flow({v: ts - {j} for v, ts in sub.items() if v != j}, left, edge - {j}, sinks, own_tries, fold_loops=True)
            if not s3[left] and len(m3) == 1:
                return {j}, m3[left] - {left}
    return s2[left], m2[left] - {left}


def loop_left_at_one_place(S, P, c, boundary, sinks, tries, done):
    """Whether loop `c` is left at one place: its exits, each with its own stages as a break's branch, meet at one
    node - for a loop over a collection, the node its done link leads to."""
    meet, _ = loop_meets(S, P, c, boundary, sinks, tries, done)
    return len(meet) <= 1 and not (done and meet - done)


def nested_loop_reasons(S, P, c, head, boundary, sinks, tries, dones):
    """The reasons of the loops nested in loop `c` (head `head`), read on the whole graph, where a nested loop's exits
    to the loops around it stay apart: a break of two loops at once is a place of its own."""
    out = set()
    inner = {v: S[v] & (c - {head}) for v in c}
    for ic in strongly_connected(inner, set(c) - {head}):
        if len(ic) == 1 and next(iter(ic)) not in inner[next(iter(ic))]:
            continue
        entries = {v for v in ic if P[v] - ic}
        if len(entries) > 1:
            out.add('jump-into-loop')
            continue
        (inner_head,) = entries
        if not loop_left_at_one_place(S, P, ic, boundary, sinks, tries, {d for le, ls, d in dones if le in ic and ls == inner_head} - ic):
            out.add('loop-multi-exit')
        out |= nested_loop_reasons(S, P, ic, inner_head, boundary, sinks, tries, dones)
    return out


def shape_reasons(succ, entry, boundary, sinks, tries=None, dones=(), conditions=frozenset()):
    """Why the flow from `entry` does not reduce to one region - nothing when it does. Each loop that is left: a loop
    entered at two places; a loop left at two places; the loop's own body, its returns to its head and its exits
    being breaks and continues - except from inside a loop nested in it, which a break cannot leave in one step - and
    the loops taken as single nodes: a node branches reach that do not otherwise meet is a shared tail. `dones`:
    (loop end, loop start, the node its done link leads to) of the loops over a collection, a constraint on the loop
    whose head is that start; `conditions`: the decisions and
    loop ends - one that only returns to its loop's head or leaves the loop is the loop's condition, where a continue
    lands."""
    members, S, tries = reduce_flow(succ, entry, boundary, sinks, tries)
    if len(members) <= 1:
        return set()
    heads = set(members)
    head_of = {m: h for h, ms in members.items() for m in ms}
    dones = [(head_of.get(le, le), head_of.get(ls, ls), head_of.get(d, d)) for le, ls, d in dones]
    P = preds_of(S)
    reasons = set()
    comps = strongly_connected(S, heads)
    loops = [c for c in comps if len(c) > 1 or next(iter(c)) in S[next(iter(c))]]
    for c in loops:
        entries = {v for v in c if v == entry or P[v] - c}
        if len(entries) > 1:
            reasons.add('jump-into-loop')
            continue
        (head,) = entries
        if not loop_left_at_one_place(S, P, c, boundary, sinks, tries, {d for le, ls, d in dones if le in c and ls == head} - c):
            reasons.add('loop-multi-exit')
        reasons |= nested_loop_reasons(S, P, c, head, boundary, sinks, tries, dones)
        latch = {v for v in c - {head} if v in conditions and all(t == head or t not in c for t in S[v])  # what it took in ends there
                 and all(set(succ.get(m, ())) <= members[v] for m in members[v] - {v})}
        body = {v: {'<loop>' if t == head or t not in c or t in latch else t for t in S[v]} for v in c - latch}
        bp, inner = preds_of(body), set()
        for ic in strongly_connected({v: ts - {'<loop>'} for v, ts in body.items()}, set(body)):
            if len(ic) > 1 or next(iter(ic)) in body[next(iter(ic))]:
                inner |= ic | own_stages(body, bp, ic, {'<loop>'}, {d for le, ls, d in dones if le in ic and ls in ic} - ic)[1]
        for v in body:
            if v not in inner:
                body[v].discard('<loop>')
        body_tries = {v: ('<loop>' if e == head or e not in c or e in latch else e) for v, e in tries.items() if v in body}
        body_dones = [(le, ls, '<loop>' if d == head or d not in c or d in latch else d) for le, ls, d in dones if le in body]
        reasons |= shape_reasons(body, head, {'<loop>'}, sinks, {v: e for v, e in body_tries.items() if e in body[v]}, body_dones, conditions)
    comp_of = {v: i for i, c in enumerate(comps) for v in c}
    rep = {i: f"<loop {i}>" if c in loops else next(iter(c)) for i, c in enumerate(comps)}
    for i, c in enumerate(comps):  # a loop left at one place takes in its breaks' own stages
        if c in loops:
            heads_of_c = {v for v in c if v == entry or P[v] - c}
            done = {d for le, ls, d in dones if le in c and ls in heads_of_c} - c
            meet, taken = loop_meets(S, P, c, boundary, sinks, tries, done)
            if len(meet) <= 1 and not (done and meet - done):
                for v in taken:
                    comp_of[v] = i
    cond = collections.defaultdict(set)
    for v in heads:
        for t in S[v]:
            a, b = rep[comp_of[v]], (rep[comp_of[t]] if t in comp_of else t)
            if a != b:
                cond[a].add(b)
    cond_tries = {v: (rep[comp_of[e]] if e in comp_of else e) for v, e in tries.items() if rep[comp_of[v]] == v}
    cm, cs, ct = reduce_flow(cond, rep[comp_of[entry]], boundary, sinks, cond_tries)
    if len(cm) > 1:
        cp = preds_of(cs)
        joins = [h for h in cm if len(cp[h]) > 1]
        handler = [h for h in joins if all(ct.get(q) == h for q in cp[h])]  # runs sharing their handler: shared-handler
        if len(joins) > len(handler) or not (reasons or handler):
            reasons.add('shared-tail')
    return reasons


def scope_runs(scope, succ, preds, targets, sinks):
    """The runs of one recovery scope that a try / catch can hold whole: its stages split where a link enters other
    than at a run's first stage, or leaves towards two places. `targets`: stage -> the stages its links reach, Ends
    included; returns [{stages, entry, exits}] in a stable order."""
    sub = {s: {t if t in scope else ('out', t) for t in succ.get(s, ())} for s in scope}
    sub['<in>'] = {s for s in scope if preds.get(s, set()) - scope}
    boundary = {'<in>'} | {t for ts in sub.values() for t in ts if isinstance(t, tuple)}
    members, _, _ = reduce_flow(sub, '<in>', boundary, sinks, fold_loops=True)
    return [{'stages': sorted(ms, key=str), 'entry': head, 'exits': sorted({t for m in ms for t in targets.get(m, ()) if t not in ms}, key=str)}
            for head, ms in sorted(members.items(), key=lambda kv: str(kv[0]))]


def classify_flow(kinds, edges, start, scopes):
    """(shape, reasons, runs per Recover) of a page's flow. `kinds`: node id -> kind (`shape_kind`); `edges`:
    (from, to, kind) links, the implicit `loop` and `empty` links of a loop included, `exception` links ignored here;
    `scopes`: Recover id -> the stages it catches. Ends meet at one virtual end; the throws are ends that need no
    meeting; each run of a scope a try / catch can hold whole is one try node whose failure leads to its Recover, and
    a scope of several runs is a handler they share."""
    targets, succ = collections.defaultdict(set), collections.defaultdict(set)
    for a, b, k in edges:
        if k != 'exception':
            targets[a].add(b)
            succ[a].add(SHAPE_EXIT if kinds.get(b) == 'end' else b)
    live, todo = set(), [start]
    while todo:  # what can run: from the Start, and from each Recover once a stage it catches can
        while todo:
            n = todo.pop()
            if n not in live:
                live.add(n)
                todo += list(succ.get(n, ()))
        todo = [r for r, scope in scopes.items() if r not in live and scope & live]
    preds = preds_of({a: bs for a, bs in succ.items() if a in live})
    sinks = {v for v, k in kinds.items() if k in ('throw', 'rethrow')}
    runs, run_of = {}, {}
    for r, scope in sorted(scopes.items()):
        if not scope:
            continue
        runs[r] = scope_runs(set(scope), succ, preds, targets, sinks)
        for k, run in enumerate(runs[r]):
            for m in run['stages']:
                run_of[m] = f"<try {r} {k}>"
    graph = collections.defaultdict(set)
    for a, bs in list(succ.items()):
        for b in bs:
            na, nb = run_of.get(a, a), run_of.get(b, b)
            if na != nb:
                graph[na].add(nb)
    tries = {}
    for r, rs in runs.items():
        for k in range(len(rs)):
            graph[f"<try {r} {k}>"].add(r)
            tries[f"<try {r} {k}>"] = r
    conditions = {v for v, k in kinds.items() if k in ('decision', 'loop-end')}
    start_of = {a: b for a, b, k in edges if k == 'loop'}  # a loop's end -> its start
    all_dones = [(a, start_of.get(a), SHAPE_EXIT if kinds.get(b) == 'end' else b) for a, b, k in edges if k == 'done']
    dones = [(run_of.get(a, a), run_of.get(ls, ls), run_of.get(b, b)) for a, ls, b in all_dones]
    reasons = shape_reasons(graph, start, {SHAPE_EXIT}, sinks, tries, dones, conditions)
    for rs in runs.values():  # inside each run, as its try holds it
        for run in rs:
            inside = set(run['stages'])
            sub = {m: {t if t in inside else ('out', t) for t in succ.get(m, ())} for m in inside}
            reasons |= shape_reasons(sub, run['entry'], {t for ts in sub.values() for t in ts if isinstance(t, tuple)}, sinks,
                                     dones=[(a, ls, b if b in inside else ('out', b)) for a, ls, b in all_dones if a in inside], conditions=conditions)
    if any(len(rs) > 1 for rs in runs.values()):
        reasons.add('shared-handler')
    ordered = [r for r in SHAPE_REASONS if r in reasons]
    return ('graph' if ordered else 'blocks'), ordered, runs


def page_flow(p: Proc, page_id):
    """The flow of one page read from its stages: {stages by id, kinds of the reached flow stages and the page's notes,
    edges - links (from, to, kind) of the reached stages, through Anchors, linked Notes and a Choice's or Wait's end; a
    loop's start has an implicit `empty` link past the loop and its end a `loop` link back to its start; every stage a
    Recover catches has an `exception` link to it -, start, reached (stage ids, the ends and Anchors passed through
    included), scopes {Recover: the stages it catches}, handlers {Recover: the stages it runs in recovery}, rank (flow
    order)}. A Recover catches what a stage raises when the innermost Block holding the stage that has a Recover is its
    own Block, or, with none, when it lies in no Block; the stages it runs until a Resume are in recovery and raise to
    the caller."""
    stages = p.page_stages(page_id)
    by_id = {s.id: s for s in stages}
    group = collections.defaultdict(dict)
    for s in stages:
        if s.groupid:
            group[s.groupid].setdefault(s.type, s)

    def to(sid, passed):
        """the stage a link reaches, through Anchors, linked Notes and a Choice's or Wait's end, which go to `passed`"""
        hops = []
        while sid in by_id and by_id[sid].type in ('Anchor', 'Note', 'ChoiceEnd', 'WaitEnd') and sid not in hops:
            hops.append(sid)
            sid = by_id[sid].onsuccess
        passed += hops
        return sid if sid in by_id and sid not in hops else None
    links, via = collections.defaultdict(list), collections.defaultdict(list)
    for s in stages:
        t = s.type
        if t in NOT_FLOW or t in ('Anchor', 'Note', 'ChoiceEnd', 'WaitEnd', 'End', 'Exception'):
            continue
        out = []
        if t == 'Decision':
            out = [(s.ontrue, 'true'), (s.onfalse, 'false')]
        elif t in ('ChoiceStart', 'WaitStart'):
            out = [(ch['ontrue'], f"case:{ch['name']}") for ch in s.choices]
            end = group[s.groupid].get('ChoiceEnd' if t == 'ChoiceStart' else 'WaitEnd') if s.groupid else None
            if end is not None:
                via[s.id].append(end.id)
                out.append((end.onsuccess, 'otherwise' if t == 'ChoiceStart' else 'timeout'))
        elif t == 'LoopEnd':
            start = group[s.groupid].get('LoopStart') if s.groupid else None
            if start is not None:
                links[s.id].append((start.id, 'loop'))
            out = [(s.onsuccess, 'done')]
        else:
            out = [(s.onsuccess, 'resume' if t == 'Resume' else 'next')]
        end = group[s.groupid].get('LoopEnd') if t == 'LoopStart' and s.groupid else None
        if end is not None:  # an empty collection: straight past the loop
            via[s.id].append(end.id)
            out.append((end.onsuccess, 'empty'))
        for target, kind in out:
            b = to(target, via[s.id])
            if b is not None:
                links[s.id].append((b, kind))
    start = p.start_of(page_id)
    recovers = [s for s in stages if s.type == 'Recover']
    reached, passed = set(), set()

    def walk(sid):
        todo = [sid]
        while todo:
            n = todo.pop()
            if n not in reached:
                reached.add(n)
                passed.update(via.get(n, []))
                todo += [b for b, _ in links.get(n, [])]
    if start is not None:
        walk(start.id)
    while True:  # a Recover runs once a stage it covers is reached, as the dump walks it
        r = next((r for r in recovers if r.id not in reached and any(
            (s.id in reached or s.id in passed) and s.type in FLOW_TYPES and s.type not in ('Note', 'Anchor', 'Recover') and (r.block is None or r.block in s.blocks)
            for s in stages)), None)
        if r is None:
            break
        walk(r.id)
    handlers = {}
    for r in recovers:
        if r.id in reached:
            run, todo = set(), [r.id]
            while todo:
                n = todo.pop()
                if n not in run:
                    run.add(n)
                    if by_id[n].type != 'Resume':
                        todo += [b for b, _ in links.get(n, [])]
            handlers[r.id] = run
    in_recovery = set().union(*handlers.values()) if handlers else set()
    kinds = {sid: shape_kind(by_id[sid].type, (by_id[sid].exception or {}).get('usecurrent')) for sid in reached}
    kinds.update({s.id: 'note' for s in stages if s.type == 'Note'})
    scopes = {r: set() for r in handlers}
    for sid in reached:
        s = by_id[sid]
        if sid in in_recovery or kinds[sid] not in SCOPED_KINDS:
            continue
        mine = next((r for b in s.blocks for r in recovers if r.id in handlers and r.block is b), None) \
            or next((r for r in recovers if r.id in handlers and r.block is None), None)
        if mine is not None:
            scopes[mine.id].add(sid)
    order, todo = [], ([start.id] if start is not None else []) + [r.id for r in recovers if r.id in reached]
    while todo:  # flow order: from the Start along the links as written, then each Recover's path
        n = todo.pop(0)
        if n in order:
            continue
        order.append(n)
        todo[:0] = [b for b, _ in links.get(n, []) if b not in order]
    order += [sid for sid in by_id if sid not in order]
    rank = {sid: i for i, sid in enumerate(order)}
    edges = [(a, b, k) for a in sorted(reached, key=rank.get) for b, k in links.get(a, [])]
    edges += [(sid, r, 'exception') for r, sc in sorted(scopes.items(), key=lambda kv: rank[kv[0]]) for sid in sorted(sc, key=rank.get)]
    return {'stages': by_id, 'kinds': kinds, 'edges': edges, 'start': start.id if start else None, 'reached': reached | passed,
            'scopes': scopes, 'handlers': handlers, 'rank': rank}


def coordinate(v):
    """A stage position as the catalogs give it: a whole number as one."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


NAMED_FLOW = FLOW_TYPES - {'Note', 'Anchor'}  # the flow stages a rendering names: a note prints its text, an anchor nothing


def repeated_names(stages):
    """The names more than one named flow stage of a page carries: those stages are named with their short id."""
    return {n for n, c in collections.Counter(s.name for s in stages if s.type in NAMED_FLOW).items() if c > 1}


def shapes(x: Export):
    """The shape catalog: per process and object, per page with a Start, its shape (`blocks` or `graph`) with the
    reasons, its nodes (id, name, type, kind, Block, region and run, centre), its links, its recovery regions (the Recover, its Block, the stages it catches split
    into runs a try / catch can hold whole, its handler stages and Resumes) and its unlinked stages; and its Markdown
    rendering - every page's shape, and a graph page's nodes, links and regions, a repeated name with its short id."""
    cat, md = [], []
    for p in x.procs:
        for pid, pname, _ in p.pages():
            flow = x.flow(p, pid)
            if flow['start'] is None:
                continue
            by_id, kinds, edges, rank = flow['stages'], flow['kinds'], flow['edges'], flow['rank']
            shape, reasons, runs_of = classify_flow(kinds, edges, flow['start'], flow['scopes'])
            regions, place = [], {}
            for n, (r, rs) in enumerate(sorted(runs_of.items(), key=lambda kv: rank[kv[0]])):
                rec, runs = by_id[r], sorted(rs, key=lambda run: rank[run['entry']])
                for k, run in enumerate(runs):
                    for m in run['stages']:
                        place[m] = (n, k)
                b = rec.block
                regions.append({'recover': r, 'name': rec.name,
                                'block': {'id': b.id, 'name': b.name, 'x': coordinate(b.x), 'y': coordinate(b.y), 'w': coordinate(b.w), 'h': coordinate(b.h)} if b else None,
                                'scope': sorted(flow['scopes'][r], key=rank.get),
                                'runs': [{'stages': sorted(run['stages'], key=rank.get), 'entry': run['entry'], 'exits': sorted(run['exits'], key=rank.get)} for run in runs],
                                'handler': sorted(flow['handlers'][r], key=rank.get),
                                'resumes': [{'id': h, 'continuation': next((t for a, t, kd in edges if a == h and kd == 'resume'), None)}
                                            for h in sorted(flow['handlers'][r], key=rank.get) if by_id[h].type == 'Resume']})
            sid = short_ids(list(by_id))
            repeated = repeated_names(by_id.values())
            nodes = []
            for n in sorted(kinds, key=rank.get):
                st = by_id[n]
                node = {'id': n, 'shortId': sid[n], 'name': st.name, 'type': st.type, 'kind': kinds[n], 'block': st.block.name if st.block else None,
                        'region': place[n][0] if n in place else None, 'run': place[n][1] if n in place else None, 'x': coordinate(st.x), 'y': coordinate(st.y)}
                nodes.append(node)
            unlinked = [{'id': s.id, 'name': s.name, 'type': s.type} for s in by_id.values()
                        if s.id not in flow['reached'] and s.type in FLOW_TYPES and s.type != 'Anchor' and not (s.type == 'Note' and not s.onsuccess)]
            cat.append({'object': p.name, 'objectId': p.id, 'objectKind': p.kind, 'page': pname, 'pageId': pid, 'shape': shape, 'reasons': reasons,
                        'nodes': nodes, 'edges': [{'from': a, 'to': b, 'kind': k} for a, b, k in edges], 'regions': regions, 'unlinked': unlinked})

            def label(n):
                return f"‹{by_id[n].name}› [{by_id[n].type}]" + (f" #{sid[n]}" if by_id[n].name in repeated else '')
            if shape == 'blocks':
                md.append(f"- `{p.name}` ({p.id}) «{pname}» — blocks")
                continue
            md.append(f"\n## `{p.name}` ({p.id}) «{pname}» — graph ({', '.join(reasons)})\n")
            md.append("nodes, in flow order (kind, centre x y, the region and run of a stage a Recover catches):")
            for nd in nodes:
                md.append(f"- {label(nd['id'])} {nd['kind']}" + (f" ({nd['x']}, {nd['y']})" if nd['x'] is not None and nd['y'] is not None else ' (no position)')
                          + (f" region {nd['region'] + 1} run {nd['run'] + 1}" if nd['region'] is not None else ''))
            md.append("links:")
            md += [f"- {label(a)} → {label(b)} ({k})" for a, b, k in edges if k != 'exception']
            md.append("regions:")
            for n, rg in enumerate(regions, 1):
                md.append(f"- region {n}: {label(rg['recover'])} catches " + (f"block «{rg['block']['name']}»" if rg['block'] else 'the whole page')
                          + '; runs: ' + '; '.join(f"{k}. {', '.join(label(m) for m in run['stages'])} → {', '.join(label(e) for e in run['exits']) or 'nothing'}"
                                                   for k, run in enumerate(rg['runs'], 1))
                          + '; resumes: ' + (', '.join(f"{label(rs['id'])} → {label(rs['continuation']) if rs['continuation'] else 'nothing'}"
                                                       for rs in rg['resumes']) or 'none'))
    head = ["# Blue Prism page shapes\n", f"{sum(1 for c in cat if c['shape'] == 'graph')} of {len(cat)} pages are graphs. Reasons: "
            + ', '.join(f"{r} ({sum(1 for c in cat if r in c['reasons'])})" for r in SHAPE_REASONS) + "\n"]
    return cat, '\n'.join(head + md)


# ----------------------------------------------------------------------------------------------------------------------
# Schema coverage. The renderers read the elements they know; an element or attribute a newer Blue Prism writes, or one
# no release has exercised yet, would be skipped in silence. Every path at which a release holds a value must therefore
# be documented in the pack's schema.md, with what it means and whether a rendering shows it.
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'schema.md')
# (type of an element, child tag) -> type of the child: paths restart at each typed element, so a stage, an application
# model element or a collection row has the same path wherever it sits
SCHEMA_TYPES = {('RELEASE', 'contents'): 'CONTENTS', ('CONTENTS', 'process'): 'WRAPPER', ('CONTENTS', 'object'): 'WRAPPER',
                ('WRAPPER', 'process'): 'PROC', ('WRAPPER', 'object'): 'PROC', ('PROC', 'stage'): 'STAGE', ('PROC', 'subsheet'): 'SUBSHEET',
                ('PROC', 'appdef'): 'APPDEF', ('APPDEF', 'element'): 'ELEMENT', ('APPDEF', 'group'): 'GROUP', ('ELEMENT', 'element'): 'ELEMENT',
                ('ELEMENT', 'group'): 'GROUP', ('GROUP', 'element'): 'ELEMENT', ('GROUP', 'group'): 'GROUP', ('ELEMENT', 'region-container'): 'ELEMENT',
                ('GROUP', 'region-container'): 'ELEMENT', ('APPDEF', 'region-container'): 'ELEMENT', ('ELEMENT', 'region'): 'ELEMENT',
                ('STAGE.initialvalue', 'row'): 'ROW', ('STAGE.initialvalue', 'singlerow'): 'ROW',
                ('ROW', 'field'): 'FIELD', ('FIELD', 'row'): 'ROW'}
XML_NS = '{http://www.w3.org/XML/1998/namespace}'


def schema_paths(el, t):
    """(typed path, value) for every attribute (`T@name`), every text (`T`) and every bare element (`T`, value
    `<present>` - an empty element is often a flag) under `el`, an element of type `t`. A child of the release's
    contents is typed by its kind (`ITEM:work-queue`)."""
    for k, v in el.attrib.items():
        yield f"{t}@{'xml:' + k[len(XML_NS):] if k.startswith(XML_NS) else local(k)}", v
    txt = (el.text or '').strip()
    if txt:
        yield t, txt
    elif not el.attrib and len(el) == 0:
        yield t, '<present>'
    for c in el:
        yield from schema_paths(c, SCHEMA_TYPES.get((t, c.tag)) or (f"ITEM:{c.tag}" if t == 'CONTENTS' else f"{t}.{c.tag}"))


def schema_rows(path=SCHEMA_FILE):
    """The documented paths as [(glob pattern, status, allowed values or None)], in table order, from every table row
    of schema.md whose first cell holds backticked paths. `ignored while `a` `b` - …` allows only those values."""
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
    """The status of the first row whose pattern matches the path - only `*` is special."""
    for pat, status, allowed in rows:
        if re.fullmatch('.*'.join(re.escape(part) for part in pat.split('*')), path):
            return status, allowed
    return None, None


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


def _present(x: Export, s, text, name='', as_json=False):
    """Is value `s` in `text` - as the text renderings write it (as written, invisible characters `visible`, line
    breaks as ` / ` in free text and ` ⏎ ` in an expression) or, in a JSON output (`as_json`), also JSON-escaped; or
    masked as a secret? A text rendering never escapes: a value found there only repr- or JSON-escaped is a miss, the
    reader copies the escapes. A candidate of four characters or fewer must stand alone, so that `1` is not found
    inside a stage id."""
    bases = (s, s.strip())
    cands = {form for b in bases for form in (b, oneline(b), expr(b))}
    cands |= {visible(c) for c in cands}
    if as_json:
        cands |= {json.dumps(b, ensure_ascii=False)[1:-1] for b in bases}
    else:  # base64 bytes by their size: the JSON outputs keep them whole
        blobs = {md_blob(c) for c in list(cands)}
        cands |= blobs | {visible(b) for b in blobs}
    cands |= {x.safe(c, name) for c in list(cands)} | {x.safe(c) for c in list(cands)}
    for c in cands:
        if not c:
            continue
        if len(c) <= 4:
            if re.search(r'(?<![A-Za-z0-9_])' + re.escape(c) + r'(?![A-Za-z0-9_])', text):
                return True
        elif c in text:
            return True
    return False


def raw_declarations(p: Proc):
    """Every application-model element, region and window container, read from the object's own `appdef`."""
    return [e for e in p.appdef.iter() if e.tag in ('element', 'region') or is_window_container(e)] if p.appdef is not None else []


def raw_element_names(p: Proc):
    """Application-model element or region id -> name, read from the object's own `appdef`."""
    return {e.findtext('id'): e.get('name') or '' for e in raw_declarations(p)}


# the stage types `Export.render` prints field by field; any other type prints every value its XML holds
FIELD_RENDERED = {'Start', 'End', 'Action', 'Calculation', 'MultipleCalculation', 'Decision', 'ChoiceStart', 'ChoiceEnd', 'LoopStart', 'LoopEnd',
                  'SubSheet', 'Process', 'Exception', 'Recover', 'Resume', 'Note', 'Anchor', 'Navigate', 'Read', 'Write', 'WaitStart', 'WaitEnd', 'Code'}


def raw_stock_narratives(procs):
    """(object in lower case, action, narrative) of the calls to a Blue Prism internal object whose narrative another
    call of the same action repeats - the vendor's description, copied into every call - read from the XML."""
    classes = {k.split('.')[-1].lower() for k in INTERNAL}
    seen = collections.Counter()
    for p in procs:
        for st in p.inner.findall('stage'):
            res, nar = st.find('resource'), (st.findtext('narrative') or '').strip()
            obj = (res.get('object') or '') if res is not None else ''
            if st.get('type') == 'Action' and nar and (obj.split('.')[-1].lower() in classes or re.match(r'(?i)blue ?prism\.', obj)):
                seen[(obj.lower(), res.get('action'), nar)] += 1
    return {k for k, n in seen.items() if n > 1}


def raw_loopdata(st):
    """The collection a LoopStart walks: `<loopdata>` text or its `<collection name>` child."""
    c = st.find('loopdata/collection')
    return (st.findtext('loopdata') or '').strip() or (c.get('name') if c is not None else None)


def raw_values(st):
    """(path, value, masking name) of every attribute and text inside a stage but its layout, links and narrative -
    what the rendering of a type no renderer reads field by field must show. A parameter's value is masked by its name."""
    out = []

    def walk(e, path, name):
        for k, v in e.attrib.items():
            if path and v not in (None, ''):
                out.append((f"{path}@{k}", v, name if k in ('expr', 'expression', 'value') else ''))
        if path and (e.text or '').strip():
            out.append((path, e.text.strip(), name))
        for ch in e:
            if path or ch.tag not in ('display', 'displayx', 'displayy', 'displaywidth', 'displayheight', 'font', 'loginhibit', 'subsheetid', 'onsuccess', 'narrative'):
                walk(ch, f"{path}.{ch.tag}" if path else ch.tag, ch.get('name') if ch.tag in ('input', 'output', 'field') else (ch.get('stage') if ch.tag == 'calculation' else name))
    walk(st, '', '')
    return out


def stage_expectations(st, names, procs_by_guid, pages, stock=frozenset()):
    """(field, value, masking name) for every value the XML of stage `st` carries that its dump rendering must show -
    read from the element itself, never through `Stage`. `names`: element id -> name; `procs_by_guid`: a Process stage's
    callee; `pages`: subsheet id -> page name; `stock`: the internal-action narratives that are the vendor's."""
    t = st.get('type')
    out = []

    def add(field, v, name=''):
        if v not in (None, ''):
            out.append((field, v, name))
    if t not in ('Note', 'Anchor', 'ChoiceEnd', 'WaitEnd', 'Data', 'Collection', 'Block', 'SubSheetInfo', 'ProcessInfo') \
            and not (t == 'Start' and st.get('name') == 'Start') and not (t == 'End' and st.get('name') == 'End'):
        add('name', st.get('name'))
    res = st.find('resource')
    nar = (st.findtext('narrative') or '').strip()
    vendor = res is not None and ((res.get('object') or '').lower(), res.get('action'), nar) in stock
    if t not in ('Anchor', 'ChoiceEnd', 'WaitEnd', 'LoopEnd', 'Resume', 'Data', 'Collection', 'Block', 'SubSheetInfo', 'ProcessInfo') and not vendor:
        add('narrative', nar)
    if t not in FIELD_RENDERED:
        for path, v, name in raw_values(st):
            add(f"value {path}", v, name)
        return out
    if t in ('Start', 'End'):
        for side in ('inputs/input', 'outputs/output'):
            for prm in st.findall(side):
                for k in ('name', 'type', 'stage', 'narrative'):
                    add(f"{side.split('/')[1]}@{k}", prm.get(k), prm.get('name') or '')
        for c in st.findall('preconditions/condition') + st.findall('postconditions/condition'):
            add('condition@narrative', c.get('narrative'))
    if t in ('Action', 'SubSheet', 'Process', 'Code'):
        for prm in st.findall('inputs/input'):
            if prm.get('expr'):
                add('input@name', prm.get('name'))
                add('input@expr', prm.get('expr'), prm.get('name') or '')
            elif t != 'Code':  # a code stage lists every parameter with its type
                add('blank input', prm.get('name'))
        for prm in st.findall('outputs/output'):
            if prm.get('stage'):
                add('output@name', prm.get('name'))
                add('output@stage', prm.get('stage'))
    if res is not None:
        add('resource@object', f"ACTION {raw_action_label(res.get('object') or '')}.{res.get('action')}(")  # whole: a family's name can sit inside its class name
        add('resource@action', res.get('action'))
    if t == 'SubSheet':
        add('processid', pages.get(st.findtext('processid')))
    if t == 'Process':
        callee = procs_by_guid.get(st.findtext('processid'))
        add('processid', f"({callee.id})" if callee else st.findtext('processid'))
    for c in st.findall('calculation') + st.findall('steps/calculation'):
        add('calculation@stage', c.get('stage'))
        add('calculation@expression', c.get('expression'), c.get('stage') or '')
    dec = st.find('decision')
    if dec is not None:
        add('decision@expression', dec.get('expression'))
    add('looptype', st.findtext('looptype'))
    add('loopdata', raw_loopdata(st))
    add('timeout', st.findtext('timeout'))
    add('attempts', st.findtext('attempts'))
    add('@interval', st.get('interval'))
    ex = st.find('exception')
    if ex is not None:
        if ex.get('usecurrent') == 'yes':
            add('exception@usecurrent', 'RETHROW')
        else:
            add('exception@type', ex.get('type'))
            add('exception@detail', ex.get('detail'))
        if ex.get('savescreencapture') == 'yes':
            add('exception@savescreencapture', '[screen capture]')
    for stp in st.findall('step'):
        el = stp.find('element')
        add('step.element', names.get(el.get('id')) if el is not None else None)
        add('step@expr', stp.get('expr'), names.get(el.get('id')) if el is not None else '')
        add('step@stage', stp.get('stage'))
        add('step.action.id', stp.findtext('action/id'))
        for a in stp.findall('action/arguments/argument') + stp.findall('action/outputs/output'):
            add('step.action.argument.value', a.findtext('value'), a.findtext('id') or '')
        for ep in (el.findall('elementparameter') if el is not None else []):
            add('elementparameter.name', ep.findtext('name'))
            add('elementparameter.expression', ep.findtext('expression'))
            if ep.findtext('comparisontype') not in (None, '', 'Equal'):
                add('elementparameter.comparisontype', ep.findtext('comparisontype'))
    if t == 'Code':  # the parameters' types (the stage defines them), the body line by line under `--full`
        for prm in st.findall('inputs/input') + st.findall('outputs/output'):
            add('parameter@type', prm.get('type'))
        for ln in (st.findtext('code') or '').splitlines():
            add('code line', ln.strip())
    return out


def raw_action_label(obj):
    """The object of an Action stage as the renderings name it, from its `resource@object`: an internal object by its
    family, named with or without its namespace; another object inside the product by its class; any other as written."""
    by_class = {k.split('.')[-1].lower(): v for k, v in INTERNAL.items()}
    return by_class.get(obj.split('.')[-1].lower()) or (obj.split('.')[-1] if re.match(r'(?i)blue ?prism\.', obj) else obj)


def case_expectations(st, ch, names):
    """(field, value, masking name) a Choice or Wait branch carries that its case line must show."""
    out = [('choice.name', ch.findtext('name'), '')]
    if st.get('type') == 'ChoiceStart':
        out.append(('choice@expression', ch.get('expression'), ''))
    else:
        el = ch.find('element')
        out += [('choice.element', names.get(el.get('id')) if el is not None else None, ''), ('choice.condition.id', ch.findtext('condition/id'), '')]
        out += [('choice.condition.argument.value', a.findtext('value'), a.findtext('id') or '') for a in ch.findall('condition/arguments/argument')]
        if ch.get('reply') not in (None, '', 'True') or ch.findtext('comparetype') not in (None, '', 'Equal'):
            out += [('choice@reply', ch.get('reply'), ''), ('choice.comparetype', ch.findtext('comparetype'), '')]
        for ep in (el.findall('elementparameter') if el is not None else []):
            out += [('elementparameter.name', ep.findtext('name'), ''), ('elementparameter.expression', ep.findtext('expression'), '')]
    return [(f, v, n) for f, v, n in out if v not in (None, '')]


def item_expectations(st, x: Export):
    """(field, value, masking name) a data item or collection carries that its entry on the page header must show."""
    name, dtype = st.get('name') or '', st.findtext('datatype')
    out = [('name', name, '')] + ([('datatype', dtype, '')] if st.get('type') == 'Data' else [])  # a collection's type is its entry
    iv = st.find('initialvalue')
    if st.get('type') == 'Data' and iv is not None and (iv.text or '').strip():
        out.append(('initialvalue', '***' if sensitive_item(dtype, name) else iv.text, name))
    if (st.findtext('initialvalueenc') or '').strip():
        out.append(('initialvalueenc', '***', ''))
    out.append(('exposure', st.findtext('exposure'), ''))
    if st.find('private') is None:
        out.append(('private', '[shared]', ''))
    if st.find('alwaysinit') is None:
        out.append(('alwaysinit', '[no reset]', ''))
    for f in (st.find('collectioninfo').iter('field') if st.find('collectioninfo') is not None else []):  # a nested collection's fields too
        out += [('collectioninfo.field@name', f.get('name'), ''), ('collectioninfo.field@type', f.get('type'), '')]
    if st.find('collectioninfo/singlerow') is not None or st.find('singlerow') is not None:
        out.append(('collectioninfo.singlerow', '[single row]', ''))
    return [(f, v, n) for f, v, n in out if v not in (None, '')]


def raw_geo(e):
    """(x, y, w, h) of a stage from the XML - `<display x y w h>` (Blue Prism 7) or `displayx` ... elements (Blue
    Prism 6) - or None."""
    d = e.find('display')
    vals = (d.get('x'), d.get('y'), d.get('w'), d.get('h')) if d is not None and d.get('x') is not None else \
        tuple(e.findtext(k) for k in ('displayx', 'displayy', 'displaywidth', 'displayheight'))
    try:
        return tuple(float(v) if v not in (None, '') else 0.0 for v in vals) if vals[0] not in (None, '') and vals[1] not in (None, '') else None
    except ValueError:
        return None


def raw_blocks_around(st, blocks):
    """The Block stages whose rectangle - top-left corner and extent - holds the stage's centre, smallest first."""
    here = raw_geo(st)
    if here is None:
        return []
    inside = [(g[2] * g[3], n, b) for n, (b, g) in enumerate((b, raw_geo(b)) for b in blocks) if g and g[0] <= here[0] <= g[0] + g[2] and g[1] <= here[1] <= g[1] + g[3]]
    return [b for _, _, b in sorted(inside, key=lambda i: i[:2])]


def raw_scope(st, blocks, stages):
    """What a Recover stage's rendering names as its scope, from the XML: the innermost Block holding it with the
    stages that Block holds (not Anchors, Notes, Recovers or data; top to bottom, then left to right), `whole page`
    when none does. `blocks`, `stages`: the page's Block stages and all its stages."""
    around = raw_blocks_around(st, blocks)
    if not around:
        return 'whole page'
    b = around[0]
    skip = ('Data', 'Collection', 'Block', 'SubSheetInfo', 'ProcessInfo', 'Anchor', 'Note', 'Recover')
    held = sorted(((raw_geo(s)[1], raw_geo(s)[0], s.get('name') or '', s.get('type')) for s in stages
                   if s.get('type') not in skip and any(o is b for o in raw_blocks_around(s, blocks))), key=lambda h: h[:3])
    return f"block «{b.get('name')}» holding " + (', '.join(f"‹{n}› [{t}]" for _, _, n, t in held) if held else 'no stage')


def raw_reached(stages):
    """The ids of the stages of one page that can run, from the XML: from the page's Start, every link (`onsuccess`,
    `ontrue`, `onfalse`, a choice's `ontrue`), from a Choice, Wait or Loop start to the end that shares its `groupid`,
    and into a Recover once a stage it covers is reached - one in the Recover's Block, or with no Block any stage of
    the page."""
    by = {st.get('stageid'): st for st in stages}
    ends = collections.defaultdict(dict)
    for st in stages:
        if st.findtext('groupid'):
            ends[st.findtext('groupid')][st.get('type')] = st.get('stageid')
    partner = {'ChoiceStart': 'ChoiceEnd', 'WaitStart': 'WaitEnd', 'LoopStart': 'LoopEnd'}
    blocks = [b for b in stages if b.get('type') == 'Block']
    around = {st.get('stageid'): raw_blocks_around(st, blocks) for st in stages}
    reached, todo = set(), [st.get('stageid') for st in stages if st.get('type') == 'Start']
    while todo:
        while todo:
            sid = todo.pop()
            if sid in reached or sid not in by:
                continue
            reached.add(sid)
            st = by[sid]
            todo += [n for n in [st.findtext(k) for k in ('onsuccess', 'ontrue', 'onfalse')] + [c.findtext('ontrue') for c in st.findall('choices/choice')] if n]
            if st.get('type') in partner and ends[st.findtext('groupid') or ''].get(partner[st.get('type')]):
                todo.append(ends[st.findtext('groupid')][partner[st.get('type')]])
        for r in stages:
            if r.get('type') != 'Recover' or r.get('stageid') in reached:
                continue
            own = around[r.get('stageid')][0] if around[r.get('stageid')] else None
            if any(s.get('stageid') in reached and s.get('type') in FLOW_TYPES and s.get('type') not in ('Note', 'Anchor', 'Recover')
                   and (own is None or own in around[s.get('stageid')]) for s in stages):
                todo.append(r.get('stageid'))
                break
    return reached


def raw_is_object(p):
    """Is `p` a business object, read from its XML for `check`: an `object` item, or a process element typed so?"""
    return p.wrapper.tag == 'object' or p.inner.get('type') == 'object'


def raw_reach(x: Export):
    """The reach `check` expects, read from the XML by the rule `Reach` states: {(process or object, page id): status},
    {(process or object, stage id): status} and {(process or object, page id): [(caller, stage element)]}. A page's
    stages run as `raw_reached` walks them; a SubSheet stage calls the page its `processid` names, a Process stage the
    process its `processid` names, an Action stage the object its `resource@object` names (case-insensitively) - the
    page named exactly as its `resource@action` and the Initialise and Clean Up pages."""
    by_guid = {p.guid: p for p in x.procs}
    on, linked, page_of, calls = collections.defaultdict(list), {}, {}, {}
    for p in x.procs:
        for st in p.inner.findall('stage'):
            on[(p, st.findtext('subsheetid'))].append(st)
            page_of[(p, st.get('stageid'))] = st.findtext('subsheetid')
    units = [(p, pid) for p in x.procs for pid in [None] + [sh.get('subsheetid') for sh in p.inner.findall('subsheet')]]
    for p, pid in units:
        got = raw_reached(on[(p, pid)])
        linked.update({(p, st.get('stageid')): st.get('stageid') in got for st in on[(p, pid)]})
    for p in x.procs:
        sheets = {sh.get('subsheetid') for sh in p.inner.findall('subsheet')}
        for st in p.inner.findall('stage'):
            t, res, target = st.get('type'), st.find('resource'), st.findtext('processid')
            if t == 'SubSheet' and target in sheets:
                calls[(p, st)] = [(p, target)]
            elif t == 'Process' and target in by_guid:
                calls[(p, st)] = [(by_guid[target], None)]
            elif t == 'Action' and res is not None and x.by_name_ci.get((res.get('object') or '').lower()) is not None:
                o = x.by_name_ci[(res.get('object') or '').lower()]
                if raw_is_object(o):
                    calls[(p, st)] = [(o, sh.get('subsheetid')) for sh in o.inner.findall('subsheet')
                                      if (sh.findtext('name') or '') == res.get('action') or sh.get('type') == 'CleanUp'] + [(o, None)]
    callers = collections.defaultdict(list)
    for c, targets in calls.items():
        for u in targets:
            callers[u].append(c)
    processes = [p for p in x.procs if not raw_is_object(p)]
    if processes:
        todo = [(p, None) for p in processes if all(c is p for c, _ in callers.get((p, None), []))]
    else:
        todo = []
        for o in x.procs:
            published = [sh.get('subsheetid') for sh in o.inner.findall('subsheet') if (sh.get('published') or '').lower() == 'true']
            if published:
                todo += [(o, pid) for pid in published + [None] + [sh.get('subsheetid') for sh in o.inner.findall('subsheet') if sh.get('type') == 'CleanUp']]
    def closure(todo, stop=frozenset()):
        seen = set()
        while todo:
            u = todo.pop()
            if u in seen or u in stop:
                continue
            seen.add(u)
            for st in on[u]:
                if linked.get((u[0], st.get('stageid'))):
                    todo += calls.get((u[0], st), [])
        return seen
    runs = closure(todo)
    unused = closure([u for u in units if u not in runs and all((c, st.findtext('subsheetid')) == u for c, st in callers.get(u, []))], runs)
    page = {u: 'runs' if u in runs else 'unused' if u in unused else 'dead' for u in units}
    stage = {k: (page.get((k[0], page_of[k]), 'dead') if ok else 'dead') for k, ok in linked.items()}
    return page, stage, callers


def marked(text, ident, status):
    """Does `text` show `ident` with the reach mark of `status` - and, for a stage that runs, with none?"""
    want = ident + REACH_MARK[status]
    at = text.find(want)
    while at >= 0:
        if status != 'runs' or not text.startswith((' [unused]', ' [dead]'), at + len(want)):
            return True
        at = text.find(want, at + 1)
    return False


def raw_item_values(it, names):
    """(path, value) of every attribute and text of a release item, read from its XML - a member by the name of the
    process or object its id gives (`names`: GUID -> name), a credential without its GUID."""
    for e in it.iter():
        for k, v in e.attrib.items():
            if e is it and k == 'id' and it.tag == 'credential':
                continue
            yield f"{e.tag}@{k}", (names.get(v, f"{e.tag}:{v}") if e.tag in ('process', 'object') and k == 'id' else v)
        if (e.text or '').strip():
            yield e.tag, e.text.strip()


def raw_label(st):
    """The action a stage stands for in a trace of an Item ID: a Work Queues action by its name, another action by
    object and action, any other stage by its type."""
    res = st.find('resource')
    if st.get('type') != 'Action' or res is None:
        return st.get('type')
    return res.get('action') if internal_family(res.get('object') or '') == 'Work Queues' else f"{res.get('object')}.{res.get('action')}"


def raw_origins(p: Proc):
    """`came_from(page, expression, seen)` for `check`, read from the process's XML: the stages (elements) that write the
    data item an expression starts with through an output - an End stage's outputs only name what it returns - and the
    texts for what a trace cannot follow, through calculations and the inputs a page's callers bind."""
    stages = p.inner.findall('stage')
    items = [st for st in stages if st.get('type') in ('Data', 'Collection')]
    own = {(st.findtext('subsheetid'), st.get('name')): st.get('stageid') for st in items}
    shared = {st.get('name'): st.get('stageid') for st in items if st.find('private') is None}

    def scope(page, name):
        return own.get((page, name)) or shared.get(name) or (page, name)
    writes = collections.defaultdict(list)
    for st in stages:
        pg = st.findtext('subsheetid')
        for o in st.findall('outputs/output') if st.get('type') != 'End' else []:
            if o.get('stage'):
                writes[scope(pg, o.get('stage'))].append((st, None))
        for i in st.findall('inputs/input') if st.get('type') == 'Start' else []:
            if i.get('stage'):
                writes[scope(pg, i.get('stage'))].append((st, i.get('name')))
        for c in st.findall('calculation') + st.findall('steps/calculation'):
            if c.get('stage'):
                writes[scope(pg, c.get('stage'))].append((st, c.get('expression') or ''))

    def came_from(page, ex, seen):
        m = re.match(r'\s*\[([^\].]+)', ex or '')
        key = scope(page, m.group(1)) if m else None
        if key is None or key in seen:
            return set()
        out = set()
        for st, how in writes.get(key, ()):
            pg = st.findtext('subsheetid')
            if st.get('type') == 'Start':
                bound = [(c.findtext('subsheetid'), i.get('expr')) for c in stages if c.get('type') == 'SubSheet' and pg and c.findtext('processid') == pg
                         for i in c.findall('inputs/input') if i.get('name') == how and i.get('expr')]
                out |= set().union(*(came_from(cp, e, seen | {key}) or {"a caller's expression"} for cp, e in bound)) or {'a page input' if pg else 'a process input'}
            elif how is not None:
                out |= came_from(pg, how, seen | {key}) or {st.get('type')}
            else:
                out.add(st)
        return out
    return came_from


def raw_names(stages):
    """`name_of(expression)` for `check`, read from a process's stages: the name an input gives a queue, a credential or
    a calendar - a text literal's text, the literal initial value of the data item it names, any other expression as
    written."""
    literal = {st.get('name'): (st.findtext('initialvalue') or '') for st in stages
               if st.get('type') == 'Data' and (st.findtext('initialvalue') or '') and not sensitive_item(st.findtext('datatype'), st.get('name'))}

    def name_of(e):
        lit = re.fullmatch(r'\s*"((?:[^"]|"")*)"\s*', e)
        ref = re.fullmatch(r'\s*\[([^\]]+)\]\s*', e)
        return lit.group(1).replace('""', '"') if lit else literal.get(ref.group(1), e) if ref else e
    return name_of


def raw_queue_stages(p: Proc):
    """{stage id: the queue names it acts on} for the Work Queues stages of a process, read from its XML for `check`:
    the queue a stage names (a text literal, or the literal initial value of the data item it names), else the queues of
    the Work Queues stages its Item ID is traced back to."""
    stages = p.inner.findall('stage')
    came_from = raw_origins(p)
    name_of = raw_names(stages)

    def named(st):
        e = next((i.get('expr') for i in st.findall('inputs/input') if i.get('name') == 'Queue Name' and i.get('expr')), None)
        return None if e is None else name_of(e)

    def queues(st, active):
        if named(st) is not None:
            return [named(st)]
        item = next((i.get('expr') for i in st.findall('inputs/input') if i.get('name') == 'Item ID' and i.get('expr')), '')
        out = []
        for o in sorted((o for o in came_from(st.findtext('subsheetid'), item, frozenset()) if not isinstance(o, str)), key=lambda o: o.get('stageid')):
            res = o.find('resource')
            if res is not None and o.get('type') == 'Action' and internal_family(res.get('object') or '') == 'Work Queues' and o.get('stageid') not in active:
                out += [n for n in queues(o, active | {st.get('stageid')}) if n not in out]
        return out
    return {st.get('stageid'): queues(st, frozenset()) for st in stages
            if st.get('type') == 'Action' and st.find('resource') is not None and internal_family(st.find('resource').get('object') or '') == 'Work Queues'}


# the attributes and texts of a stage that read a data item: the places an expression stands
RAW_EXPR_ATTRS = ('expr', 'expression', 'detail', 'timeout', 'value', 'reply')
RAW_EXPR_TEXTS = ('value', 'expression', 'detail', 'timeout', 'loopdata')


def raw_unwritten(p: Proc):
    """The stage ids of the data items and collections of a process that an expression reads and nothing writes, read from
    the XML for `check`: written is the stage attribute of an output (an End stage's outputs name what it returns), of
    a Start input, of a calculation and of a step, a step's action outputs, and a collection a Collections or Work
    Queues action names in `Collection Name`; read is a data item name in an expression attribute or text of any other
    stage, or the collection a loop walks. An item exposed outside is filled by its environment."""
    stages = p.inner.findall('stage')
    items = [st for st in stages if st.get('type') in ('Data', 'Collection')]
    own = {(st.findtext('subsheetid'), st.get('name').lower()): st.get('stageid') for st in items}
    shared = {st.get('name').lower(): st.get('stageid') for st in items if st.find('private') is None}

    def find(page, name):
        n = (name or '').strip().lower()
        return next((own.get((page, c)) or shared.get(c) for c in (n, n.split('.', 1)[0].strip()) if own.get((page, c)) or shared.get(c)), None)
    written, read = set(), set()
    for st in stages:
        pg = st.findtext('subsheetid')
        if st.get('type') in ('Data', 'Collection'):
            continue
        written |= {find(pg, e.get('stage')) for e in st.iter() if e.get('stage') and not (e.tag == 'output' and st.get('type') == 'End')}
        written |= {find(pg, o.findtext('value')) for o in st.findall('step/action/outputs/output')}
        res = st.find('resource')
        if st.get('type') == 'Action' and res is not None and internal_family(res.get('object') or '') in ('Collections', 'Work Queues'):
            for i in st.findall('inputs/input'):
                lit = re.fullmatch(r'\s*"((?:[^"]|"")*)"\s*', i.get('expr') or '')
                if i.get('name') == 'Collection Name' and lit:
                    written.add(find(pg, lit.group(1).replace('""', '"')))
        for e in st.iter():
            for k in RAW_EXPR_ATTRS:
                for ref in re.findall(r'\[([^\[\]]+)\]', e.get(k) or ''):
                    read.add(find(pg, ref))
            if e.tag in RAW_EXPR_TEXTS:
                for ref in re.findall(r'\[([^\[\]]+)\]', e.text or ''):
                    read.add(find(pg, ref))
        if st.get('type') == 'LoopStart':
            lp = st.find('loopdata')
            read.add(find(pg, (lp.text or '').strip() or (lp.find('collection').get('name') if lp is not None and lp.find('collection') is not None else '') if lp is not None else ''))
    exposed = {st.get('stageid') for st in items if st.findtext('exposure') in ('Environment', 'Session')}
    return (read - written - exposed) - {None}


def raw_flow(stages):
    """One page's flow read from its stages' XML, for `check` - the structure `page_flow` gives, read afresh: links
    through Anchors, linked Notes and a Choice's or Wait's end, a loop's implicit `empty` (past the loop) and `loop` links, the
    Recovers entered once a stage they cover is reached, the stages each runs until a Resume, and the stages each
    catches: those whose innermost Block holding a reached Recover is that Recover's, or, in no such Block, the page's
    Recover outside every Block."""
    by = {st.get('stageid'): st for st in stages}
    ends = collections.defaultdict(dict)
    for st in stages:
        if st.findtext('groupid'):
            ends[st.findtext('groupid')].setdefault(st.get('type'), st.get('stageid'))
    blocks = [st for st in stages if st.get('type') == 'Block']
    around = {sid: raw_blocks_around(st, blocks) for sid, st in by.items()}
    links, passed = collections.defaultdict(list), collections.defaultdict(list)

    def link(sid, target, kind):
        seen = []
        while target in by and by[target].get('type') in ('Anchor', 'Note', 'ChoiceEnd', 'WaitEnd') and target not in seen:
            seen.append(target)
            target = by[target].findtext('onsuccess')
        passed[sid] += seen
        if target in by and target not in seen:
            links[sid].append((target, kind))
    for st in stages:
        sid, t = st.get('stageid'), st.get('type')
        g = ends.get(st.findtext('groupid') or '', {})
        if t == 'Decision':
            link(sid, st.findtext('ontrue'), 'true')
            link(sid, st.findtext('onfalse'), 'false')
        elif t in ('ChoiceStart', 'WaitStart'):
            for ch in st.findall('choices/choice'):
                link(sid, ch.findtext('ontrue'), f"case:{ch.findtext('name') or ''}")
            end = g.get('ChoiceEnd' if t == 'ChoiceStart' else 'WaitEnd')
            if end:
                passed[sid].append(end)
                link(sid, by[end].findtext('onsuccess'), 'otherwise' if t == 'ChoiceStart' else 'timeout')
        elif t == 'LoopEnd':
            if g.get('LoopStart'):
                links[sid].append((g['LoopStart'], 'loop'))
            link(sid, st.findtext('onsuccess'), 'done')
        elif t not in ('Anchor', 'Note', 'ChoiceEnd', 'WaitEnd', 'End', 'Exception') and t not in NOT_FLOW:
            link(sid, st.findtext('onsuccess'), 'resume' if t == 'Resume' else 'next')
        if t == 'LoopStart' and g.get('LoopEnd'):
            passed[sid].append(g['LoopEnd'])
            link(sid, by[g['LoopEnd']].findtext('onsuccess'), 'empty')
    start = next((st.get('stageid') for st in stages if st.get('type') == 'Start'), None)
    recovers = [st.get('stageid') for st in stages if st.get('type') == 'Recover']
    own = {r: (around[r][0] if around[r] else None) for r in recovers}
    reached, seen = set(), set()

    def walk(sid):
        todo = [sid]
        while todo:
            n = todo.pop()
            if n not in reached:
                reached.add(n)
                seen.update(passed[n])
                todo += [b for b, _ in links[n]]
    if start:
        walk(start)
    while True:
        r = next((r for r in recovers if r not in reached and any(
            (s in reached or s in seen) and by[s].get('type') in FLOW_TYPES and by[s].get('type') not in ('Note', 'Anchor', 'Recover')
            and (own[r] is None or any(b is own[r] for b in around[s])) for s in by)), None)
        if r is None:
            break
        walk(r)
    handlers = {}
    for r in recovers:
        if r in reached:
            run, todo = set(), [r]
            while todo:
                n = todo.pop()
                if n not in run:
                    run.add(n)
                    if by[n].get('type') != 'Resume':
                        todo += [b for b, _ in links[n]]
            handlers[r] = run
    recovery = set().union(*handlers.values()) if handlers else set()
    kinds = {s: shape_kind(by[s].get('type'), (by[s].find('exception').get('usecurrent') if by[s].find('exception') is not None else None)) for s in reached}
    scopes = {r: set() for r in handlers}
    for s in reached:
        if s in recovery or kinds[s] not in SCOPED_KINDS:
            continue
        mine = next((r for b in around[s] for r in handlers if own[r] is b), None) or next((r for r in handlers if own[r] is None), None)
        if mine:
            scopes[mine].add(s)
    edges = [(a, b, k) for a in reached for b, k in links[a]] + [(s, r, 'exception') for r, sc in scopes.items() for s in sc]
    return {'kinds': kinds, 'edges': edges, 'start': start, 'reached': reached | seen, 'scopes': scopes, 'own': own, 'by': by}


def check_shapes(x: Export, full_dumps, misses, total):
    """The shape catalog against each page's XML: every link of every reached stage and every stage a Recover catches,
    and nothing else; each Recover's region with the stages it catches and its Block; the shape and reasons the page's
    own flow classifies to; on a graph page every node with its centre; the unlinked stages; the Markdown catalog's
    line or section per page with every link. And the dump: a stage whose name its page repeats carries its short id
    on its line and on every goto to it."""
    cat, md = shapes(x)
    by_page = {(c['objectId'], c['pageId']): c for c in cat}
    md_lines = md.splitlines()
    sections = {}
    for sec in md.split('\n## ')[1:]:
        sections[sec.split('\n', 1)[0]] = sec

    def miss(key, text):
        misses[('shapes',) + key].append(text)
    for p in x.procs:
        head = f"`{p.name}` ({p.id})"
        pages = {None: p.page_name(None)}
        pages.update({sh.get('subsheetid'): sh.findtext('name') or '' for sh in p.sheets_xml})
        dumped = {blk.split('»', 1)[0]: blk for blk in full_dumps[p.id].split('\n=== PAGE «')[1:]}
        for pid, pname in pages.items():
            on_page = [st for st in p.inner.findall('stage') if st.findtext('subsheetid') == pid]
            where = f"{head} «{pname}»"
            counts = collections.Counter(st.get('name') for st in on_page if st.get('type') in FLOW_TYPES - {'Note', 'Anchor'})
            sid = short_ids([st.get('stageid') for st in on_page])
            for ln in dumped.get(pname, '').splitlines():  # a goto to a repeated name names its stage
                m = re.search(r'→ goto ‹(.*)› \[(\w+)\](.*)$', ln)
                if m and counts[m.group(1)] > 1:
                    total['dump'] += 1
                    ids = {f" #{sid[st.get('stageid')]}" for st in on_page if st.get('name') == m.group(1) and st.get('type') == m.group(2)}
                    if m.group(3) not in ids:
                        misses[('dump', 'goto', 'stage id')].append(f"{where}: {ln.strip()}")
            flow = raw_flow(on_page)
            if flow['start'] is None:
                continue
            total['shapes'] += 1
            entry = by_page.get((p.id, pid))
            if entry is None:
                miss(('page',), where)
                continue
            got = collections.Counter((e['from'], e['to'], e['kind']) for e in entry['edges'])
            want = collections.Counter(flow['edges'])
            total['shapes'] += 1
            if got != want:
                lost, extra = want - got, got - want
                miss(('edge',), f"{where}: {sum(lost.values())} links of the export missing"
                     + (f" (e.g. ‹{flow['by'][next(iter(lost))[0]].get('name')}› {next(iter(lost))[2]})" if lost else '') + f", {sum(extra.values())} not in it")
            for r, scope in flow['scopes'].items():
                if not scope:
                    continue
                total['shapes'] += 1
                rg = next((rg for rg in entry['regions'] if rg['recover'] == r), None)
                if rg is None or set(rg['scope']) != scope:
                    miss(('scope',), f"{where} ‹{flow['by'][r].get('name')}›: catches {len(scope)} stages, the catalog {len(rg['scope']) if rg else 'no region'}")
                    continue
                block = flow['own'][r]
                if (rg['block'] or {}).get('name') != (block.get('name') if block is not None else None):
                    miss(('block',), f"{where} ‹{flow['by'][r].get('name')}›: {written((rg['block'] or {}).get('name'), py=True)}, the XML gives {written(block.get('name') if block is not None else None, py=True)}")
            if {rg['recover'] for rg in entry['regions']} - {r for r, sc in flow['scopes'].items() if sc}:
                miss(('scope',), f"{where}: a region for a Recover that catches no stage")
            shape, reasons, _ = classify_flow(flow['kinds'], flow['edges'], flow['start'], flow['scopes'])
            total['shapes'] += 1
            if (entry['shape'], entry['reasons']) != (shape, reasons):
                miss(('shape',), f"{where}: {entry['shape']} {entry['reasons']}, its own flow gives {shape} {reasons}")
            total['shapes'] += 1
            if {n['id'] for n in entry['nodes']} != set(flow['kinds']) | {st.get('stageid') for st in on_page if st.get('type') == 'Note'}:
                miss(('node',), f"{where}: {len(entry['nodes'])} nodes listed")
            if shape == 'graph':
                for n in entry['nodes']:
                    total['shapes'] += 1
                    g = raw_geo(flow['by'][n['id']]) if n['id'] in flow['by'] else None
                    if g is None or (n['x'], n['y']) != (coordinate(g[0]), coordinate(g[1])):
                        miss(('geometry',), f"{where} ‹{n['name']}›: ({n['x']}, {n['y']}), the XML gives {g[:2] if g else 'no position'}")
            unreached = {st.get('stageid') for st in on_page if st.get('stageid') not in flow['reached'] and st.get('type') in FLOW_TYPES and st.get('type') != 'Anchor'
                         and not (st.get('type') == 'Note' and not st.findtext('onsuccess'))}
            total['shapes'] += 1
            if {u['id'] for u in entry['unlinked']} != unreached:
                miss(('unlinked',), f"{where}: {len(entry['unlinked'])} listed, {len(unreached)} unreached")

            def label(s):
                st = flow['by'][s]
                return f"‹{st.get('name')}› [{st.get('type')}]" + (f" #{sid[s]}" if counts[st.get('name')] > 1 else '')
            total['shapes-md'] += 1
            if shape == 'blocks':
                if f"- {head} «{pname}» — blocks" not in md_lines:
                    misses[('shapes-md', 'page')].append(where)
                continue
            sec = sections.get(f"{head} «{pname}» — graph ({', '.join(reasons)})")
            if sec is None:
                misses[('shapes-md', 'page')].append(where)
                continue
            for a, b, k in flow['edges']:
                if k != 'exception':
                    total['shapes-md'] += 1
                    if f"- {label(a)} → {label(b)} ({k})" not in sec.splitlines():
                        misses[('shapes-md', 'link')].append(f"{where}: {label(a)} → {label(b)} ({k})")
            for n in entry['nodes']:
                total['shapes-md'] += 1
                if not any(ln.startswith(f"- {label(n['id'])} {n['kind']} ({n['x']}, {n['y']})") for ln in sec.splitlines()):
                    misses[('shapes-md', 'node')].append(f"{where}: {label(n['id'])}")


def md_blob(v):
    """A value as a text rendering must print it: the base64 bytes it carries as their size - an image's after its
    `<width>,<height>,<size>,` head, and every run of base64 characters that reads as encoded bytes (`raw_base64`),
    on one line or wrapped at one width of 60 to 120, with its closing `=` padding."""
    m = re.fullmatch(r'(\d+,\d+,\d+,)([A-Za-z0-9+/=\s]{300,})', v)
    if m:
        return f"{m.group(1)}<{len(m.group(2))} base64 characters>"

    def size(m):
        run = m.group()
        if not raw_base64(run, re.split(r'[\s"\'()<>=,]', v[:m.start()])[-1]):
            return run
        return f"<{len(run) - run.count(chr(10)) - run.count(chr(13))} base64 characters>"
    return re.sub(r'(?:[A-Za-z0-9+/]{60,120}\r?\n)+[A-Za-z0-9+/]{1,120}={0,2}(?![A-Za-z0-9+/])|[A-Za-z0-9+/]{300,}={0,2}', size, v)


def raw_base64(run, before):
    """Does a run of base64 characters read as encoded bytes, for `check`: 300 or more, its lines but the last of one
    width and the last no longer, 40 or more distinct, and neither a URL nor a path - a scheme's `://`, a drive or `//`
    where the token holding it starts (`before` + `run`)?"""
    widths = [len(ln) for ln in re.split(r'\r?\n', run)]
    return (sum(widths) >= 300 and len(set(widths[:-1])) <= 1 and widths[-1] <= widths[0] and len(set(run) - set('=\r\n')) >= 40
            and not re.match(r'[A-Za-z][A-Za-z0-9+.-]*://|[A-Za-z]:[\\/]|[\\/]{2}', before + run))


def check_profile(x: Export, prof, need, misses, total, reach):
    """`profile` against the XML: the release identity, each process's and object's row, each work queue, environment
    variable and group with its members, each call graph edge, each tag-carrying queue stage, the web API services, and
    the stage vocabulary lines in their documented order (most used first, a tie by name); every caller, call, queue
    role, tag-carrying stage and unused action with the reach `raw_reach` gives (`reach`)."""
    lines = prof.splitlines()
    page_reach, stage_reach, _ = reach

    def reach_of(p, st):
        return stage_reach.get((p, st.get('stageid')), 'dead')

    def line(pred):
        return '\n'.join(ln for ln in lines if pred(ln))

    def section(title):
        i = next((n for n, ln in enumerate(lines) if ln.startswith(f"## {title}")), None)
        if i is None:
            return []
        out = []
        for ln in lines[i + 1:]:
            if ln.startswith('## ') or not ln.strip():
                break
            out.append(ln)
        return out
    names = {p.guid: p for p in x.procs}
    queue_names = {}
    for f in x.files:
        root = parse_xml(f)
        if root.tag != 'release':
            continue
        rel = line(lambda ln: ln.startswith('  release `'))
        for k in ('name', 'created', 'package-id', 'package-name'):
            need('profile', ('export', k), root.findtext(k), rel, os.path.basename(f))
        need('profile', ('export', 'contents'), root.find('contents').get('count') if root.find('contents') is not None else None, rel, os.path.basename(f))
        items = list(root.find('contents')) if root.find('contents') is not None else []
        queue_names.update({it.get('id'): it.get('name') for it in items if it.tag == 'work-queue'})
        for it in items:
            where = f"{it.tag} `{it.get('name')}`"
            if it.tag == 'work-queue':
                row = '\n'.join(ln for ln in section('WORK QUEUES') if f"`{it.get('name')}` (" in ln)
                for k in ('key-field', 'max-attempts'):
                    need('profile', ('work-queue', k), it.get(k), row, where)
            elif it.tag == 'environment-variable':
                row = '\n'.join(ln for ln in section('ENVIRONMENT VARIABLES') if ln.startswith(f"  {it.get('name')} |"))
                secret = SENSITIVE.search(it.get('name') or '') or it.get('type') == 'password'
                for k in ('type', 'value'):
                    need('profile', ('environment-variable', k), '***' if k == 'value' and secret and it.get(k) else it.get(k), row, where, it.get('name') or '')
                need('profile', ('environment-variable', 'description'), (it.findtext('description') or '').strip(), row, where)
            elif it.tag.endswith('-group'):
                row = '\n'.join(ln for ln in section(it.tag.upper()) if ln.startswith(f"  {it.get('name')} "))
                need('profile', ('group', 'name'), it.get('name'), row, where)
                for m in it.findall('members/*'):
                    member = names.get(m.get('id'))
                    need('profile', ('group', 'member'), member.name if member else queue_names.get(m.get('id'), m.get('id')), row, where)
            elif it.tag == 'webapiservice':
                row = '\n'.join(ln for ln in section('WEB API SERVICES') if ln.startswith(f"  `{it.get('name')}` ("))
                cfg = it.find('configuration')
                need('profile', ('web api', 'base url'), cfg.get('baseurl') if cfg is not None else None, row, where)
            elif it.tag in ('calendar', 'credential'):
                row = '\n'.join(ln for ln in section('CALENDARS' if it.tag == 'calendar' else 'CREDENTIALS') if f"`{it.get('name')}`" in ln)
                for k, v in raw_item_values(it, {g: p.name for g, p in names.items()}):
                    need('profile', (it.tag, k), v, row, where)
    counts = {'process': collections.Counter(), 'object': collections.Counter()}
    for p in x.procs:
        kind = 'object' if raw_is_object(p) else 'process'
        row = line(lambda ln: ln.startswith(f"  `{p.name}` ({p.id}) |"))
        where = f"`{p.name}` ({p.id})"
        sheet_names = {sh.get('subsheetid'): sh.findtext('name') or '' for sh in p.sheets_xml}
        if kind == 'process':
            need('profile', ('process', 'published'), f"published={p.wrapper.get('published')}", row, where)
        else:
            need('profile', ('object', 'runmode'), p.inner.get('runmode'), row, where)
            need('profile', ('object', 'shared'), f"shared={p.inner.get('shared')}" if p.inner.get('shared') else None, row, where)
            need('profile', ('object', 'application type'), p.appdef.findtext('apptypeinfo/id') if p.appdef is not None else None, row, where)
        for st in p.inner.findall('stage'):
            counts[kind][st.get('type')] += 1
            if st.get('type') == 'Process':
                callee = names.get(st.findtext('processid'))
                edge = line(lambda ln: ln.startswith(f"    `{p.name}` ({p.id}) -> "))
                need('profile', ('call graph', 'process'), f"-> `{callee.name}` ({callee.id})" if callee else f"UNRESOLVED {st.findtext('processid')}", edge, where)
                at = f"  at ‹{st.get('name')}› on page «{p.page_name(None) if st.findtext('subsheetid') is None else sheet_names.get(st.findtext('subsheetid'))}»"
                total['profile'] += 1
                if not any(ln.endswith(at + REACH_MARK[reach_of(p, st)]) for ln in edge.splitlines()):
                    misses[('profile', 'call graph', 'process reach')].append(f"{where}{at} = {reach_of(p, st)}")
            elif st.get('type') == 'Action' and st.find('resource') is not None:
                obj = st.find('resource').get('object') or ''
                callee = x.by_name_ci.get(obj.lower())
                if callee is not None and callee is not p and not re.match(r'(?i)blue ?prism\.', obj):
                    edge = line(lambda ln: ln.startswith(f"    `{p.name}` ({p.id}) -> `{obj}` ({callee.id}){REACH_MARK[reach_of(p, st)]}: "))
                    need('profile', ('call graph', 'object'), st.find('resource').get('action'), edge, where)
    tags = section('QUEUE TAGS')
    for p in x.procs:  # each tag-carrying Work Queues stage: its tag inputs and outputs, and the actions its Item ID comes from
        where, stages = f"`{p.name}` ({p.id})", p.inner.findall('stage')
        pages = {None: p.page_name(None)}
        pages.update({sh.get('subsheetid'): sh.findtext('name') or '' for sh in p.sheets_xml})
        came_from = raw_origins(p)
        for st in stages:
            res = st.find('resource')
            if st.get('type') != 'Action' or res is None or internal_family(res.get('object') or '') != 'Work Queues':
                continue
            terms = [i for i in st.findall('inputs/input') if i.get('name') in TAG_INPUTS and i.get('expr')]
            reads = [o for o in st.findall('outputs/output') if 'tag' in (o.get('name') or '').lower()]
            if not (terms or reads or res.get('action') in ('Tag Item', 'Untag Item')):
                continue
            head = f"  {where} «{pages.get(st.findtext('subsheetid'))}» ‹{st.get('name')}› {res.get('action')}{REACH_MARK[reach_of(p, st)]}"
            row = '\n'.join(ln for ln in tags if ln == head or ln.startswith(head + ' |'))  # same-named stages of two pages apart
            need('profile', ('queue tags', 'stage'), f"‹{st.get('name')}› {res.get('action')}", row, where)
            for i in terms:
                need('profile', ('queue tags', 'terms'), i.get('expr'), row, where)
            for o in reads:
                need('profile', ('queue tags', 'outputs'), f"{o.get('name')}→[{o.get('stage') or ''}]", row, where)
            item = next((i.get('expr') for i in st.findall('inputs/input') if i.get('name') == 'Item ID' and i.get('expr')), '')
            if item:  # exactly these sources: a page's data item merged with a same-named one elsewhere adds one
                want = ', '.join(sorted({o if isinstance(o, str) else raw_label(o) for o in came_from(st.findtext('subsheetid'), item, frozenset())})) or '(nothing in the process)'
                total['profile'] += 1
                if not any(ln.endswith(f" from {want}") for ln in row.splitlines()):
                    misses[('profile', 'queue tags', 'item from')].append(f"{head.strip()} = {written(want, py=True)}")
    roles = section('QUEUE ROLES')  # each queue's roles, and the actions a process takes under each, from the XML's own trace of every Item ID
    split = next((n for n, ln in enumerate(roles) if ln == '  by process:'), len(roles))
    queue_roles_text, cur = collections.defaultdict(dict), None
    for ln in roles[:split]:
        if ln.startswith('  `'):
            cur = re.match(r'  `(.*)`(?: \(\d+\))?$', ln).group(1).lower()
        elif cur is not None and ln.startswith('    '):
            role, _, rest = ln.strip().partition(': ')
            queue_roles_text[cur][role] = rest
    per_process, expected = '\n'.join(roles[split:]), 0
    for p in x.procs:
        queues_of = raw_queue_stages(p)
        found = collections.defaultdict(collections.Counter)  # (queue, role, reach) -> action counts
        for st in p.inner.findall('stage'):
            if st.get('stageid') not in queues_of:
                continue
            action = st.find('resource').get('action')
            role = 'adds' if action in ITEM_ADDS else 'takes' if action in ITEM_TAKES else 'changes' if action in ITEM_CHANGES else 'reads'
            for qn in queues_of[st.get('stageid')] or [BY_ITEM_ID]:
                found[(visible(qn).lower(), role, reach_of(p, st))][action] += 1
        lead = f"  `{p.name}` ({p.id}): "  # cut off: a queue can carry the process's own name
        mine = next((ln[len(lead):] for ln in per_process.splitlines() if ln.startswith(lead)), '').lower().split('; ')
        for (qn, role, st_), acts in found.items():
            where = f"`{p.name}` ({p.id}) on `{qn}`"
            mark = f"`{p.name}` ({p.id}){REACH_MARK[st_]} "  # a process's segment for the stages of one reach
            seg = next((s_ for s_ in queue_roles_text.get(qn, {}).get(role, '').split('; ') if s_.startswith(mark) and not s_.startswith('[', len(mark))), '')
            for a_, n_ in acts.items():
                expected += n_
                need('profile', ('queue roles', role), f"{a_}×{n_}", seg, where)
            need('profile', ('queue roles', 'by process'), role + REACH_MARK[st_], next((s_ for s_ in mine if s_.startswith(f"`{qn}`")), ''), where)
    total['profile'] += 1
    shown = sum(int(n_) for ln in roles[:split] for n_ in re.findall(r'×(\d+)', ln))
    if shown != expected:  # an action shown under a queue the XML does not trace it to, or shown twice
        misses[('profile', 'queue roles', 'action count')].append(f"the section counts {shown} action(s), the XML's trace gives {expected}")
    unwritten = section('DATA ITEMS READ AND NEVER WRITTEN')
    for p in x.procs:
        want = raw_unwritten(p)
        at = next((n_ for n_, ln in enumerate(unwritten) if ln == f"  `{p.name}` ({p.id})"), None)
        shown_items = []
        for ln in unwritten[at + 1:] if at is not None else []:
            if not ln.startswith('    '):
                break
            shown_items.append(ln)
        names = {st.get('stageid'): st.get('name') for st in p.inner.findall('stage')}
        total['profile'] += 1
        if len(shown_items) != len(want):
            misses[('profile', 'unwritten items', 'count')].append(f"`{p.name}` ({p.id}) lists {len(shown_items)}, the XML gives {len(want)}")
        for sid in want:
            need('profile', ('unwritten items', 'item'), names[sid], '\n'.join(shown_items), f"`{p.name}` ({p.id})")
    for o in x.procs:  # a running object's actions: how many run, and exactly those that do not, unused and dead apart
        if not raw_is_object(o) or page_reach.get((o, None)) != 'runs':
            continue
        acts = [(sh.findtext('name') or '', page_reach.get((o, sh.get('subsheetid')))) for sh in o.inner.findall('subsheet') if sh.get('type') != 'CleanUp']
        head = f"    `{o.name}` ({o.id}): "
        row = next((ln for ln in lines if ln.startswith(head) and ' used, ' in ln), '')
        listed, _, dead = row[len(head):].partition(' unused')[2].partition(' dead: ')
        for st_, shown in (('unused', listed), ('dead', dead)):
            want = [n for n, s_ in acts if s_ == st_]
            total['profile'] += 1
            if not listed_exactly(shown, want):
                misses[('profile', 'unused actions')].append(f"`{o.name}` ({o.id}) lists {st_} {shown.strip() or '[]'}, the XML gives {written(want, py=True)}")
        total['profile'] += 1
        if not row.startswith(f"    `{o.name}` ({o.id}): {sum(1 for _, s_ in acts if s_ == 'runs')} used, "):
            misses[('profile', 'unused actions')].append(f"`{o.name}` ({o.id}): {row.strip() or 'no row'}, the XML gives {sum(1 for _, s_ in acts if s_ == 'runs')} used")
    for o in x.procs:
        kind = 'object' if raw_is_object(o) else 'process'
        where, stages = f"`{o.name}` ({o.id})", o.inner.findall('stage')
        main = [st for st in stages if st.findtext('subsheetid') is None]
        if kind == 'process':  # the row: the main page's parameters
            row = line(lambda ln: ln.startswith(f"  `{o.name}` ({o.id}) |"))
            for st in main:
                for prm in (st.findall('inputs/input') if st.get('type') == 'Start' else st.findall('outputs/output') if st.get('type') == 'End' else []):
                    need('profile', ('process', 'parameter'), f"'{prm.get('name')}'", row, where)
        else:  # the row's callers, and the application model's launch parameters and element types
            row = line(lambda ln: ln.startswith(f"  `{o.name}` ({o.id}) |"))
            for q in x.procs:
                calls = [st for st in q.inner.findall('stage') if st.get('type') == 'Action' and st.find('resource') is not None
                         and (st.find('resource').get('object') or '').lower() == o.name.lower()]
                if q is not o and calls:
                    need('profile', ('object', 'caller'), f"'{q.name}{REACH_MARK[min((reach_of(q, st) for st in calls), key=REACH_ORDER.index)]}'", row, where)
            decl = raw_declarations(o)
            if len(decl) > 1:
                model = line(lambda ln: ln.startswith(f"  `{o.name}` ({o.id}): "))
                for prm in o.appdef.findall('apptypeinfo/parameters/parameter'):
                    need('profile', ('application model', 'parameter'), prm.findtext('value'), model, where, prm.findtext('name') or '')
                for el in decl:
                    if el.findtext('type') and el.findtext('type') != 'Application':
                        need('profile', ('application model', 'element type'), f"'{el.findtext('type')}'", model, where)
        exposed = [st for st in stages if st.get('type') in ('Data', 'Collection') and st.findtext('exposure')]
        if exposed or any(st.get('type') == 'Data' and st.findtext('datatype') == 'password' for st in stages):
            row = line(lambda ln: ln.startswith(f"  `{o.name}`: exposed="))
            for st in exposed:
                need('profile', ('data items', 'exposed'), f"{st.get('name')}({st.findtext('exposure')})", row, where)
        raising = [st.find('exception') for st in stages if st.get('type') == 'Exception' and st.find('exception') is not None and st.find('exception').get('usecurrent') != 'yes']
        if raising or any(st.get('type') == 'Recover' for st in stages):
            row = line(lambda ln: ln.startswith(f"  `{o.name}`: throws="))
            for ex in raising:
                need('profile', ('exceptions', 'type'), f"'{ex.get('type') or '(none)'}'", row, where)
            page_blocks = collections.defaultdict(list)
            for st in stages:
                if st.get('type') == 'Block':
                    page_blocks[st.findtext('subsheetid')].append(st)
            scoped = sorted(b[0].get('name') for b in (raw_blocks_around(st, page_blocks[st.findtext('subsheetid')]) for st in stages if st.get('type') == 'Recover') if b)
            need('profile', ('exceptions', 'block-scoped'), f"block-scoped={written(scoped, py=True)}", row, where)
        if kind == 'process':  # the collection fields: declared, and read as [Collection.Field] in any expression
            colls = {}  # one set per name, compared case-insensitively, named by its first spelling
            for st in stages:
                if st.get('type') == 'Collection' and st.get('name'):
                    colls.setdefault(st.get('name').lower(), (st.get('name'), set()))[1].update(f.get('name') for f in st.findall('collectioninfo/field'))
            for st in stages:
                texts = [c.get('expression') for c in st.findall('calculation') + st.findall('steps/calculation')]
                texts += [st.find('decision').get('expression')] if st.find('decision') is not None else []
                texts += [i.get('expr') for i in st.findall('inputs/input')] + [st.findtext('timeout')]
                texts += [st.find('exception').get('detail')] if st.find('exception') is not None else []
                for ch in st.findall('choices/choice'):
                    texts += [ch.get('expression'), ch.get('reply')] + [a.findtext('value') for a in ch.findall('condition/arguments/argument')]
                    texts += [e.findtext('expression') for e in ch.findall('element/elementparameter')]
                for stp in st.findall('step'):
                    texts += [stp.get('expr')] + [a.findtext('value') for a in stp.findall('action/arguments/argument')]
                    texts += [e.findtext('expression') for e in stp.findall('element/elementparameter')]
                for v in texts:
                    for c, fld in re.findall(r'\[([^\[\].]+?)\.([^\[\]]+)\]', v or ''):
                        if c.strip().lower() in colls:
                            colls[c.strip().lower()][1].add(fld.strip())
            for name, fields in colls.values():
                row = line(lambda ln: ln.startswith(f"  `{o.name}` [{name}]: "))
                for fld in fields:
                    need('profile', ('collection fields', 'field'), f"'{fld}'", row, where)
    types = set(counts['process']) | set(counts['object'])
    want = [f"  {k}: {counts['process'][k]} / {counts['object'][k]}" for k in sorted(types, key=lambda k: (-(counts['process'][k] + counts['object'][k]), k))]
    total['profile'] += 1
    got = section('STAGE VOCABULARY')
    if got != want:
        at = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        misses[('profile', 'stage vocabulary')].append(f"line {at + 1} reads {written(got[at] if at < len(got) else '', py=True)}, the XML gives {written(want[at] if at < len(want) else '', py=True)}")
    check_profile_reach(x, lines, section, misses, total, reach)


def listed_exactly(text, want):
    """Does `text` - a list as the profile writes it - hold each name of `want` quoted, and no other? A name may hold `, `."""
    return all(f"'{visible(n)}'" in text for n in want) and text.count("'") == sum(2 + visible(n).count("'") for n in want)


def check_profile_reach(x: Export, lines, section, misses, total, reach):
    """The profile's reach against `raw_reach`: each sub-process with its mark, exactly the unused objects, each dead
    process and object with the stages calling it, exactly the pages of each running process that do not run, and
    each credential and calendar read with the reach of the stage reading it."""
    page_reach, stage_reach, callers = reach

    def where_of(p, st):
        pid = st.findtext('subsheetid')
        return f"`{p.name}` ({p.id}) «{p.page_name(None) if pid is None else next((sh.findtext('name') or '' for sh in p.sheets_xml if sh.get('subsheetid') == pid), '')}» ‹{st.get('name')}›"
    graph = section('CALL GRAPH')
    sub = next((ln for ln in graph if ln.startswith('  SUB-PROCESSES ')), '')
    for p in x.procs:
        if not raw_is_object(p) and any(c is not p for c, _ in callers.get((p, None), [])):
            total['profile'] += 1
            if f"'`{p.name}` ({p.id}){REACH_MARK[page_reach[(p, None)]]}'" not in sub:
                misses[('profile', 'call graph', 'sub-process reach')].append(f"`{p.name}` ({p.id}) = {page_reach[(p, None)]}")
    unused = next((ln for ln in graph if ln.startswith('  UNUSED OBJECTS ')), '').partition('): ')[2]
    want = [f"`{o.name}` ({o.id})" for o in x.procs if raw_is_object(o) and page_reach[(o, None)] == 'unused']
    total['profile'] += 1
    if not listed_exactly(unused, want):
        misses[('profile', 'call graph', 'unused objects')].append(f"lists {unused or '[]'}, the XML gives {written(want, py=True)}")
    at = next((n for n, ln in enumerate(graph) if ln.startswith('  DEAD PROCESSES AND OBJECTS ')), len(graph))
    dead_lines = []
    for ln in graph[at + 1:]:
        if not ln.startswith('    '):
            break
        dead_lines.append(ln)
    dead = [p for p in x.procs if page_reach[(p, None)] == 'dead']
    total['profile'] += 1
    if len(dead_lines) != len(dead):
        misses[('profile', 'call graph', 'dead count')].append(f"lists {len(dead_lines)}, the XML gives {len(dead)}")
    for p in dead:
        row = next((ln for ln in dead_lines if ln.startswith(f"    `{p.name}` ({p.id}) {'object' if raw_is_object(p) else 'process'}")), '')
        for c, st in callers[(p, None)]:
            total['profile'] += 1
            if where_of(c, st) not in row:
                misses[('profile', 'call graph', 'dead caller')].append(f"`{p.name}` ({p.id}) from {where_of(c, st)}")
    off = section('CALL GRAPH')
    at = next((n for n, ln in enumerate(off) if ln.startswith('  PAGES THAT DO NOT RUN ')), len(off))
    for p in x.procs:
        if raw_is_object(p) or page_reach[(p, None)] != 'runs':
            continue
        sheets = [(sh.findtext('name') or '', page_reach[(p, sh.get('subsheetid'))]) for sh in p.sheets_xml]
        head = f"    `{p.name}` ({p.id}): unused "
        row = next((ln for ln in off[at + 1:] if ln.startswith(head)), '')
        shown_unused, _, shown_dead = row[len(head):].partition('; dead ')
        for st_, shown in (('unused', shown_unused), ('dead', shown_dead)):
            want = [n for n, s_ in sheets if s_ == st_]
            total['profile'] += 1
            if not listed_exactly(shown, want):
                misses[('profile', 'call graph', 'pages that do not run')].append(f"`{p.name}` ({p.id}) lists {st_} {shown or '[]'}, the XML gives {written(want, py=True)}")
    creds, cals = section('CREDENTIALS'), section('CALENDARS')
    for p in x.procs:  # every name a Credentials or Calendars action reads, under that name, with the reach of the stage reading it
        name_of = raw_names(p.inner.findall('stage'))
        for st in p.inner.findall('stage'):
            res = st.find('resource')
            family = internal_family(res.get('object') or '') if st.get('type') == 'Action' and res is not None else None
            if family not in ('Credentials', 'Calendars'):
                continue
            for i in st.findall('inputs/input'):
                if not i.get('expr') or i.get('name') not in (('Credentials Name', 'Name') if family == 'Credentials' else ('Calendar Name',)):
                    continue
                name, entry = name_of(i.get('expr')), f"'{p.name} ({res.get('action')}){REACH_MARK[stage_reach.get((p, st.get('stageid')), 'dead')]}'"
                rows = [ln for ln in creds if ln.startswith(f"  {name}: [")] if family == 'Credentials' else [ln for ln in cals if ln.startswith(f"  `{name}` (")]
                if family == 'Calendars' and not rows:
                    continue  # a calendar the release does not define has no line
                total['profile'] += 1
                if not any(entry in ln for ln in rows):
                    misses[('profile', family.lower(), 'read')].append(f"`{name}` read by {entry}")


def check(x: Export):
    """Completeness self-test of every rendering the extractor and the executor read, and schema coverage. Every value
    a stage's XML carries - its name, narrative, parameters with their data items and descriptions, bindings, resource,
    expressions, loop, wait and exception settings, UI steps with their elements, arguments, outputs and element
    parameters, code - must appear, as written (never repr- or JSON-escaped: only the JSON outputs escape) or masked
    as a secret, in:
      - the stage's `dump --full` line, and that line must be in the process's dump (reached, recovery or unlinked);
        a Choice or Wait branch's values in its case line; a data item's or collection's in its page-header entry,
        on that header's line; a collection's initial rows in its rows line; the page's name, type and description;
        the process header, with every group the release lists it in and no other;
      - its `cards` item: the header's groups as the dump header's, every page, decision, choice, loop and code stage,
        every throw's type and detail, every caller by id, the stage logging counts;
      - its `targets` entry: every application-model element's identity and every attribute it recorded, and every UI
        step's action, arguments, element parameters, written value and read target on the element it acts on;
      - its `data` entries: every call's bindings and exactly its groups in the process inventory, every release
        environment variable, work queue (exactly its groups), calendar, credential definition (its members by name)
        and web API service in the data sources;
      - the Markdown catalogs: each element's line in `bp-targets.md` with its identity, attribute values and the
        actions applied, each release item's line in `bp-data-sources.md` with its values (a work queue's groups);
      - the page shapes: every link of every reached stage and every stage a Recover catches, each Recover's Block and
        scope, the shape the page's own flow classifies to, a graph page's positions, the unlinked stages, and each
        page's line or section in `bp-page-shapes.md`; a repeated stage name's short id on its dump line and gotos;
      - `profile`: the release identity, each process's row with its parameters and each object's with its callers,
        each work queue, environment variable, calendar, credential definition and group with its members, each call
        graph edge, each tag-carrying queue stage's tag inputs and outputs and the stages writing its Item ID, each running
        object's unused and dead actions, the application models' parameters and element types, the collection fields,
        the exposed data items, the exception types, the web API services, and the stage vocabulary in its order;
      - the reach `raw_reach` gives: on every page head, card page, caller and callee, call graph edge, sub-process,
        object caller, queue role, tag-carrying stage, credential and calendar read, and in the process inventory; and
        exactly the unused objects, the dead processes and objects with their calling stages, and the pages of each
        running process that do not run.
    A Recover's line names the Block the stage's geometry puts it in, a description stays on its header line. No text
    rendering prints base64 bytes whole (a code body aside). The process inventory's calls of each process must be
    exactly its own Action and Process stages. A miss is a
    rendering defect: a reading of that value taken from the rendering is suspect. Every path holding a value must be
    documented in schema.md. Returns the report text and the miss count."""
    misses = collections.defaultdict(list)
    total = collections.Counter()

    def need(surface, key, s, text, where, name=''):
        if s in (None, ''):
            return
        total[surface] += 1
        if not _present(x, str(s), text, name, as_json=surface in ('targets', 'data')):
            misses[(surface,) + key].append(f"{where} = {written(x.safe(str(s), name)[:60], py=True)}")

    def one_line(surface, key, text, where):
        """a rendered line is one line: a line break printed as it is splits it, and the reader takes the rest for
        another line"""
        total[surface] += 1
        if len(text.splitlines()) > 1:
            misses[(surface,) + key].append(where)

    def need_line(surface, key, values, lines, where):
        """one line of the rendering holds every value (a card item names its stage and states its expression)"""
        total[surface] += 1
        if not any(all(_present(x, str(v), ln, n) for v, n in values) for ln in lines):
            misses[(surface,) + key].append(f"{where} = {written(x.safe(str(values[-1][0]))[:60], py=True)}")
    by_guid = {p.guid: p for p in x.procs}
    tcat, targets_md = targets(x)
    cat = {c['objectId']: c for c in tcat}
    md_objects = collections.defaultdict(list)  # object id -> its lines of the Markdown catalog: element ids repeat across objects
    for sec in targets_md.split('\n## ')[1:]:
        m = re.match(r'`[^`]*` \((\d+)\)', sec)
        if m:
            md_objects[int(m.group(1))] = sec.splitlines()[1:]
    pd, sources, data_md = data(x)
    pd = {e['id']: e for e in pd}
    stock = raw_stock_narratives(x.procs)
    card_text = cards(x)
    card_blocks = {}
    for block in re.split(r'^### ', card_text, flags=re.M)[1:]:  # a card's head starts its line; a page name may hold `### `
        m = re.match(r'`[^`]*` \((\d+)\)', block)
        if m:
            card_blocks[int(m.group(1))] = block
    # group memberships from each release's group items: a member names its process, object or work queue by id, and
    # only within that release
    raw_groups = collections.defaultdict(list)  # (file, member id) -> [{path, default}] in the groups' order
    for f in x.files:
        root = parse_xml(f)
        for g in (root.find('contents') if root.tag == 'release' and root.find('contents') is not None else []):
            if g.tag.endswith('-group'):
                for m in g.findall('members/*'):
                    if m.get('id'):
                        raw_groups[(f, m.get('id'))].append({'path': g.get('name'), 'default': (g.get('isDefaultGroup') or '').lower() == 'true'})

    def need_groups(surface, key, want, text, where):
        """`text` (one line) shows each membership, path and flag together, and no other"""
        for g in want:
            need(surface, key + ('group',), f'"path": "{g["path"]}", "default": {"true" if g["default"] else "false"}', text, where)
        total[surface] += 1
        shown = text.count('{"path": ')
        if shown != len(want):
            misses[(surface,) + key + ('group count',)].append(f"{where} shows {shown}, the release's groups list it {len(want)} time(s)")
    reach = raw_reach(x)
    page_reach, stage_reach, _ = reach
    full_dumps = {}
    for p in x.procs:
        head = f"`{p.name}` ({p.id})"
        groups_want = raw_groups.get((p.path, p.wrapper.get('id')), []) if p.wrapper.get('id') else []
        full = full_dumps[p.id] = dump(x, str(p.id), True)
        repeats = collections.Counter((st.findtext('subsheetid'), st.get('name')) for st in p.inner.findall('stage') if st.get('type') in FLOW_TYPES - {'Note', 'Anchor'})
        sids = {}
        for st in p.inner.findall('stage'):
            sids.setdefault(st.findtext('subsheetid'), []).append(st.get('stageid'))
        sids = {k: short_ids(v) for k, v in sids.items()}
        names = raw_element_names(p)
        twins = collections.Counter(names.values())
        eids = short_ids([i for i, n in names.items() if twins[n] > 1 and i])
        pages = {s.get('subsheetid'): s.findtext('name') or '' for s in p.sheets_xml}
        pages[None] = p.page_name(None)
        # dump: the header, every page's head, every stage's line with its values, every item's entry
        hline = full.split('\n', 1)[0]
        item_lines = '\n'.join(ln for ln in full.splitlines() if ln.startswith(('  data items (', '  collections (')))  # an entry split by a line break is a miss
        for k in ('name', 'version', 'bpversion', 'runmode', 'shared'):
            need('dump', ('header', k), p.inner.get(k), hline, head)
        need('dump', ('header', 'published'), p.wrapper.get('published'), hline, head)
        need_groups('dump', ('header',), groups_want, hline, head)
        desc = '\n'.join(ln for ln in full.splitlines() if ln.startswith(('DESCRIPTION: ', 'PRECONDITIONS: ')))  # split by a line break is a miss
        need('dump', ('header', 'narrative'), p.inner.get('narrative'), desc, head)
        for c in p.inner.findall('preconditions/condition'):
            if c.get('narrative'):
                need('dump', ('header', 'precondition'), c.get('narrative'), desc, head)
        blocks_on, stages_on = collections.defaultdict(list), collections.defaultdict(list)
        for st in p.inner.findall('stage'):
            stages_on[st.findtext('subsheetid')].append(st)
            if st.get('type') == 'Block':
                blocks_on[st.findtext('subsheetid')].append(st)
        if p.appdef is not None:
            need('dump', ('header', 'apptypeinfo'), p.appdef.findtext('apptypeinfo/id'), full, head)
            for prm in p.appdef.findall('apptypeinfo/parameters/parameter'):
                if prm.findtext('value'):
                    need('dump', ('header', 'apptypeinfo parameter'), prm.findtext('value'), full, head, prm.findtext('name') or '')
        for sh in p.sheets_xml:
            where = f"{head} «{sh.findtext('name')}»"
            need('dump', ('page', 'name'), f"=== PAGE «{sh.findtext('name')}»", full, where)
            if sh.get('type') not in (None, 'Normal'):
                need('dump', ('page', 'type'), f"[{sh.get('type')}]", full, where)
        for st in p.inner.findall('stage'):
            s = p.stages.get(st.get('stageid'))
            t = st.get('type')
            where = f"{head} «{pages.get(st.findtext('subsheetid'), '?')}» ‹{st.get('name')}›"
            if t in ('Data', 'Collection'):
                text = data_item_text(x, s) if t == 'Data' else collection_text(s)
                need('dump', (t, 'entry'), text, item_lines, where)
                for f, v, n in item_expectations(st, x):
                    need('dump', (t, f), v, text, where, n)
                if t == 'Data' and not st.findtext('initialvalue') and not (st.findtext('initialvalueenc') or '').strip():  # a line break is a value
                    total['dump'] += 1  # an item with no initial value shows none: a masked value would say one is stored
                    if text.startswith(f"{st.get('name') or ''}:{st.findtext('datatype')}="):
                        misses[('dump', t, 'a value where the item has none')].append(where)
                rows_line = '\n'.join(ln for ln in full.splitlines() if ln.startswith(f"  initial rows of [{st.get('name')}]"))
                nrows = len(st.findall('initialvalue/row'))
                if t == 'Collection' and nrows:  # blank rows count too
                    need('dump', (t, 'initial row count'), f"  initial rows of [{st.get('name')}] ({nrows}): ", rows_line, where)
                for fld in st.iter('field'):
                    if fld.get('value'):
                        need('dump', (t, 'row value'), fld.get('value'), rows_line, where, fld.get('name') or '')
                    if fld.get('encvalue'):
                        need('dump', (t, 'row value'), f'"{fld.get("name")}": "***"', rows_line, where)
                continue
            if t == 'SubSheetInfo':
                if (st.findtext('narrative') or '').strip():
                    need('dump', (t, 'narrative'), st.findtext('narrative').strip(), full, where)
                continue
            if t == 'ProcessInfo':
                need('dump', (t, 'language'), st.findtext('language'), full, where)
                for k in ('code', 'globalcode'):
                    for ln in (st.findtext(k) or '').splitlines():
                        need('dump', (t, k), ln.strip(), full, where)
                for k in ('references/reference', 'imports/import'):
                    for r in st.findall(k):
                        need('dump', (t, k), r.text or '', full, where)
                continue
            if t in ('Anchor', 'Block') or s is None:
                continue
            line = x.render(s, p, True)
            if line is None:
                continue
            need('dump', (t, 'line in the dump'), line, full, where)
            if t != 'Code':  # `--full` prints a code body under its stage's line
                one_line('dump', (t, 'line split'), line, where)
            if repeats[(st.findtext('subsheetid'), st.get('name'))] > 1:  # a repeated name with the stage's own id
                need('dump', (t, 'stage id'), f" #{sids[st.findtext('subsheetid')][st.get('stageid')]}", line, where)
            for el in st.findall('step/element'):  # a repeated element name with the element's own id
                if el.get('id') in eids:
                    need('dump', (t, 'element id'), f"«{names[el.get('id')]} #{eids[el.get('id')]}»", line, where)
            for f, v, n in stage_expectations(st, names, by_guid, pages, stock):
                need('dump', (t, f), v, line, where, n)
            if t == 'Recover':
                need('dump', (t, 'scope'), raw_scope(st, blocks_on[st.findtext('subsheetid')], stages_on[st.findtext('subsheetid')]), line, where)
            for i, ch in enumerate(st.findall('choices/choice')):
                cl = case_line(x, p, s, s.choices[i])
                need('dump', (t, 'case line in the dump'), cl, full, where)
                one_line('dump', (t, 'case line split'), cl, where)
                for f, v, n in case_expectations(st, ch, names):
                    need('dump', (t, f), v, cl, where, n)
                el = ch.find('element')
                if el is not None and el.get('id') in eids:
                    need('dump', (t, 'element id'), f"«{names[el.get('id')]} #{eids[el.get('id')]}»", cl, where)
        page_text = {blk.split('»', 1)[0]: blk for blk in full.split('\n=== PAGE «')[1:]}
        for pid, pname in pages.items():
            on_page = [st for st in p.inner.findall('stage') if st.findtext('subsheetid') == pid]
            reached = raw_reached(on_page)
            want = collections.Counter()
            for st in on_page:
                t = st.get('type')
                if t in FLOW_TYPES and t != 'Anchor' and not (t == 'Note' and not st.findtext('onsuccess')) and st.get('stageid') not in reached:
                    line = x.render(p.stages[st.get('stageid')], p, True)
                    want[f"    [{t}] " + (line.splitlines()[0] if line else f"‹{st.get('name')}›")] += 1
            blk = page_text.get(pname, '')
            got = collections.Counter(ln for ln in blk.split('  unlinked stages (', 1)[1].splitlines()[1:] if ln.startswith('    [')) if '  unlinked stages (' in blk else collections.Counter()
            total['dump'] += 1
            if want != got:
                misses[('dump', 'unlinked stages')].append(f"{head} «{pname}»: {sum((want - got).values())} unreached not listed, {sum((got - want).values())} listed but reached")
            st_ = page_reach.get((p, pid), 'dead')
            total['dump'] += 1
            if not any(ln.endswith(REACH_MARK[st_]) and (st_ != 'runs' or not ln.endswith((' [unused]', ' [dead]')))
                       for ln in full.splitlines() if ln.startswith(f"=== PAGE «{pname}»")):
                misses[('dump', 'page', 'reach')].append(f"{head} «{pname}» = {st_}")
        # cards
        block = card_blocks.get(p.id, '')
        lines = block.splitlines()
        need('cards', ('header',), f"`{p.name}` ({p.id})", block, head)
        need_groups('cards', ('header',), groups_want, lines[0] if lines else '', head)
        need('cards', ('header', 'shared'), f"shared={p.inner.get('shared')}" if p.inner.get('shared') else None, lines[0] if lines else '', head)
        if p.inner.get('narrative'):
            need('cards', ('description',), p.inner.get('narrative'), block, head)
        for sh in p.sheets_xml:
            need('cards', ('page',), f"«{sh.findtext('name')}»", block, head)
        for pid, pname in pages.items():
            ptype = next((sh.get('type') for sh in p.sheets_xml if sh.get('subsheetid') == pid), None)
            st_ = page_reach.get((p, pid), 'dead')
            total['cards'] += 1
            if not any(ln.startswith(f"    «{pname}»" + (f" [{ptype}]" if ptype not in (None, 'Normal') else '') + REACH_MARK[st_] + ' in=[') for ln in lines):
                misses[('cards', 'page', 'reach')].append(f"{head} «{pname}» = {st_}")
        callees = collections.defaultdict(list)  # the calls line: each distinct callee with the reach of the stages calling it
        for st in p.inner.findall('stage'):
            res, target = st.find('resource'), st.findtext('processid')
            if st.get('type') == 'Action' and res is not None:
                key = f"{raw_action_label(res.get('object') or '')}.{res.get('action')}"
            elif st.get('type') == 'SubSheet':
                key = f"page:{pages.get(target, f'<page {target}>')}"
            elif st.get('type') == 'Process':
                key = f"process:`{by_guid[target].name}` ({by_guid[target].id})" if target in by_guid else f"process:UNRESOLVED {target}"
            else:
                continue
            callees[key].append(stage_reach.get((p, st.get('stageid')), 'dead'))
        body = '; ' + next((ln for ln in lines if ln.startswith('  calls (in order, distinct): ')), '').partition(': ')[2] + '; '
        for key, sts in callees.items():
            st_ = min(sts, key=REACH_ORDER.index)
            total['cards'] += 1
            if f"; {key}{REACH_MARK[st_]}; " not in body:
                misses[('cards', 'calls', 'reach')].append(f"{head} {key} = {st_}")
        for st in p.inner.findall('stage'):
            t, nm = st.get('type'), st.get('name') or ''
            where = f"{head} ‹{nm}›"
            if t == 'Decision':
                need_line('cards', ('decision',), [(f"‹{nm}›", ''), ((st.find('decision').get('expression') if st.find('decision') is not None else '') or '', '')], lines, where)
            elif t == 'ChoiceStart':
                need_line('cards', ('choice',), [(f"‹{nm}›", '')] + [(v, '') for ch in st.findall('choices/choice') for v in (ch.findtext('name'), ch.get('expression')) if v],
                          lines, where)
            elif t == 'LoopStart':
                need_line('cards', ('loop',), [(f"‹{nm}›", ''), (raw_loopdata(st) or '', '')], lines, where)
            elif t == 'Code':
                need_line('cards', ('code stage',), [(f"‹{nm}›", '')], lines, where)
            elif t == 'Exception' and st.find('exception') is not None and st.find('exception').get('usecurrent') != 'yes':
                ex = st.find('exception')
                need_line('cards', ('throw',), [(ex.get('type') or '(none)', '')] + ([(ex.get('detail'), '')] if ex.get('detail') else []), lines, where)
            elif t in ('Data', 'Collection') and st.findtext('exposure'):
                need('cards', ('environment variable',), nm, block, where)
        for q in x.procs:  # every caller by id: an Action naming the object, a Process stage naming the process; with the reach of its calls
            sts = [stage_reach.get((q, st.get('stageid')), 'dead') for st in q.inner.findall('stage')
                   if (st.get('type') == 'Action' and st.find('resource') is not None and (st.find('resource').get('object') or '').lower() == p.name.lower())
                   or (st.get('type') == 'Process' and st.findtext('processid') == p.guid)]
            if sts and q is not p:
                st_ = min(sts, key=REACH_ORDER.index)
                callers_line = next((ln for ln in lines if ln.startswith('  callers:')), '')
                need('cards', ('callers',), f"({q.id})", callers_line, f"{head} called by `{q.name}`")
                total['cards'] += 1
                if not marked(callers_line, f"`{q.name}` ({q.id})", st_):
                    misses[('cards', 'callers', 'reach')].append(f"{head} called by `{q.name}` ({q.id}) = {st_}")
        log = collections.Counter()
        for st in p.inner.findall('stage'):
            if st.get('type') in FLOW_TYPES and st.get('type') not in ('Anchor', 'Note'):
                li = st.find('loginhibit')
                log['all' if li is None else ('errors' if li.get('onsuccess') == 'true' else 'none')] += 1
        need('cards', ('logging',), f"logging: all={log['all']} errors-only={log['errors']} none={log['none']}", block, head)
        # targets: every element's identity and recorded attributes, and every UI step on the element it acts on
        entry = cat.get(p.id)
        steps_on = [stp for st in p.inner.findall('stage') for stp in st.findall('step') + st.findall('choices/choice') if stp.find('element') is not None]
        if p.appdef is not None or steps_on:
            entry = entry or {'elements': [], 'undeclared': []}
            by_id = {e['id']: _flat(e) for e in entry['elements']}
            by_id.update({u['id']: _flat(u) for u in entry.get('undeclared', [])})

            md_lines = md_objects.get(p.id, [])

            def md_line(eid):
                return next((ln for ln in md_lines if f"({eid})" in ln), '')
            for el in raw_declarations(p):
                if not el.findtext('type'):
                    continue
                eid = el.findtext('id')
                text, where = by_id.get(eid, ''), f"{head} element ‹{el.get('name')}›"
                mdl = md_line(eid) if el.findtext('type') != 'Application' else None
                for k in ('id', 'type', 'basetype', 'datatype', 'description', 'narrative'):
                    if (el.findtext(k) or '').strip():
                        need('targets', ('element', k), el.findtext(k).strip(), text, where)
                need('targets', ('element', 'name'), el.get('name') or '', text, where)
                if mdl is not None:
                    need('targets-md', ('element', 'name and id'), f"{el.get('name') or ''} ({eid})", mdl, where)
                    need('targets-md', ('element', 'type'), f"<{el.findtext('type')}>", mdl, where)
                for a in el.findall('attributes/attribute'):
                    pv = a.find('ProcessValue')
                    if pv is not None and pv.get('value') not in (None, ''):
                        need('targets', ('element', 'attribute name'), a.get('name') or '', text, where)
                        need('targets', ('element', 'attribute value'), pv.get('value'), text, where, a.get('name') or '')
                        if mdl is not None:
                            need('targets-md', ('element', 'attribute'), f"{a.get('name')}", mdl, where)
                            need('targets-md', ('element', 'attribute value'), md_blob(pv.get('value')), mdl, where, a.get('name') or '')
                    if a.get('comparisontype'):
                        need('targets', ('element', 'attribute comparisontype'), a.get('comparisontype'), text, where)
            declared = set(names)
            for st in p.inner.findall('stage'):
                where = f"{head} ‹{st.get('name')}›"
                for stp in st.findall('step') + st.findall('choices/choice'):
                    el = stp.find('element')
                    if el is None or not el.get('id'):
                        continue
                    text = by_id.get(el.get('id'), '')
                    need('targets', (st.get('type'), 'stage'), st.get('name') or '', text, where)
                    act = stp.findtext('action/id') or stp.findtext('condition/id')
                    mdl = md_line(el.get('id')) if el.get('id') in declared else next((ln for ln in md_lines if f"undeclared element ({el.get('id')})" in ln), '')
                    if act and (el.get('id') not in declared or names.get(el.get('id')) is not None):
                        need('targets-md', (st.get('type'), 'action'), act, mdl, where)
                    for v, n in ([(stp.findtext('action/id'), ''), (stp.findtext('condition/id'), ''), (stp.get('expr'), names.get(el.get('id'), '')), (stp.get('stage'), ''),
                                  (st.get('interval'), ''), (stp.get('reply'), ''), (stp.findtext('comparetype'), '')]
                                 + [(a.findtext('value'), a.findtext('id') or '') for a in stp.findall('action/arguments/argument') + stp.findall('condition/arguments/argument')]
                                 + [(ep.findtext('expression'), '') for ep in el.findall('elementparameter')]):
                        if v not in (None, ''):
                            need('targets', (st.get('type'), 'step value'), v, text, where, n)
        # data: the process inventory's calls are the process's own Action and Process stages, in document order
        own = [st for st in p.inner.findall('stage') if (st.get('type') == 'Action' and st.find('resource') is not None)
               or (st.get('type') == 'Process' and st.findtext('processid'))]
        calls = (pd.get(p.id) or {}).get('calls', [])
        total['data'] += 1
        if len(calls) != len(own):
            misses[('data', 'process inventory', 'calls')].append(f"{head} lists {len(calls)}, its stages hold {len(own)}")
        total['data'] += 1
        want_reach = [page_reach.get((p, None), 'dead')] * 2 + [page_reach.get((p, sh.get('subsheetid')), 'dead') for sh in p.sheets_xml]  # the item's, then each page's
        got_reach = [(pd.get(p.id) or {}).get('reach')] + [pg.get('reach') for pg in (pd.get(p.id) or {}).get('pages', [])]
        if got_reach != want_reach:
            misses[('data', 'process inventory', 'reach')].append(f"{head} lists {written(got_reach, py=True)}, the XML gives {written(want_reach, py=True)}")
        total['data'] += 1
        if [c.get('reach') for c in calls] != [stage_reach.get((p, st.get('stageid')), 'dead') for st in own]:
            misses[('data', 'process inventory', 'call reach')].append(head)
        total['data'] += 1
        got = (pd.get(p.id) or {}).get('groups')
        if got != groups_want:
            misses[('data', 'process inventory', 'groups')].append(f"{head} lists {written(got, py=True)}, the release's groups give {written(groups_want, py=True)}")
        for st, c in zip(own, calls):
            text, where = _flat(c), f"{head} ‹{st.get('name')}›"
            for prm in st.findall('inputs/input'):
                if prm.get('expr'):
                    need('data', ('process inventory', 'input'), prm.get('name') or '', text, where)
                    need('data', ('process inventory', 'input'), prm.get('expr'), text, where, prm.get('name') or '')
            for prm in st.findall('outputs/output'):
                if prm.get('stage'):
                    need('data', ('process inventory', 'output'), prm.get('stage'), text, where)
    # data: release items
    by_kind = collections.defaultdict(dict)
    for s in sources:
        by_kind[s['kind']][s['name']] = _flat(s)
    md_kind = collections.defaultdict(list)  # data sources Markdown: kind -> its lines
    for sec in data_md.split('\n## ')[1:]:
        kind_head, _, body = sec.partition('\n')
        md_kind[kind_head.strip()] = body.splitlines()

    def md_source(kind, name):
        return next((ln for ln in md_kind[kind] if f"`{name}` —" in ln), '')
    for f in x.files:
        root = parse_xml(f)
        for it in (root.find('contents') if root.tag == 'release' and root.find('contents') is not None else []):
            kind, where = it.tag, f"{it.tag} `{it.get('name')}`"
            if kind == 'work-queue':
                text = next((v for k, v in by_kind['work-queue'].items() if k.lower() == (it.get('name') or '').lower()), '')
                mdl = next((ln for ln in md_kind['work-queue'] if f"({it.get('id')})" in ln), '')
                for k in ('name', 'key-field', 'max-attempts'):
                    if it.get(k):
                        need('data', (kind, k), it.get(k), text, where)
                        need('data-md', (kind, k), it.get(k), mdl, where)
                want = raw_groups.get((f, it.get('id')), []) if it.get('id') else []
                got = next((s.get('groups') for s in sources if s['kind'] == kind and s['name'].lower() == (it.get('name') or '').lower()), None)
                total['data'] += 1
                if got != want:
                    misses[('data', kind, 'groups')].append(f"{where} lists {written(got, py=True)}, the release's groups give {written(want, py=True)}")
                need_groups('data-md', (kind,), want, mdl, where)
            elif kind == 'environment-variable':
                text, mdl = by_kind['environment-variable'].get(it.get('name'), ''), md_source('environment-variable', it.get('name'))
                for k in ('name', 'type', 'value'):
                    if it.get(k):
                        v = '***' if k == 'value' and (SENSITIVE.search(it.get('name') or '') or it.get('type') == 'password') else it.get(k)
                        need('data', (kind, k), v, text, where, it.get('name') or '')
                        need('data-md', (kind, k), v, mdl, where, it.get('name') or '')
                if (it.findtext('description') or '').strip():
                    need('data', (kind, 'description'), it.findtext('description').strip(), text, where)
                    need('data-md', (kind, 'description'), it.findtext('description').strip(), mdl, where)
            elif kind == 'webapiservice':  # every attribute and text of the definition, a secret-named literal as masked
                text, mdl = by_kind['web-api'].get(it.get('name'), ''), md_source('web-api', it.get('name'))
                parent = {c: pe for pe in it.iter() for c in pe}
                for e in it.iter():
                    named = parent.get(e) if e.tag in ('initialvalue', 'description') else e
                    secret = e.tag in ('initialvalue', 'httpheader') and named is not None and bool(SENSITIVE.search(named.get('name') or ''))
                    for k, v in e.attrib.items():
                        need('data', (kind, f"{e.tag}@{k}"), '***' if secret and k == 'value' and '[' not in v else v, text, where)
                        need('data-md', (kind, f"{e.tag}@{k}"), '***' if secret and k == 'value' and '[' not in v else v, mdl, where)
                    if (e.text or '').strip():
                        need('data', (kind, e.tag), '***' if secret else e.text.strip(), text, where)
                        need('data-md', (kind, e.tag), '***' if secret else e.text.strip(), mdl, where)
            elif kind in ('calendar', 'credential'):  # every value, a member by name
                text, mdl = by_kind[kind].get(it.get('name'), ''), md_source(kind, it.get('name'))
                for k, v in raw_item_values(it, {p.guid: p.name for p in x.procs}):
                    need('data', (kind, k), v, text, where)
                    need('data-md', (kind, k), v, mdl, where)
    prof = profile(x)
    check_profile(x, prof, need, misses, total, reach)
    texts = [('profile', prof), ('cards', card_text), ('targets-md', targets_md), ('data-md', data_md)] + [('dump', d) for d in full_dumps.values()]
    for surface, text in texts:
        total[surface] += 1
        unwrapped = (ln.replace('\\u000D\\u000A', '\n').replace('\\u000A', '\n') for ln in text.splitlines())  # a value's line breaks as written
        long_runs = [ln for ln in unwrapped if not ln.lstrip().startswith('| ') and md_blob(ln) != ln]  # a code body prints whole
        if long_runs:
            misses[(surface, 'base64 printed whole')].append(f"{len(long_runs)} line(s), e.g. {long_runs[0][:60]}")
    check_shapes(x, full_dumps, misses, total)
    # schema coverage: every path holding a value is documented; a path documented as ignored while it holds a default
    # carries nothing else
    rows = schema_rows()
    if rows is None:
        misses[('schema', 'schema.md')].append(f"{SCHEMA_FILE} not found")
    else:
        for f in x.files:
            root = parse_xml(f)
            for path, v in schema_paths(root, 'RELEASE' if root.tag == 'release' else 'PROC'):
                total['schema'] += 1
                status, allowed = schema_status(rows, path)
                shown = '***' if 'initialvalue' in path or path.endswith('@encvalue') or SENSITIVE.search(path) else x.safe(str(v))[:60]
                if status is None:
                    misses[('schema', 'undocumented path', path)].append(f"{os.path.basename(f)} = {written(shown, py=True)}")
                elif allowed is not None and str(v).lower() not in allowed:
                    misses[('schema', 'value outside its documented default', path)].append(f"{os.path.basename(f)} = {written(shown, py=True)}")
    n_miss = sum(len(v) for v in misses.values())
    L = [f"# Renderer completeness check — {len(x.procs)} processes and objects, "
         + ', '.join(f"{s} {total[s]}" for s in ('dump', 'cards', 'targets', 'targets-md', 'data', 'data-md', 'shapes', 'shapes-md', 'profile', 'schema'))
         + f" values checked, {n_miss} not rendered", '']
    for key, items in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        L.append(f"- {' '.join(f'`{k}`' if i else k for i, k in enumerate(key))}: {len(items)} — e.g. {'; '.join(items[:3])}")
    return '\n'.join(L), n_miss


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['profile', 'cards', 'dump', 'raw', 'targets', 'data', 'check'])
    ap.add_argument('export')
    ap.add_argument('name', nargs='?', help='dump, raw: process or object id, or a name that occurs once, optionally ::<page name>')
    ap.add_argument('--out', default='.')
    ap.add_argument('--full', action='store_true', help='dump: print code stage bodies')
    ap.add_argument('--stage', help='raw: the stages of this name or stage id (a data item is a stage)')
    ap.add_argument('--find', help='raw: the stages whose XML holds this text')
    ap.add_argument('--element', help='raw: the application-model elements of this name or id')
    a = ap.parse_args()
    if a.command == 'raw' and (not a.name or not (a.stage or a.find or a.element)):
        ap.error('raw requires NAME and at least one of --stage NAME|STAGEID, --find TEXT, --element NAME|ID')
    x = Export(a.export)
    if a.command == 'raw':
        text, code = raw(x, a.name, a.stage, a.find, a.element)
        print(text)
        return code
    if a.command == 'dump':
        if not a.name:
            ap.error('dump requires the process or object id, or a name that occurs once')
        text = dump(x, a.name, a.full)
        print(text)
        return 2 if text.startswith(('no process or object', 'name ')) else 0
    if a.command == 'check':
        report, missed = check(x)
        print(report)
        return 1 if missed else 0
    os.makedirs(a.out, exist_ok=True)

    def W(name, content):
        path = os.path.join(a.out, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content)
        print(f"written {path}")
    if a.command == 'targets':
        cat, md = targets(x)
        W('bp-targets.json', json.dumps(cat, indent=1, ensure_ascii=False))
        W('bp-targets.md', md)
        return 0
    if a.command == 'data':
        pd, rs, md = data(x)
        W('bp-process-data.json', json.dumps(pd, indent=1, ensure_ascii=False))
        W('bp-data-sources.json', json.dumps(rs, indent=1, ensure_ascii=False))
        W('bp-data-sources.md', md)
        cat, md = shapes(x)
        W('bp-page-shapes.json', json.dumps(cat, indent=1, ensure_ascii=False))
        W('bp-page-shapes.md', md)
        return 0
    W(f'bp-{a.command}.txt', profile(x) if a.command == 'profile' else cards(x))
    return 0


if __name__ == '__main__':
    sys.exit(main())
