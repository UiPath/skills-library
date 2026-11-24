#!/usr/bin/env node
// tenant-scan.mjs — read-only, redacting security scan of a UiPath Orchestrator tenant.
//
// Usage:
//   node tenant-scan.mjs [--folder <PATH_PREFIX>] [--max-queue-items <n>] [--max-jobs <n>]
//                        [--log-lines <n>] [--max-assets <n>] [--profile <name>] [--json] [--out <file>]
//
// What it does:
//   1. Calls ONLY read-only `uip or ...` commands from a fixed allowlist (list / get / logs).
//   2. Writes the fetched tenant content to a private temp directory, one file per container,
//      runs the sibling scan.mjs detectors over it, then deletes the directory.
//   3. Adds tenant checks: secret-named Text assets, admin roles on robot / external-app
//      identities, insecure webhooks, sensitive file names in storage buckets.
//   4. Prints findings that NEVER contain a value: rule, severity, resource (kind, folder, name),
//      field, detector, masked shape hint, salted fingerprint.
//
// Never: reads Credential or Secret asset values, downloads bucket files, requests pre-signed
// URLs, or runs any create / update / delete / start / stop verb.
//
// Exit codes: 0 scan finished (findings are data), 2 bad usage, 5 not logged in / CLI missing.

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, writeFileSync, rmSync, chmodSync } from 'node:fs';
import { join, dirname, delimiter } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const KNOWN_FLAGS = new Set(['--folder', '--max-queue-items', '--max-jobs', '--log-lines', '--max-assets', '--profile', '--json', '--out', '--concurrency']);
const VALUE_FLAGS = new Set(['--folder', '--max-queue-items', '--max-jobs', '--log-lines', '--max-assets', '--profile', '--out', '--concurrency']);
for (let i = 0; i < args.length; i++) {
  if (!KNOWN_FLAGS.has(args[i])) { process.stderr.write(`unknown argument: ${args[i]}\n`); process.exit(2); }
  if (VALUE_FLAGS.has(args[i])) i++;
}
const opt = (f, d) => (args.includes(f) ? args[args.indexOf(f) + 1] : d);
const num = (f, d, max) => { const n = parseInt(opt(f, d), 10); return Number.isFinite(n) && n >= 0 ? Math.min(n, max) : d; };
const folderFilter = opt('--folder', '');
const MAX_QUEUE_ITEMS = num('--max-queue-items', 500, 5000);
const MAX_JOBS = num('--max-jobs', 25, 500);
const LOG_LINES = num('--log-lines', 200, 2000);
const MAX_ASSETS = num('--max-assets', 300, 5000);
const CONCURRENCY = num('--concurrency', 8, 16) || 1;
const profile = opt('--profile', '');
const asJson = args.includes('--json');
const outFile = opt('--out', '');
if (profile && !/^[A-Za-z0-9_.-]{1,64}$/.test(profile)) { process.stderr.write('invalid --profile\n'); process.exit(2); }

// ───────────────────────────── CLI invocation ─────────────────────────────
// Read-only allowlist. Anything else is refused before spawning.
const ALLOWED = new Set([
  'login status', 'or folders list', 'or assets list', 'or assets get-folders', 'or assets get-asset-value',
  'or queue-items list', 'or jobs list', 'or jobs get', 'or jobs logs', 'or processes list',
  'or triggers list', 'or webhooks list', 'or users list', 'or buckets list', 'or bucket-files list',
]);

// Resolve the CLI's JS entry so arguments are passed without a shell (folder names with spaces,
// quotes or `&` are never re-parsed). SECURITY_REVIEW_UIP_ENTRY overrides it (tests).
function findEntry() {
  if (process.env.SECURITY_REVIEW_UIP_ENTRY) return process.env.SECURITY_REVIEW_UIP_ENTRY;
  for (const d of (process.env.PATH || '').split(delimiter)) {
    if (!d) continue;
    const e = join(d, 'node_modules', '@uipath', 'cli', 'dist', 'index.js');
    if (existsSync(e)) return e;
    const e2 = join(d, '..', 'lib', 'node_modules', '@uipath', 'cli', 'dist', 'index.js');
    if (existsSync(e2)) return e2;
  }
  return null;
}
const ENTRY = findEntry();
const SAFE_ARG = /^[A-Za-z0-9_.:\-]{1,200}$/;

let commandsRun = 0;
const errors = [];
function uip(verbArgs, extra = []) {
  const verbLen = verbArgs[0] === 'login' ? 2 : 3;
  const verb = verbArgs.slice(0, verbLen).join(' ');
  if (!ALLOWED.has(verb)) throw new Error(`refused non-allowlisted command: ${verb}`);
  // Positional arguments (keys) come from tenant data: identifier charset only.
  if (!verbArgs.slice(verbLen).every(a => SAFE_ARG.test(a))) return Promise.resolve({ Result: 'Refused', Message: 'unsafe key' });
  const all = [...verbArgs, ...extra, '--output', 'json', ...(profile ? ['--profile', profile] : [])];
  return new Promise(res => {
    let child;
    if (ENTRY) child = spawn(process.execPath, [ENTRY, ...all], { stdio: ['ignore', 'pipe', 'pipe'] });
    else {
      // Fallback without the JS entry: only identifier-safe arguments, so a shell cannot re-parse them.
      if (!all.every(a => SAFE_ARG.test(a))) { res({ Result: 'Refused', Message: 'unsafe argument for shell fallback' }); return; }
      child = spawn(process.platform === 'win32' ? 'uip.cmd' : 'uip', all, { stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' });
    }
    commandsRun++;
    const chunks = [];
    let size = 0;
    child.stdout.on('data', d => { size += d.length; if (size <= 256 * 1024 * 1024) chunks.push(d); });
    child.stderr.on('data', () => {});
    const timer = setTimeout(() => child.kill(), 180000);
    child.on('error', () => { clearTimeout(timer); res({ Result: 'SpawnError' }); });
    child.on('close', () => {
      clearTimeout(timer);
      let j;
      try { j = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { j = { Result: 'NonJson' }; }
      if (j.Result !== 'Success') errors.push({ command: verb, result: String(j.Result).slice(0, 40), message: clean(j.Instructions || j.Message || '', 160) });
      res(j);
    });
  });
}
async function pool(items, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); }
  }));
  return out;
}

// ───────────────────────────── helpers ─────────────────────────────
// Tenant-supplied names are untrusted: reduce to a display-safe charset so a queue, folder or
// asset name cannot inject instructions or terminal escapes into the agent's context.
function clean(s, max = 64) {
  const t = String(s ?? '').replace(/[^A-Za-z0-9 _.\-\/()#@]/g, '_');
  return t.length > max ? t.slice(0, max) + '…' : t;
}
const data = j => (j && j.Result === 'Success' ? j.Data : null);
const list = j => { const d = data(j); return Array.isArray(d) ? d : []; };
const parseMaybe = v => { if (typeof v !== 'string') return v; const t = v.trim(); if (!/^[[{]/.test(t)) return v; try { return JSON.parse(t); } catch { return v; } };
function flatten(v, path, out, depth = 0) {
  v = parseMaybe(v);
  if (depth > 8 || v === null || v === undefined) return;
  if (Array.isArray(v)) { v.slice(0, 200).forEach((x, i) => flatten(x, `${path}[${i}]`, out, depth + 1)); return; }
  if (typeof v === 'object') { for (const [k, x] of Object.entries(v)) flatten(x, path ? `${path}.${k}` : k, out, depth + 1); return; }
  if (typeof v === 'string' && v.length) out.push([path, v]);
  else if (typeof v === 'number' && String(v).length >= 9) out.push([path, String(v)]);
}
const inFolder = p => !folderFilter || String(p || '').toLowerCase().startsWith(folderFilter.toLowerCase());

// Temp content store: one file per container. lineMap[file][line] = { resource, field }
const work = mkdtempSync(join(tmpdir(), 'secrev-tenant-'));
try { chmodSync(work, 0o700); } catch {}
const files = new Map();
function record(container, resource, entries) {
  if (!entries.length) return;
  let f = files.get(container);
  if (!f) { f = { name: `c${files.size}.txt`, lines: [], map: [], bytes: 0 }; files.set(container, f); }
  for (const [field, value, keyOverride] of entries) {
    if (f.bytes > 1.5 * 1024 * 1024) break;
    const keyName = keyOverride || field.split(/[.[\]]/).filter(Boolean).pop() || 'value';
    const line = `"${keyName.replace(/"/g, '')}": ${JSON.stringify(value.length > 20000 ? value.slice(0, 20000) : value)}`;
    f.lines.push(line);
    f.map.push({ resource, field });
    f.bytes += line.length + 1;
  }
}

const findings = [];
function add(rule, severity, resource, field, hint, extra = {}) {
  findings.push({ rule, severity, resource, field: field ? clean(field, 96) : undefined, hint, ...extra });
}

// ───────────────────────────── scan ─────────────────────────────
// Paged list up to a cap; a hit cap is reported in stats.truncated, never silent.
async function listAll(verbArgs, extra, cap, label) {
  const out = [];
  for (let offset = 0; offset < cap; offset += 500) {
    const page = list(await uip(verbArgs, [...extra, '--limit', String(Math.min(500, cap - offset)), '--offset', String(offset)]));
    out.push(...page);
    if (page.length < Math.min(500, cap - offset)) return out;
  }
  stats.truncated.push(`${label}: first ${cap}`);
  return out;
}

const stats = { truncated: [], folders: 0, assets: 0, textAssetValuesRead: 0, assetsSkippedNoFolder: 0, queueItemsSampled: 0, jobsSampled: 0, logLinesSampled: 0, processes: 0, triggers: 0, webhooks: 0, users: 0, buckets: 0, bucketFilesListed: 0, commandsRun: 0, commandsFailed: 0 };
const notCovered = [
  'Credential and Secret asset values (never read; stored encrypted by design)',
  'Storage bucket file contents (file names only)',
  'Folder-level role assignments, personal access tokens, external-app scopes, audit logs → uipath-admin',
  'Agent / LLM traces, Action Center task data, Data Fabric records, Integration Service connections',
];

async function main() {
  const login = await uip(['login', 'status']);
  const ld = data(login);
  if (!ld || !/^logged in$/i.test(String(ld.Status || '').trim())) {
    process.stderr.write(ENTRY || login.Result !== 'SpawnError' ? 'Not logged in. Run `uip login` and select the tenant to scan.\n' : 'uip CLI not found. Install @uipath/cli.\n');
    cleanup();
    process.exit(5);
  }
  let host = '';
  try { host = new URL(String(ld.BaseUrl)).host; } catch {}
  const target = { organization: clean(ld.Organization), tenant: clean(ld.Tenant), host: clean(host), folderFilter: folderFilter ? clean(folderFilter, 128) : null };

  const folders = (await listAll(['or', 'folders', 'list'], [], 5000, 'folders')).filter(f => inFolder(f.Path));
  stats.folders = folders.length;
  const folderKeyByPath = new Map(folders.map(f => [String(f.Path).toLowerCase(), f.Key]));
  const res = (kind, folder, name, key) => ({ kind, folder: clean(folder, 128), name: clean(name), ...(key ? { key: clean(key, 40) } : {}) });

  // Assets: list types everywhere; read values of Text assets only.
  const assets = list(await uip(['or', 'assets', 'list'], ['--all-folders', '--limit', String(MAX_ASSETS)]));
  stats.assets = assets.length;
  const SECRET_NAME = /(pass(word|wd|phrase)?|pwd|secret|token|api[_\- ]?key|apikey|client[_\- ]?secret|private[_\- ]?key|connection[_\- ]?string|conn[_\- ]?str|credential|auth[_\- ]?key|access[_\- ]?key|sas)/i;
  const textAssets = assets.filter(a => String(a.ValueType).toLowerCase() === 'text');
  await pool(textAssets, async a => {
    const folderRes = data(await uip(['or', 'assets', 'get-folders', String(a.Key)]));
    const fq = (folderRes?.AccessibleFolders || []).map(f => f.FullyQualifiedName).find(p => inFolder(p) && folderKeyByPath.has(String(p).toLowerCase()));
    if (!fq) { stats.assetsSkippedNoFolder++; return; }
    if (String(a.ValueType).toLowerCase() !== 'text') return; // defense in depth: never read Credential / Secret values
    const v = data(await uip(['or', 'assets', 'get-asset-value', String(a.Key)], ['--folder-key', folderKeyByPath.get(String(fq).toLowerCase())]));
    if (!v || String(v.ValueType).toLowerCase() !== 'text') return;
    stats.textAssetValuesRead++;
    const r = res('asset', fq, a.Name, a.Key);
    // Global value plus per-robot values, each recorded under the asset's own name so name-based detectors apply.
    const values = [['StringValue', String(v.StringValue ?? v.Value ?? '')]];
    for (const kv of v.KeyValueList || []) if (kv && kv.Value) values.push(['KeyValueList (per-robot value)', String(kv.Value)]);
    record(`asset:${a.Key}`, r, values.filter(([, x]) => x).map(([field, x]) => [field, x, String(a.Name || 'value')]));
    const real = values.filter(([, x]) => x.trim().length >= 6 && !/^(x+|\*+|<[^>]*>|changeme|placeholder|todo|tbd|none|null|n\/a)$/i.test(x.trim()));
    if (SECRET_NAME.test(String(a.Name)) && real.length)
      add('ORCH_SECRET_AS_TEXT_ASSET', 'critical', r, real[0][0], `Text asset named like a credential holds ${real.length} non-placeholder value(s); anyone with asset view rights reads them in clear`);
  });

  // Queue items: newest sample across accessible folders.
  const items = [];
  for (let offset = 0; offset < MAX_QUEUE_ITEMS; offset += 100) {
    const page = list(await uip(['or', 'queue-items', 'list'], ['--all-folders', '--all-fields', '--limit', String(Math.min(100, MAX_QUEUE_ITEMS - offset)), '--offset', String(offset), '--sort-by', 'Id desc']));
    items.push(...page);
    if (page.length < 100) break;
  }
  for (const q of items) {
    const folder = q.OrganizationUnitFullyQualifiedName;
    if (!inFolder(folder)) continue;
    stats.queueItemsSampled++;
    const queue = q.QueueDefinition?.Name || q.QueueDefinitionName || 'queue';
    const r = res('queue-item', folder, queue, String(q.Id ?? ''));
    r.encryptedQueue = Boolean(q.QueueDefinition?.Encrypted ?? q.Encrypted);
    const entries = [];
    flatten(q.SpecificContent, 'SpecificContent', entries);
    flatten(q.Output, 'Output', entries);
    flatten(q.Analytics, 'Analytics', entries);
    flatten(q.ProcessingException, 'ProcessingException', entries);
    if (q.Reference) entries.push(['Reference', String(q.Reference)]);
    if (q.Progress) entries.push(['Progress', String(q.Progress)]);
    const sd = parseMaybe(q.SpecificData);
    if (sd && typeof sd === 'object' && !q.SpecificContent) flatten(sd, 'SpecificData', entries);
    record(`queue:${folder}/${queue}`, r, entries);
  }

  // Jobs: newest sample, full arguments + robot logs.
  const jobs = list(await uip(['or', 'jobs', 'list'], ['--all-folders', '--limit', String(MAX_JOBS * 2), '--sort-by', 'Id desc'])).filter(j => inFolder(j.OrganizationUnitFullyQualifiedName ?? j.FolderPath ?? '')).slice(0, MAX_JOBS);
  await pool(jobs, async j => {
    const full = data(await uip(['or', 'jobs', 'get', String(j.Key)], ['--all-fields'])) || {};
    const folder = full.OrganizationUnitFullyQualifiedName || j.OrganizationUnitFullyQualifiedName || '';
    stats.jobsSampled++;
    const r = res('job', folder, full.ReleaseName || j.ReleaseName || j.ProcessName, j.Key);
    const entries = [];
    flatten(full.InputArguments, 'InputArguments', entries);
    flatten(full.OutputArguments, 'OutputArguments', entries);
    if (full.Info) entries.push(['Info', String(full.Info)]);
    flatten(full.EnvironmentVariables, 'EnvironmentVariables', entries);
    record(`job:${j.Key}`, r, entries);
    if (LOG_LINES) {
      const logs = list(await uip(['or', 'jobs', 'logs', String(j.Key)], ['--limit', String(LOG_LINES)]));
      stats.logLinesSampled += logs.length;
      record(`log:${r.name}`, { ...r, kind: 'job-log' }, logs.filter(l => l.Message).map(l => ['Message', String(l.Message)]));
    }
  });

  // Processes: default input arguments and environment variables.
  const procs = await listAll(['or', 'processes', 'list'], ['--all-folders', '--all-fields'], 5000, 'processes');
  const processFolders = new Set(procs.map(p => String(p.OrganizationUnitFullyQualifiedName || '').toLowerCase()));
  for (const p of procs) {
    const folder = p.OrganizationUnitFullyQualifiedName;
    if (!inFolder(folder)) continue;
    stats.processes++;
    const entries = [];
    flatten(p.Arguments?.Input, 'Arguments.Input', entries);
    flatten(p.EnvironmentVariables, 'EnvironmentVariables', entries);
    record(`process:${p.Key}`, res('process', folder, p.Name, p.Key), entries);
  }

  // Triggers: every type, only in folders that hold a process (a trigger always targets one).
  const triggerFolders = stats.truncated.some(t => t.startsWith('processes')) ? folders : folders.filter(f => processFolders.has(String(f.Path).toLowerCase()));
  await pool(triggerFolders.flatMap(f => ['time', 'queue', 'api'].map(t => [f, t])), async ([f, t]) => {
    const trig = list(await uip(['or', 'triggers', 'list'], ['--type', t, '--folder-key', String(f.Key), '--all-fields', '--limit', '500']));
    for (const tr of trig) {
      stats.triggers++;
      const entries = [];
      flatten(tr.InputArguments, 'InputArguments', entries);
      record(`trigger:${tr.Key ?? tr.Id}`, res(`trigger-${t}`, f.Path, tr.Name, tr.Key), entries);
    }
  });

  // Webhooks: TLS, scheme, signing secret presence, URL content. The Secret value is never recorded.
  const hooks = list(await uip(['or', 'webhooks', 'list'], ['--all-fields', '--limit', '500']));
  for (const h of hooks) {
    stats.webhooks++;
    const r = res('webhook', '(tenant)', h.Name, h.Key);
    if (h.AllowInsecureSsl) add('ORCH_WEBHOOK_INSECURE_TLS', 'warning', r, 'AllowInsecureSsl', 'Webhook accepts invalid TLS certificates; job and queue events can be intercepted');
    if (/^http:\/\//i.test(String(h.Url || ''))) add('ORCH_WEBHOOK_PLAIN_HTTP', 'warning', r, 'Url', 'Webhook delivers events over plain HTTP');
    if (!h.Secret) add('ORCH_WEBHOOK_NO_SECRET', 'info', r, 'Secret', 'No signing secret; the receiver cannot verify events come from Orchestrator');
    if (h.Url) record(`webhook:${h.Key}`, r, [['Url', String(h.Url)]]);
  }

  // Identities: robot accounts and external apps holding tenant administrator roles.
  const users = await listAll(['or', 'users', 'list'], ['--all-fields'], 10000, 'users');
  stats.users = users.length;
  for (const u of users) {
    if (!/Robot|ExternalApplication/i.test(String(u.Type))) continue;
    const admin = (u.RolesList || []).filter(x => /administrator/i.test(String(x)) && !/^allow to be/i.test(String(x)));
    if (admin.length) add('ORCH_AUTOMATION_IDENTITY_ADMIN', 'warning', res(String(u.Type).replace(/^Directory/, '').toLowerCase(), '(tenant)', u.UserName || u.Name, u.Key), 'RolesList', `Automation identity holds tenant role(s) ${admin.map(x => `"${clean(x, 40)}"`).join(', ')}; grant folder-scoped least privilege`);
  }

  // Buckets: file names only.
  const SENSITIVE_FILE = /(^|\/)(\.env(\..*)?|id_(rsa|ed25519|ecdsa)|[^/]*\.(pem|pfx|p12|key|jks|keystore|kdbx|har)|[^/]*(password|passwd|credential|secret|token|ssn|social.?security|medical.?record|health.?record|patient.?(record|data|list|export|file|info)|payroll|salary|passport|bank.?statement|card.?holder)[^/]*|[^/]*(patients?|customers?|employees?|members?)[^/]*\.(csv|tsv|xlsx?|json|xml|txt))$/i;
  const SAMPLE_FILE = /(example|sample|synthetic|dummy|mock|fake|test)/i;
  await pool(folders, async f => {
    const buckets = list(await uip(['or', 'buckets', 'list'], ['--folder-key', String(f.Key), '--limit', '200']));
    for (const b of buckets) {
      stats.buckets++;
      const page = data(await uip(['or', 'bucket-files', 'list', String(b.Key)], ['--folder-key', String(f.Key), '--take-hint', '1000']));
      const fileList = page?.Items || [];
      stats.bucketFilesListed += fileList.length;
      const hits = fileList.filter(x => SENSITIVE_FILE.test(String(x.FullPath || '')));
      const real = hits.filter(x => !SAMPLE_FILE.test(String(x.FullPath)));
      if (hits.length) add('ORCH_BUCKET_SENSITIVE_FILE', real.length ? 'warning' : 'info', res('bucket', f.Path, b.Name, b.Key), 'FullPath', `${hits.length} file name(s) suggest credentials or personal data${real.length ? '' : ', all named as samples'} (e.g. "${clean(String((real[0] || hits[0]).FullPath).split('/').pop(), 40)}"); contents not read`);
    }
  });

  // Run the local detectors over everything collected.
  for (const f of files.values()) writeFileSync(join(work, f.name), f.lines.join('\n') + '\n', { mode: 0o600 });
  const byName = new Map([...files.values()].map(f => [f.name, f]));
  if (files.size) {
    const r = spawnSync(process.execPath, [join(HERE, 'scan.mjs'), work, '--json'], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 });
    let scan;
    try { scan = JSON.parse(r.stdout); } catch { errors.push({ command: 'scan.mjs', result: 'Failure', message: 'detector run failed' }); }
    for (const sf of scan?.findings || []) {
      const f = byName.get(String(sf.file).split(/[\\/]/).pop());
      if (!f) continue;
      const m = f.map[(sf.line || 1) - 1] || f.map[0];
      const cat = /^(PII|PHI)_/.test(sf.rule) ? 'PII' : 'SECRET';
      const kind = { asset: 'ASSET', 'queue-item': 'QUEUE_ITEM', job: 'JOB_ARGUMENT', 'job-log': 'JOB_LOG', process: 'PROCESS_DEFAULT', webhook: 'WEBHOOK_URL' }[m.resource.kind] || 'TRIGGER_ARGUMENT';
      let severity = sf.severity;
      let hint = sf.hint;
      if (m.resource.kind === 'queue-item') {
        if (m.resource.encryptedQueue && severity === 'critical' && cat === 'PII') { severity = 'warning'; hint += ' (queue encryption on)'; }
        else if (!m.resource.encryptedQueue && cat === 'PII') hint += ' (queue encryption off)';
      }
      add(`ORCH_${kind}_${cat}`, severity, m.resource, m.field, hint, { detector: sf.rule, fingerprint: sf.fingerprint, ...(sf.knownTestValue ? { knownTestValue: true } : {}) });
    }
  }
  cleanup();

  // Group repeats (same rule + resource + field + detector) into one finding with a count.
  const grouped = new Map();
  for (const f of findings) {
    const k = [f.rule, f.resource.kind, f.resource.folder, f.resource.name, f.field?.replace(/\[\d+\]/g, '[]'), f.detector].join('|');
    const g = grouped.get(k);
    if (g) { g.occurrences++; if (f.fingerprint && g.fingerprints.length < 5 && !g.fingerprints.includes(f.fingerprint)) g.fingerprints.push(f.fingerprint); }
    else grouped.set(k, { ...f, occurrences: 1, fingerprints: f.fingerprint ? [f.fingerprint] : [] });
  }
  const order = { critical: 0, warning: 1, info: 2 };
  const out = [...grouped.values()].map(({ fingerprint, ...g }) => g).sort((a, b) => order[a.severity] - order[b.severity] || a.rule.localeCompare(b.rule));
  for (const g of out) if (g.resource) delete g.resource.encryptedQueue;
  stats.commandsRun = commandsRun;
  stats.commandsFailed = errors.length;
  const counts = out.reduce((c, f) => (c[f.severity]++, c), { critical: 0, warning: 0, info: 0 });
  const result = { mode: 'tenant', target, stats, counts, findings: out, notCovered, errors: errors.slice(0, 50) };

  let output;
  if (asJson) output = JSON.stringify(result, null, 2);
  else {
    const lines = [
      `Tenant ${target.tenant} (org ${target.organization}, ${target.host})${target.folderFilter ? `, folders under ${target.folderFilter}` : ''} — untrusted tenant names sanitized`,
      `Folders ${stats.folders}, assets ${stats.assets} (Text values read ${stats.textAssetValuesRead}), queue items ${stats.queueItemsSampled}, jobs ${stats.jobsSampled} (log lines ${stats.logLinesSampled}), processes ${stats.processes}, triggers ${stats.triggers}, webhooks ${stats.webhooks}, users ${stats.users}, buckets ${stats.buckets}`,
      `Critical: ${counts.critical}  Warning: ${counts.warning}  Info: ${counts.info}  (commands ${stats.commandsRun}, failed ${stats.commandsFailed})`, ''];
    for (const f of out) lines.push(`[${f.severity.toUpperCase()}] ${f.rule}  ${f.resource.kind} ${f.resource.folder}/${f.resource.name}${f.field ? ` · ${f.field}` : ''}  — ${f.hint}${f.occurrences > 1 ? ` ×${f.occurrences}` : ''}${f.fingerprints.length ? `  #${f.fingerprints.join(' #')}` : ''}`);
    output = lines.join('\n');
  }
  if (outFile) writeFileSync(outFile, output + '\n');
  process.stdout.write(output + '\n');
}

function cleanup() { try { rmSync(work, { recursive: true, force: true }); } catch {} }
process.on('exit', cleanup);
main().catch(err => { cleanup(); process.stderr.write(`tenant scan failed: ${clean(err.message, 200)}\n`); process.exit(1); });
