#!/usr/bin/env node
// automation-index: a read-only, deterministic inventory of UiPath automations.
//
// Reads deployed packages and Orchestrator metadata, parses every package with an
// XML parser, and writes the facts to SQLite with provenance (file, line, activity,
// extractor) on every row. It never publishes, packs, runs, starts, deletes or
// changes anything in Orchestrator or in the source it reads.
//
// Node.js 22.5+ built-ins only (the same Node the uip CLI requires). No npm install.
//
//   node automation-index.mjs [--index-dir DIR] <command> [options]
//
// Commands
//   crawl     Read Orchestrator metadata and download the deployed packages.
//   extract   Parse packages (downloaded, supplied, or source folders) into the index.
//   analyze   Run `uip rpa analyze` on throwaway copies of the indexed sources.
//   review    Compute findings and write a review report.
//   cards     Write one Markdown card per automation, plus INDEX.md.
//   query     Run a read-only SQL query against the index.
//   status    Show coverage: what was read, what could not be read, and why.
//   summary   Store or list AI-written summaries (always labelled non-authoritative).

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import zlib from "node:zlib";
import { spawnSync } from "node:child_process";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

const TOOL_VERSION = "1.0.0";
// Bump when extraction logic changes; packages indexed by an older extractor are re-read.
const EXTRACTOR_VERSION = 2;
const PAGE = 50;
const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_FILE_BYTES = 50 * 1024 * 1024; // any single file read from a package or project
const MAX_PACKAGE_BYTES = 1024 * 1024 * 1024; // all files of one package, inflated
const MAX_ZIP_ENTRIES = 200 * 1000;
const MAX_SHEET_COLUMNS = 256; // Config sheets are narrow; cells beyond this are ignored
const MAX_SHEET_CELLS = 500 * 1000;

// ------------------------------------------------------------------ runtime check

const [NODE_MAJOR, NODE_MINOR] = process.versions.node.split(".").map(Number);
if (NODE_MAJOR < 22 || (NODE_MAJOR === 22 && NODE_MINOR < 5)) {
  process.stderr.write(`automation-index needs Node.js 22.5 or later (found ${process.versions.node}). ` +
    "The uip CLI needs Node 22 as well; update Node and retry.\n");
  process.exit(1);
}
{
  const original = process.emitWarning;
  process.emitWarning = function (warning, ...rest) {
    const text = typeof warning === "string" ? warning : warning && warning.message;
    if (text && /SQLite is an experimental feature/i.test(text)) return;
    return original.call(process, warning, ...rest);
  };
}
let DatabaseSync;
try {
  ({ DatabaseSync } = await import("node:sqlite"));
} catch (e) {
  // Node 22.5-22.12 ships node:sqlite behind a flag: relaunch once with it.
  if (!process.execArgv.includes("--experimental-sqlite")) {
    const r = spawnSync(process.execPath, ["--experimental-sqlite", "--no-warnings", ...process.execArgv,
      process.argv[1], ...process.argv.slice(2)], { stdio: "inherit" });
    process.exit(r.status ?? 1);
  }
  process.stderr.write(`node:sqlite is not available in Node ${process.versions.node}: ${e.message}\n`);
  process.exit(1);
}

// ------------------------------------------------------------------------- schema

const SCHEMA = `
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS folder (
  key TEXT PRIMARY KEY, path TEXT, type TEXT, feed_type TEXT, source TEXT);

CREATE TABLE IF NOT EXISTS process (
  key TEXT PRIMARY KEY, name TEXT, folder_key TEXT, folder_path TEXT,
  package_id TEXT, version TEXT, process_type TEXT, target_framework TEXT,
  feed_id TEXT, is_latest INTEGER, auto_update INTEGER, description TEXT,
  tags TEXT, last_modified TEXT, created TEXT);

CREATE TABLE IF NOT EXISTS orch_asset (
  key TEXT, folder_key TEXT, folder_path TEXT, name TEXT, value_type TEXT,
  value_scope TEXT, has_default_value INTEGER, description TEXT,
  PRIMARY KEY (key, folder_key));

CREATE TABLE IF NOT EXISTS orch_queue (
  key TEXT, folder_key TEXT, folder_path TEXT, name TEXT, detail TEXT,
  PRIMARY KEY (key, folder_key));

CREATE TABLE IF NOT EXISTS orch_bucket (
  key TEXT, folder_key TEXT, folder_path TEXT, name TEXT, detail TEXT,
  PRIMARY KEY (key, folder_key));

CREATE TABLE IF NOT EXISTS orch_trigger (
  key TEXT, trigger_type TEXT, folder_key TEXT, folder_path TEXT, name TEXT,
  process_name TEXT, process_key TEXT, enabled INTEGER, detail TEXT,
  PRIMARY KEY (key, trigger_type));

CREATE TABLE IF NOT EXISTS orch_machine (
  key TEXT PRIMARY KEY, name TEXT, type TEXT, scope TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS job_summary (
  process_key TEXT, release_name TEXT, folder_path TEXT, runs INTEGER,
  runs_30d INTEGER, faulted INTEGER, stopped INTEGER, successful INTEGER,
  last_run TEXT, last_success TEXT, window_days INTEGER,
  PRIMARY KEY (process_key, release_name, folder_path));

CREATE TABLE IF NOT EXISTS job_identity (
  process_key TEXT, release_name TEXT, folder_path TEXT, local_account TEXT,
  host_machine TEXT, orchestrator_identity TEXT, runs INTEGER, last_seen TEXT,
  sample_job_key TEXT,
  PRIMARY KEY (process_key, release_name, folder_path, local_account, host_machine));

CREATE TABLE IF NOT EXISTS package (
  package_id TEXT, version TEXT, kind TEXT, origin TEXT, file_path TEXT,
  sha256 TEXT, layout TEXT, has_sources INTEGER, project_name TEXT,
  description TEXT, output_type TEXT, target_framework TEXT,
  expression_language TEXT, studio_version TEXT, main TEXT,
  extractor_version INTEGER, indexed_at TEXT, solution TEXT,
  PRIMARY KEY (package_id, version));

CREATE TABLE IF NOT EXISTS coverage (
  package_id TEXT, version TEXT, stage TEXT, status TEXT, detail TEXT, at TEXT,
  PRIMARY KEY (package_id, version, stage));

CREATE TABLE IF NOT EXISTS dependency (
  package_id TEXT, version TEXT, dep_name TEXT, dep_spec TEXT, dep_version TEXT,
  is_activity_pack INTEGER);

CREATE TABLE IF NOT EXISTS entry_point (
  package_id TEXT, version TEXT, path TEXT, source TEXT);

CREATE TABLE IF NOT EXISTS workflow (
  package_id TEXT, version TEXT, path TEXT, kind TEXT, activities INTEGER,
  reachable INTEGER, parse_error TEXT, sha256 TEXT);

CREATE TABLE IF NOT EXISTS invocation (
  package_id TEXT, version TEXT, from_path TEXT, line INTEGER, activity TEXT,
  kind TEXT, target_raw TEXT, target_path TEXT, static INTEGER, extractor TEXT);

CREATE TABLE IF NOT EXISTS edge (
  package_id TEXT, version TEXT, kind TEXT, value TEXT, normalized TEXT,
  detail TEXT, static INTEGER, file TEXT, line INTEGER, activity TEXT,
  activity_type TEXT, extractor TEXT, confidence TEXT);

CREATE TABLE IF NOT EXISTS config_key (
  package_id TEXT, version TEXT, file TEXT, sheet TEXT, name TEXT, value TEXT,
  folder TEXT, redacted INTEGER);

CREATE TABLE IF NOT EXISTS binding (
  package_id TEXT, version TEXT, resource TEXT, name TEXT, folder_path TEXT,
  key TEXT, workflow TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS analyzer_run (
  package_id TEXT, version TEXT, status TEXT, detail TEXT, errors INTEGER,
  warnings INTEGER, infos INTEGER, policy TEXT, ran_at TEXT,
  PRIMARY KEY (package_id, version));

CREATE TABLE IF NOT EXISTS analyzer_finding (
  package_id TEXT, version TEXT, level TEXT, rule TEXT, error_code TEXT,
  file TEXT, activity_id TEXT, activity TEXT, description TEXT,
  recommendation TEXT);

CREATE TABLE IF NOT EXISTS finding (
  rule TEXT, severity TEXT, package_id TEXT, version TEXT, process_key TEXT,
  subject TEXT, detail TEXT, evidence TEXT, computed_at TEXT);

CREATE TABLE IF NOT EXISTS ai_summary (
  package_id TEXT, version TEXT, model TEXT, text TEXT, source_sha256 TEXT,
  written_at TEXT, PRIMARY KEY (package_id, version));

CREATE INDEX IF NOT EXISTS ix_edge_pkg ON edge (package_id, version, kind, normalized);
CREATE INDEX IF NOT EXISTS ix_finding_pkg ON finding (package_id, version);
CREATE INDEX IF NOT EXISTS ix_entry_pkg ON entry_point (package_id, version);
CREATE INDEX IF NOT EXISTS ix_edge_kind ON edge (kind, normalized);
CREATE INDEX IF NOT EXISTS ix_dep_pkg ON dependency (package_id, version);
CREATE INDEX IF NOT EXISTS ix_inv_pkg ON invocation (package_id, version);
CREATE INDEX IF NOT EXISTS ix_wf_pkg ON workflow (package_id, version);

-- Transitive library closure, materialized (rebuilt by refreshClosure) so per-package lookups stay fast.
CREATE TABLE IF NOT EXISTS dep_closure (
  root_id TEXT, root_version TEXT, package_id TEXT, version TEXT, via TEXT);
CREATE INDEX IF NOT EXISTS ix_closure_root ON dep_closure (root_id, root_version);
CREATE INDEX IF NOT EXISTS ix_closure_pkg ON dep_closure (package_id, version);

DROP VIEW IF EXISTS v_dep_closure;
CREATE VIEW v_dep_closure AS SELECT * FROM dep_closure;

DROP VIEW IF EXISTS v_edge_all;
CREATE VIEW v_edge_all AS
SELECT c.root_id AS root_package_id, c.root_version, c.via,
       e.package_id, e.version, e.kind, e.value, e.normalized, e.detail, e.static,
       e.file, e.line, e.activity, e.activity_type, e.extractor, e.confidence
FROM dep_closure c CROSS JOIN edge e ON e.package_id = c.package_id AND e.version = c.version;

DROP VIEW IF EXISTS v_process_edge;
CREATE VIEW v_process_edge AS
SELECT p.key AS process_key, p.name AS process_name, p.folder_path, a.*
FROM process p JOIN v_edge_all a ON a.root_package_id = p.package_id AND a.root_version = p.version;

DROP VIEW IF EXISTS v_automation;
CREATE VIEW v_automation AS
SELECT p.key AS process_key, p.name AS process_name, p.folder_path, p.process_type,
       p.package_id, p.version, k.layout, k.has_sources, k.target_framework,
       k.studio_version, k.expression_language,
       COALESCE(j.runs, 0) AS runs, COALESCE(j.runs_30d, 0) AS runs_30d,
       COALESCE(j.faulted, 0) AS faulted, j.last_run, j.last_success, j.window_days,
       (SELECT group_concat(status, ',') FROM coverage cv
          WHERE cv.package_id = p.package_id AND cv.version = p.version) AS coverage
FROM process p
LEFT JOIN package k ON k.package_id = p.package_id AND k.version = p.version
LEFT JOIN (SELECT process_key, sum(runs) runs, sum(runs_30d) runs_30d, sum(faulted) faulted,
                  max(last_run) last_run, max(last_success) last_success, max(window_days) window_days
           FROM job_summary GROUP BY process_key) j ON j.process_key = p.key;
`;

// ------------------------------------------------------------------------ helpers

class Exit extends Error {}

function now() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function log(msg) {
  process.stderr.write(msg + "\n");
}

function sha256(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

/** Python-like str() for values that may be null. */
function S(x) {
  return x === null || x === undefined ? "None" : String(x);
}

/** Slice by code points, like Python. */
function cut(s, n) {
  if (s.length <= n) return s;
  const cps = Array.from(s);
  return cps.length <= n ? s : cps.slice(0, n).join("");
}

/** json.dumps with Python's default separators and ASCII escaping. */
function pyDumps(value, sortKeys = false) {
  const enc = (s) => JSON.stringify(s).replace(/[\u007f-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  const walk = (v) => {
    if (v === null || v === undefined) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return Number.isFinite(v) ? JSON.stringify(v) : (Number.isNaN(v) ? "NaN" : (v > 0 ? "Infinity" : "-Infinity"));
    if (typeof v === "string") return enc(v);
    if (Array.isArray(v)) return "[" + v.map(walk).join(", ") + "]";
    let keys = Object.keys(v);
    if (sortKeys) keys = keys.sort();
    return "{" + keys.map((k) => enc(k) + ": " + walk(v[k])).join(", ") + "}";
  };
  return walk(value);
}

function isObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function decodeUtf8Sig(buf) {
  let b = buf;
  if (b.length >= 3 && b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) b = b.subarray(3);
  return new TextDecoder("utf-8", { fatal: true }).decode(b);
}

function jsonLoads(buf) {
  return JSON.parse(decodeUtf8Sig(buf));
}

/** File-system-safe name. When characters had to be replaced (or the name is long), a short hash of
 * the original keeps names that sanitize alike ("a/b" and "a_b") from colliding. */
function safeName(s) {
  const out = s.replace(/[^A-Za-z0-9._@-]+/g, "_");
  if (out === s && out.length <= 150) return out;
  return out.slice(0, 140) + "_" + crypto.createHash("sha1").update(s, "utf8").digest("hex").slice(0, 9);
}

/** Folder name for a package's source tree: readable prefix plus a hash of the exact identity. */
function srcDirName(pkg, ver) {
  const id = `${pkg}@${ver}`;
  return id.replace(/[^A-Za-z0-9._@-]+/g, "_").slice(0, 120) + "_" + crypto.createHash("sha1").update(id, "utf8").digest("hex").slice(0, 10);
}

/** A sub-folder of the index that is a real folder (not a link), so writes and deletes stay inside. */
function indexSubdir(indexDir, name) {
  const d = path.join(indexDir, name);
  let st;
  try {
    st = fs.lstatSync(d);
  } catch {
    fs.mkdirSync(d, { recursive: true });
    return d;
  }
  if (st.isSymbolicLink() || !st.isDirectory()) throw new Exit(`${d} is a symbolic link or not a folder; refusing to write through it. Remove it and retry.`);
  return d;
}

function hasProjectJson(dir) {
  try {
    return fs.readdirSync(dir).some((f) => f.toLowerCase() === "project.json");
  } catch {
    return false;
  }
}

/** Remove an index entry without following a link it may be. */
function removeEntry(p) {
  let st;
  try {
    st = fs.lstatSync(p);
  } catch {
    return;
  }
  if (st.isSymbolicLink() || !st.isDirectory()) fs.unlinkSync(p);
  else fs.rmSync(p, { recursive: true, force: true });
}

// Real paths that are never read as project content (the index itself).
const EXCLUDED_REAL = new Set();
const isExcluded = (full) => {
  if (!EXCLUDED_REAL.size) return false;
  try {
    return EXCLUDED_REAL.has(fs.realpathSync(full));
  } catch {
    return false;
  }
};

const ACTIVITY_PACK_PREFIXES = ["UiPath.", "Microsoft.", "System.", "Newtonsoft.", "NuGet."];

function isActivityPack(name) {
  return ACTIVITY_PACK_PREFIXES.some((p) => name.startsWith(p));
}

/** NuGet spec -> exact version when the spec pins one, else null. */
function resolveVersion(spec) {
  const s = (spec || "").trim();
  let m = /^\[\s*([^,\]\s]+)\s*\]$/.exec(s);
  if (m) return m[1];
  if (/^[0-9][0-9A-Za-z.\-+]*$/.test(s)) return s;
  return null;
}

/** posixpath.normpath */
function normpath(p) {
  if (p === "") return ".";
  const initial = p.startsWith("/") ? (p.startsWith("//") && !p.startsWith("///") ? "//" : "/") : "";
  const out = [];
  for (const comp of p.split("/")) {
    if (comp === "" || comp === ".") continue;
    if (comp !== ".." || (!initial && out.length === 0) || (out.length && out[out.length - 1] === "..")) out.push(comp);
    else if (out.length) out.pop();
  }
  const r = initial + out.join("/");
  return r || ".";
}

function posixDirname(p) {
  const i = p.lastIndexOf("/");
  if (i < 0) return "";
  const head = p.slice(0, i + 1);
  return /^\/+$/.test(head) ? head : head.replace(/\/+$/, "");
}

function posixBasename(p) {
  return p.slice(p.lastIndexOf("/") + 1);
}

function posixJoin(a, b) {
  if (b.startsWith("/")) return b;
  if (!a || a.endsWith("/")) return a + b;
  return a + "/" + b;
}

/** urllib.parse.unquote: decode %XX sequences as UTF-8, leave invalid ones alone. */
function unquote(s) {
  if (!s.includes("%")) return s;
  return s.replace(/(?:%[0-9A-Fa-f]{2})+/g, (m) => {
    const bytes = Buffer.from(m.replace(/%/g, ""), "hex");
    return new TextDecoder("utf-8").decode(bytes);
  });
}

/** fnmatch.fnmatch (case handled by callers). */
function fnmatch(name, pat) {
  let re = "";
  for (let i = 0; i < pat.length; i++) {
    const c = pat[i];
    if (c === "*") re += "[\\s\\S]*";
    else if (c === "?") re += "[\\s\\S]";
    else if (c === "[") {
      let j = i + 1;
      if (pat[j] === "!") j++;
      if (pat[j] === "]") j++;
      while (j < pat.length && pat[j] !== "]") j++;
      if (j >= pat.length) re += "\\[";
      else {
        let stuff = pat.slice(i + 1, j).replace(/\\/g, "\\\\");
        if (stuff.startsWith("!")) stuff = "^" + stuff.slice(1);
        else if (stuff.startsWith("^")) stuff = "\\" + stuff;
        re += "[" + stuff + "]";
        i = j;
      }
    } else re += c.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
  }
  return new RegExp("^(?:" + re + ")$").test(name);
}

/** str.splitlines() */
function splitlines(text) {
  const parts = text.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (parts.length && parts[parts.length - 1] === "") parts.pop();
  return parts;
}

// ------------------------------------------------------------------------- SQLite

class DB {
  constructor(file, { readOnly = false } = {}) {
    if (readOnly) {
      try {
        this.db = new DatabaseSync(file, { readOnly: true });
      } catch {
        this.db = new DatabaseSync(file);
      }
      this.db.exec("PRAGMA temp_store = MEMORY"); // sorting and recursion never spill to temp files
      this.db.exec("PRAGMA query_only = ON");
    } else {
      this.db = new DatabaseSync(file);
    }
    // Another process (a second extract, a query) may hold the lock briefly: wait instead of failing.
    this.db.exec("PRAGMA busy_timeout = 60000");
    this.cache = new Map();
    this.inTx = false;
  }

  stmt(sql) {
    let s = this.cache.get(sql);
    if (!s) {
      s = this.db.prepare(sql);
      this.cache.set(sql, s);
    }
    return s;
  }

  static bind(params) {
    return params.map((v) => (v === undefined ? null : typeof v === "boolean" ? (v ? 1 : 0) : v));
  }

  /** Writes scrub every string: credential material never reaches the index, whatever column it is in. */
  static bindForWrite(params) {
    return DB.bind(params).map((v) => (typeof v === "string" && /[=:@]/.test(v) ? scrub(v) : v));
  }

  run(sql, ...params) {
    this.begin();
    return this.stmt(sql).run(...DB.bindForWrite(params));
  }

  /** Rows as arrays, in column order. */
  rows(sql, ...params) {
    const s = this.stmt(sql);
    if (typeof s.setReturnArrays === "function") {
      s.setReturnArrays(true);
      return s.all(...DB.bind(params));
    }
    // Node < 24 returns objects only, which collapse duplicate column names. As a subquery,
    // SQLite gives every result column a unique name ("NULL", "NULL:1"), in order.
    return this.stmt(`SELECT * FROM (${sql})`).all(...DB.bind(params)).map((r) => Object.values(r));
  }

  one(sql, ...params) {
    const r = this.rows(sql, ...params);
    return r.length ? r[0] : null;
  }

  objects(sql, ...params) {
    const s = this.stmt(sql);
    if (typeof s.setReturnArrays === "function") s.setReturnArrays(false);
    return s.all(...DB.bind(params));
  }

  exec(sql) {
    this.db.exec(sql);
  }

  begin() {
    if (!this.inTx) {
      this.db.exec("BEGIN IMMEDIATE");
      this.inTx = true;
    }
  }

  commit() {
    if (this.inTx) {
      this.db.exec("COMMIT");
      this.inTx = false;
    }
  }

  rollback() {
    if (this.inTx) {
      this.db.exec("ROLLBACK");
      this.inTx = false;
    }
  }

  close() {
    this.commit();
    this.db.close();
  }
}

function connect(indexDir) {
  fs.mkdirSync(indexDir, { recursive: true });
  for (const f of ["index.db", "index.db-journal", "index.db-wal", "index.db-shm"]) {
    let st = null;
    try { st = fs.lstatSync(path.join(indexDir, f)); } catch { /* absent */ }
    if (st && st.isSymbolicLink()) throw new Exit(`${path.join(indexDir, f)} is a symbolic link; refusing to open it. Remove it and retry.`);
  }
  const db = new DB(path.join(indexDir, "index.db"));
  try { // indexes created before the solution column existed
    db.exec("ALTER TABLE package ADD COLUMN solution TEXT");
  } catch { /* column exists or table not created yet */ }
  try { // ix_edge_pkg used to cover (package_id, version) only
    const def = db.one("SELECT sql FROM sqlite_master WHERE name='ix_edge_pkg'");
    if (def && !/kind/.test(def[0])) db.exec("DROP INDEX ix_edge_pkg");
  } catch { /* fresh index */ }
  db.exec(SCHEMA);
  db.run("INSERT OR REPLACE INTO meta VALUES ('tool_version', ?)", TOOL_VERSION);
  db.run("INSERT OR REPLACE INTO meta VALUES ('extractor_version', ?)", String(EXTRACTOR_VERSION));
  db.commit();
  refreshClosure(db);
  return db;
}

/** Rebuild the transitive library closure: one row per (root, reachable package), cycles visited once.
 * Package ids are matched case-insensitively (NuGet ids are). A library indexed from source stands in
 * for the pinned version when that exact version is not indexed. */
function refreshClosure(db) {
  const byId = new Map();
  for (const [id, ver] of db.rows("SELECT package_id, version FROM package")) {
    const k = String(id).toLowerCase();
    if (!byId.has(k)) byId.set(k, []);
    byId.get(k).push([id, ver]);
  }
  const deps = new Map();
  for (const [id, ver, dn, dv] of db.rows("SELECT package_id, version, dep_name, dep_version FROM dependency WHERE is_activity_pack = 0")) {
    const k = id + "\u0000" + ver;
    if (!deps.has(k)) deps.set(k, []);
    deps.get(k).push([dn, dv]);
  }
  const resolveDep = (dn, dv) => {
    const cands = byId.get(String(dn).toLowerCase()) || [];
    return cands.find((c) => dv !== null && c[1] === dv) || cands.find((c) => String(c[1]).startsWith("source")) || null;
  };
  db.run("DELETE FROM dep_closure");
  for (const list of byId.values()) {
    for (const [rid, rver] of list) {
      const seen = new Set([rid + "\u0000" + rver]);
      const queue = [[rid, rver, ""]];
      while (queue.length) {
        const [id, ver, via] = queue.shift();
        db.run("INSERT INTO dep_closure VALUES (?,?,?,?,?)", rid, rver, id, ver, via);
        for (const [dn, dv] of deps.get(id + "\u0000" + ver) || []) {
          const hit = resolveDep(dn, dv);
          if (!hit || seen.has(hit[0] + "\u0000" + hit[1])) continue;
          seen.add(hit[0] + "\u0000" + hit[1]);
          queue.push([hit[0], hit[1], (via ? via + " > " : "") + hit[0] + "@" + hit[1]]);
        }
      }
    }
  }
  db.commit();
}

function setMeta(db, key, value) {
  db.run("INSERT OR REPLACE INTO meta VALUES (?, ?)", key, value);
}

function coverage(db, pkg, ver, stage, status, detail = "") {
  db.run("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)", pkg, ver, stage, status, cut(detail || "", 2000), now());
}

// --------------------------------------------------------------------------- zip

const CP437 = "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■\u00a0";

function cp437(buf) {
  let s = "";
  for (const b of buf) s += b < 128 ? String.fromCharCode(b) : CP437[b - 128];
  return s;
}

/** Minimal ZIP reader (stored and deflate entries, ZIP64 offsets). */
class Zip {
  constructor(buf) {
    this.buf = buf;
    this.entries = [];
    this.inflated = 0; // bytes inflated so far: a package-wide budget, not only a per-file one
    const min = Math.max(0, buf.length - 65557);
    let eocd = -1;
    for (let i = buf.length - 22; i >= min; i--) {
      if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("File is not a zip file");
    let count = buf.readUInt16LE(eocd + 10);
    let cdOffset = buf.readUInt32LE(eocd + 16);
    if ((cdOffset === 0xffffffff || count === 0xffff) && eocd >= 20 && buf.readUInt32LE(eocd - 20) === 0x07064b50) {
      const z64 = Number(buf.readBigUInt64LE(eocd - 12));
      if (buf.readUInt32LE(z64) !== 0x06064b50) throw new Error("bad ZIP64 end of central directory");
      count = Number(buf.readBigUInt64LE(z64 + 32));
      cdOffset = Number(buf.readBigUInt64LE(z64 + 48));
    }
    if (count > MAX_ZIP_ENTRIES) throw new Error(`size limit: ${count} zip entries (limit ${MAX_ZIP_ENTRIES})`);
    let p = cdOffset;
    for (let n = 0; n < count; n++) {
      if (p + 46 > buf.length || buf.readUInt32LE(p) !== 0x02014b50) throw new Error("Bad magic number for central directory");
      const flags = buf.readUInt16LE(p + 8);
      const method = buf.readUInt16LE(p + 10);
      const crc = buf.readUInt32LE(p + 16);
      let csize = buf.readUInt32LE(p + 20);
      let usize = buf.readUInt32LE(p + 24);
      const nlen = buf.readUInt16LE(p + 28);
      const xlen = buf.readUInt16LE(p + 30);
      const clen = buf.readUInt16LE(p + 32);
      let local = buf.readUInt32LE(p + 42);
      const rawName = buf.subarray(p + 46, p + 46 + nlen);
      const name = flags & 0x800 ? rawName.toString("utf8") : cp437(rawName);
      let x = p + 46 + nlen;
      const xend = x + xlen;
      while (x + 4 <= xend) {
        const id = buf.readUInt16LE(x);
        const sz = buf.readUInt16LE(x + 2);
        if (id === 0x0001) {
          let q = x + 4;
          if (usize === 0xffffffff) { usize = Number(buf.readBigUInt64LE(q)); q += 8; }
          if (csize === 0xffffffff) { csize = Number(buf.readBigUInt64LE(q)); q += 8; }
          if (local === 0xffffffff) { local = Number(buf.readBigUInt64LE(q)); q += 8; }
        }
        x += 4 + sz;
      }
      this.entries.push({ name, flags, method, crc, csize, usize, local, isDir: name.endsWith("/") });
      p = xend + clen;
    }
  }

  names() {
    return this.entries.map((e) => e.name);
  }

  read(entry) {
    if (typeof entry === "string") {
      const found = [...this.entries].reverse().find((e) => e.name === entry);
      if (!found) throw new Error(`There is no item named '${entry}' in the archive`);
      entry = found;
    }
    if (entry.flags & 0x1) throw new Error(`File '${entry.name}' is encrypted`);
    if (entry.usize > MAX_FILE_BYTES) throw new Error(`size limit: '${entry.name}' is ${entry.usize} bytes (limit ${MAX_FILE_BYTES})`);
    if (this.inflated + entry.usize > MAX_PACKAGE_BYTES) throw new Error(`size limit: package contents exceed ${MAX_PACKAGE_BYTES} bytes`);
    const b = this.buf;
    if (entry.local + 30 > b.length || b.readUInt32LE(entry.local) !== 0x04034b50) throw new Error(`Bad magic number for file header: ${entry.name}`);
    const start = entry.local + 30 + b.readUInt16LE(entry.local + 26) + b.readUInt16LE(entry.local + 28);
    if (start + entry.csize > b.length) throw new Error(`Truncated entry: ${entry.name}`);
    const raw = b.subarray(start, start + entry.csize);
    let out;
    if (entry.method === 0) out = raw;
    else if (entry.method === 8) out = zlib.inflateRawSync(raw, { maxOutputLength: Math.max(entry.usize, 1) });
    else throw new Error(`compression type ${entry.method} not supported: ${entry.name}`);
    if (out.length !== entry.usize) throw new Error(`Bad size for file '${entry.name}'`);
    if ((zlib.crc32(out) >>> 0) !== entry.crc) throw new Error(`Bad CRC-32 for file '${entry.name}'`);
    this.inflated += out.length;
    return out;
  }
}

// --------------------------------------------------------------- XML with lines

class Node {
  constructor(ns, local, attrs, line, parent) {
    this.ns = ns;
    this.local = local;
    this.attrs = attrs;
    this.line = line;
    this.parent = parent;
    this.children = [];
    this.text = "";
  }
}

function decodeXml(buf) {
  let text;
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) text = new TextDecoder("utf-16le").decode(buf.subarray(2));
  else if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) text = new TextDecoder("utf-16be").decode(buf.subarray(2));
  else if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) text = new TextDecoder("utf-8").decode(buf.subarray(3));
  else {
    const head = buf.subarray(0, 4096).toString("latin1");
    const declared = (/^\s*<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']/.exec(head) || [])[1]?.toLowerCase();
    if (declared && /^(iso-8859-1|iso8859-1|latin-?1|l1|us-ascii|ascii)$/.test(declared)) {
      text = buf.toString("latin1");
    } else if (declared && /^(windows-125\d|cp125\d|iso-8859-\d+)$/.test(declared)) {
      try { text = new TextDecoder(declared).decode(buf); } catch { text = buf.toString("latin1"); }
    } else {
      // UTF-8, including Studio files that declare encoding="utf-16" but are written as UTF-8.
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(buf);
      } catch {
        text = buf.toString("latin1");
      }
    }
  }
  // Drop the declaration (its encoding label is often wrong) but keep its line breaks, so line numbers hold.
  return text.replace(/^\s*<\?xml[^>]*\?>/, (decl) => decl.replace(/[^\r\n]/g, ""));
}

const XML_NS = "http://www.w3.org/XML/1998/namespace";
const INVALID_XML_CHAR = /[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]/;
const NAME_RE = /[^\s\/>=<"']+/y;
// XML Name: a letter, '_' or ':' first; with namespaces, at most one ':' and not at either end.
const XML_NAME = /^[\p{L}_][\p{L}\p{M}\p{N}_.\-\u00B7\u203F\u2040]*(?::[\p{L}_][\p{L}\p{M}\p{N}_.\-\u00B7\u203F\u2040]*)?$/u;

class XmlError extends Error {}

function decodeEntities(s, line) {
  if (!s.includes("&")) return s;
  return s.replace(/&([^;&\s<]*);?/g, (m, ent) => {
    if (!m.endsWith(";")) throw new XmlError(`not well-formed (invalid token): line ${line}`);
    switch (ent) {
      case "lt": return "<";
      case "gt": return ">";
      case "amp": return "&";
      case "quot": return '"';
      case "apos": return "'";
      default: {
        let cp = NaN;
        if (/^#x[0-9A-Fa-f]+$/.test(ent)) cp = parseInt(ent.slice(2), 16);
        else if (/^#[0-9]+$/.test(ent)) cp = parseInt(ent.slice(1), 10);
        else throw new XmlError(`undefined entity: line ${line}`);
        const ok = cp === 0x9 || cp === 0xa || cp === 0xd || (cp >= 0x20 && cp <= 0xd7ff) ||
          (cp >= 0xe000 && cp <= 0xfffd) || (cp >= 0x10000 && cp <= 0x10ffff);
        if (!ok) throw new XmlError(`reference to invalid character number: line ${line}`);
        return String.fromCodePoint(cp);
      }
    }
  });
}

/**
 * Small non-validating XML parser with namespace resolution and line numbers,
 * matching what expat reports for well-formed documents. DTDs that declare
 * entities are refused (no entity expansion, no external resources).
 */
function parseXml(buf, { maxNodes = Infinity } = {}) {
  let nodes = 0;
  const src = decodeXml(buf).replace(/\r\n?/g, "\n");
  if (INVALID_XML_CHAR.test(src)) {
    const at = src.search(INVALID_XML_CHAR);
    throw new XmlError(`not well-formed (invalid token): line ${src.slice(0, at).split("\n").length}`);
  }
  let pos = 0;
  // Newline offsets, computed once: line lookups are O(log n), never a rescan of the document.
  const newlines = [];
  for (let i = src.indexOf("\n"); i >= 0; i = src.indexOf("\n", i + 1)) newlines.push(i);
  const lineAt = (p) => {
    let lo = 0;
    let hi = newlines.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (newlines[mid] < p) lo = mid + 1;
      else hi = mid;
    }
    return lo + 1;
  };
  let root = null;
  const stack = [];
  const bindings = new Map([["xml", [XML_NS]], ["", [""]]]); // prefix -> stack of URIs in scope
  const fail = (msg) => { throw new XmlError(`${msg}: line ${lineAt(pos)}`); };

  const addText = (t) => {
    if (stack.length) stack[stack.length - 1].text += t;
    else if (/\S/.test(t)) fail(root ? "junk after document element" : "syntax error");
  };

  while (pos < src.length) {
    const lt = src.indexOf("<", pos);
    if (lt < 0) {
      addText(decodeEntities(src.slice(pos), lineAt(pos)));
      pos = src.length;
      break;
    }
    if (lt > pos) {
      const raw = src.slice(pos, lt);
      if (raw.includes(">") && raw.includes("]]>")) { pos = pos + raw.indexOf("]]>"); fail("not well-formed (invalid token)"); }
      addText(decodeEntities(raw, lineAt(pos)));
      pos = lt;
    }
    if (src.startsWith("<!--", pos)) {
      const end = src.indexOf("-->", pos + 4);
      if (end < 0) fail("unclosed token");
      const dd = src.indexOf("--", pos + 4);
      if (dd !== end) { pos = dd; fail("not well-formed (invalid token)"); }
      pos = end + 3;
    } else if (src.startsWith("<![CDATA[", pos)) {
      const end = src.indexOf("]]>", pos + 9);
      if (end < 0) fail("unclosed CDATA section");
      if (!stack.length) fail("syntax error");
      stack[stack.length - 1].text += src.slice(pos + 9, end);
      pos = end + 3;
    } else if (src.startsWith("<?", pos)) {
      const end = src.indexOf("?>", pos + 2);
      if (end < 0) fail("unclosed token");
      if (/^xml(\s|$)/i.test(src.slice(pos + 2, end))) fail("XML or text declaration not at start of entity");
      pos = end + 2;
    } else if (src.startsWith("<!DOCTYPE", pos)) {
      if (root) fail("syntax error");
      let depth = 0;
      let p = pos + 9;
      for (; p < src.length; p++) {
        const ch = src[p];
        if (ch === "[") depth++;
        else if (ch === "]") depth--;
        else if (ch === ">" && depth <= 0) break;
      }
      if (p >= src.length) fail("unclosed token");
      // Declarations in an internal subset (entities, attribute defaults) would change the document; refuse them.
      if (/<!(ENTITY|ATTLIST|ELEMENT|NOTATION)/i.test(src.slice(pos, p))) fail("DTD declarations are not supported");
      pos = p + 1;
    } else if (src[pos + 1] === "/") {
      pos += 2;
      NAME_RE.lastIndex = pos;
      const m = NAME_RE.exec(src);
      if (!m) fail("not well-formed (invalid token)");
      pos += m[0].length;
      while (/\s/.test(src[pos] || "")) pos++;
      if (src[pos] !== ">") fail("not well-formed (invalid token)");
      pos++;
      const top = stack.pop();
      if (!top || top._qname !== m[0]) { pos -= 1; fail("mismatched tag"); }
      for (const pfx of top._declared) bindings.get(pfx).pop();
      delete top._qname;
      delete top._declared;
    } else {
      const tagLine = lineAt(pos);
      pos += 1;
      NAME_RE.lastIndex = pos;
      const m = NAME_RE.exec(src);
      if (!m) fail("not well-formed (invalid token)");
      const qname = m[0];
      if (!XML_NAME.test(qname)) fail("not well-formed (invalid token)");
      pos += qname.length;
      const rawAttrs = [];
      const seen = new Set();
      let selfClose = false;
      for (;;) {
        const before = pos;
        while (/\s/.test(src[pos] || "")) pos++;
        if (src.startsWith("/>", pos)) { pos += 2; selfClose = true; break; }
        if (src[pos] === ">") { pos += 1; break; }
        if (pos >= src.length) fail("unclosed token");
        if (pos === before) fail("not well-formed (invalid token)");
        NAME_RE.lastIndex = pos;
        const an = NAME_RE.exec(src);
        if (!an) fail("not well-formed (invalid token)");
        pos += an[0].length;
        while (/\s/.test(src[pos] || "")) pos++;
        if (src[pos] !== "=") fail("not well-formed (invalid token)");
        pos++;
        while (/\s/.test(src[pos] || "")) pos++;
        const q = src[pos];
        if (q !== '"' && q !== "'") fail("not well-formed (invalid token)");
        const end = src.indexOf(q, pos + 1);
        if (end < 0) fail("unclosed token");
        const rawVal = src.slice(pos + 1, end);
        if (rawVal.includes("<")) fail("not well-formed (invalid token)");
        if (!XML_NAME.test(an[0])) fail("not well-formed (invalid token)");
        if (seen.has(an[0])) fail("duplicate attribute");
        seen.add(an[0]);
        rawAttrs.push([an[0], decodeEntities(rawVal.replace(/[\t\n]/g, " "), lineAt(pos))]);
        pos = end + 1;
      }
      const declared = [];
      for (const [k, v] of rawAttrs) {
        if (k === "xmlns" || k.startsWith("xmlns:")) {
          const prefix = k === "xmlns" ? "" : k.slice(6);
          if (prefix === "xmlns" || (prefix === "xml" && v !== XML_NS) || (prefix !== "xml" && v === XML_NS)) fail("reserved prefix (xml) must not be undeclared or bound to another namespace name");
          if (!bindings.has(prefix)) bindings.set(prefix, []);
          bindings.get(prefix).push(v);
          declared.push(prefix);
        }
      }
      const popDeclared = () => { for (const pfx of declared) bindings.get(pfx).pop(); };
      const lookup = (pfx) => { const st = bindings.get(pfx); return st && st.length ? st[st.length - 1] : undefined; };
      const resolve = (q, isAttr) => {
        const i = q.indexOf(":");
        if (i < 0) return [isAttr ? "" : lookup("") || "", q];
        const prefix = q.slice(0, i);
        const uri = lookup(prefix);
        if (uri === undefined || uri === "") { popDeclared(); fail("unbound prefix"); }
        return [uri, q.slice(i + 1)];
      };
      const [ns, local] = resolve(qname, false);
      const attrs = Object.create(null); // no prototype: an attribute named __proto__ is just an attribute
      const expanded = new Set();
      for (const [k, v] of rawAttrs) {
        if (k === "xmlns" || k.startsWith("xmlns:")) continue;
        const [ans, alocal] = resolve(k, true);
        const key = ans + "}" + alocal;
        if (expanded.has(key)) fail("duplicate attribute");
        expanded.add(key);
        delete attrs[alocal];
        attrs[alocal] = v;
      }
      if (!stack.length && root) fail("junk after document element");
      if (++nodes > maxNodes) fail(`size limit: more than ${maxNodes} elements`);
      const node = new Node(ns, local, attrs, tagLine, stack.length ? stack[stack.length - 1] : null);
      if (stack.length) stack[stack.length - 1].children.push(node);
      else root = node;
      if (!selfClose) {
        node._qname = qname;
        node._declared = declared;
        stack.push(node);
      } else {
        popDeclared();
      }
    }
  }
  if (stack.length) fail("no element found");
  if (!root) fail("no element found");
  return root;
}

function* iterNodes(root) {
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    for (let i = n.children.length - 1; i >= 0; i--) stack.push(n.children[i]);
  }
}

// ------------------------------------------------------------- value semantics

const W = "\\p{L}\\p{N}_"; // Python's Unicode \w
const IDENT_RE = new RegExp(`^[A-Za-z_][${W}]*$`, "u");
const VB_LITERAL = /^\s*"((?:[^"]|"")*)"\s*$/;
const CS_LITERAL = /^\s*@?"((?:[^"\\]|\\[^\n]|"")*)"\s*$/;
// The settings dictionary: real projects name it Config, in_Config, io_Config, io_DictConfig, DictionaryConfig,
// dict_Config, out_Config... always ending in "Config". Any dictionary whose name ends in Config is read as it.
const CONFIG_NAME = /config$/i;
const CONFIG_REF = /Config\s*(?:\(\s*|\[\s*)"([^"]+)"\s*[)\]]/gi;
const URL_RE = /https?:\/\/[^\s"'<>\][{}|\\^`]+/gi;
const EMAIL_DOMAIN_RE = /[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})/g;

const findall = (re, text) => Array.from(String(text || "").matchAll(re), (m) => (m.length > 1 ? m[1] : m[0]));

class Val {
  constructor(raw, stat = null, expr = null) {
    this.raw = raw;
    this.static = stat;
    this.expr = expr;
  }

  get isStatic() {
    return this.static !== null && this.static !== undefined;
  }
}

function classifyAttr(v) {
  if (v === null || v === undefined) return null;
  let s = v.trim();
  if (!s || s === "{x:Null}") return null;
  if (s.startsWith("{") && s.endsWith("}") && !s.startsWith("{}")) return new Val(v, null, s); // markup extension
  if (s.startsWith("{}")) s = s.slice(2);
  if (s.startsWith("[") && s.endsWith("]")) {
    const inner = s.slice(1, -1);
    const m = VB_LITERAL.exec(inner) || CS_LITERAL.exec(inner);
    if (m) return new Val(v, m[1].replaceAll('""', '"'), inner);
    return new Val(v, null, inner);
  }
  return new Val(v, s, null);
}

const EXPR_NODES = new Set(["CSharpValue", "VisualBasicValue", "CSharpReference", "VisualBasicReference"]);

function classifyChild(propNode) {
  for (const n of iterNodes(propNode)) {
    if (n === propNode) continue;
    if (EXPR_NODES.has(n.local)) {
      const text = n.attrs.ExpressionText || n.text.trim();
      const m = CS_LITERAL.exec(text) || VB_LITERAL.exec(text);
      if (m) return new Val(text, m[1], text);
      return new Val(text, null, text);
    }
    if (n.local === "Literal" && "Value" in n.attrs) return new Val(n.attrs.Value, n.attrs.Value, null);
  }
  const text = propNode.text.trim();
  if (text) return classifyAttr(text);
  for (const n of iterNodes(propNode)) {
    const t = n.text.trim();
    if (t) return classifyAttr(t);
  }
  return null;
}

function prop(node, name) {
  if (name in node.attrs) return classifyAttr(node.attrs[name]);
  const wanted = node.local + "." + name;
  for (const c of node.children) if (c.local === wanted) return classifyChild(c);
  return null;
}

function firstProp(node, ...names) {
  for (const n of names) {
    const v = prop(node, n);
    if (v !== null) return v;
  }
  return null;
}

// --------------------------------------------------------------------- selectors

const SEL_TAG = new RegExp(`<\\s*([A-Za-z][${W}-]*)((?:\\s+[${W}:-]+\\s*=\\s*(?:'[^']*'|"[^"]*"))*)\\s*/?>`, "u");
const SEL_ATTR = new RegExp(`([${W}:-]+)\\s*=\\s*(?:'([^']*)'|"([^"]*)")`, "gu");

function htmlUnescape(s) {
  for (let i = 0; i < 3; i++) { // selectors in Object Repository files are often double-escaped
    const t = s.replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", '"').replaceAll("&apos;", "'").replaceAll("&amp;", "&");
    if (t === s) break;
    s = t;
  }
  return s;
}

/** Top-level node of a selector: [tag, attrs] or null. */
function parseSelector(sel) {
  sel = htmlUnescape(sel);
  const m = SEL_TAG.exec(sel);
  if (!m) return null;
  const attrs = Object.create(null);
  for (const am of (m[2] || "").matchAll(SEL_ATTR)) attrs[am[1].toLowerCase()] = am[2] !== undefined ? am[2] : am[3];
  return [m[1].toLowerCase(), attrs];
}

const BROWSER_EXE = { edge: "msedge.exe", chrome: "chrome.exe", firefox: "firefox.exe", ie: "iexplore.exe", internetexplorer: "iexplore.exe", safari: "safari" };

function normApp(app) {
  let a = app.trim().replace(/^"+|"+$/g, "").replaceAll("\\", "/").split("/").pop().toLowerCase();
  if (/^[a-z0-9_-]+$/.test(a)) a += ".exe"; // StartProcess "outlook" and selector app='outlook.exe' are the same application
  return a;
}

/** urllib.parse.urlsplit(url).hostname, lower-cased; "" when absent or invalid. */
function normHost(url) {
  const s = url.trim().replace(/[\t\r\n]/g, "");
  const m = /^(?:[A-Za-z][A-Za-z0-9+.-]*:)?\/\/([^/?#]*)/.exec(s);
  if (!m) return "";
  const netloc = m[1];
  if (netloc.includes("[") !== netloc.includes("]")) return "";
  const hostinfo = netloc.includes("@") ? netloc.slice(netloc.lastIndexOf("@") + 1) : netloc;
  let host;
  if (hostinfo.includes("[")) {
    const b = hostinfo.indexOf("[");
    const e = hostinfo.indexOf("]", b);
    host = e < 0 ? "" : hostinfo.slice(b + 1, e);
  } else {
    host = hostinfo.split(":")[0];
  }
  return host.toLowerCase();
}

const hostOr = (s) => normHost(s) || s.toLowerCase();

// ------------------------------------------------------------------ activity maps

const set = (...a) => new Set(a);
const ASSET_READ = set("GetAsset", "GetRobotAsset");
const ASSET_WRITE = set("SetAsset");
const CRED_READ = set("GetCredential", "GetRobotCredential", "GetSecret");
const CRED_WRITE = set("SetCredential");
const QUEUE_ADD = set("AddQueueItem", "AddTransactionItem", "BulkAddQueueItems", "AddQueueItemAndGetReference");
const QUEUE_GET = set("GetQueueItem", "GetTransactionItem", "GetQueueItems", "WaitQueueItem", "WaitForQueueItemAndResume",
  "DeleteQueueItems", "GetTestDataQueueItems", "GetTestDataQueueItem", "AddTestDataQueueItem", "BulkAddTestDataQueue");
const PROCESS_CALL = set("InvokeProcess", "BeginProcess", "StartJob", "StartJobAndGetReference", "RunJob");
const WORKFLOW_CALL = set("InvokeWorkflowFile", "StartTriggers", "InvokeWorkflowInteractive");
const CONTEXT_ACTIVITIES = new Set([...ASSET_READ, ...CRED_READ, ...QUEUE_ADD, ...QUEUE_GET, ...PROCESS_CALL, ...WORKFLOW_CALL]);
const isEdgeActivity = (L) => CONTEXT_ACTIVITIES.has(L) || URL_ACTIVITIES.has(L) || L in APP_LAUNCH || L in OFFICE || L in CODE_ACTIVITIES ||
  DB_ACTIVITIES.has(L) || MAIL_SEND.has(L) || MAIL_SERVER.has(L) || (!L.includes(".") && (L.includes("Http") || L.endsWith("StorageFile") || L.endsWith("StorageText")));
const URL_ACTIVITIES = set("OpenBrowser", "NGoToUrl", "NavigateTo", "GoToUrl", "NBrowser");
const APP_LAUNCH = { OpenApplication: "FileName", StartProcess: "FileName" };
const OFFICE = {
  ExcelApplicationScope: ["excel.exe", "WorkbookPath"], ExcelApplicationCard: ["excel.exe", "WorkbookPath"],
  ExcelProcessScopeX: ["excel.exe", null], WordApplicationScope: ["winword.exe", "FilePath"],
  WordApplicationCard: ["winword.exe", "FilePath"], PowerPointApplicationScope: ["powerpnt.exe", "PresentationPath"],
  PowerPointApplicationCard: ["powerpnt.exe", "PresentationPath"],
  SendOutlookMail: ["outlook.exe", null], GetOutlookMailMessages: ["outlook.exe", null],
  OutlookApplicationCard: ["outlook.exe", null], SaveOutlookMailMessage: ["outlook.exe", null],
  MoveOutlookMessage: ["outlook.exe", null], ReplyToOutlookMailMessage: ["outlook.exe", null],
  SAPLogon: ["saplogon.exe", null], SAPLogin: ["saplogon.exe", null],
};
const CODE_ACTIVITIES = {
  InvokeCode: ["Code", "Language"], InvokePowerShell: ["CommandText", null], InvokeVBScript: ["FileName", null],
  InvokeVBA: ["CodeFilePath", null], InvokeJavaScript: ["CodeFilePath", null], InvokePython: ["Code", null],
  RunPythonScript: ["Code", null], LoadPythonScript: ["Code", null],
};
const DB_ACTIVITIES = set("DatabaseConnect", "ExecuteQuery", "ExecuteNonQuery", "InsertDataTable", "BulkInsert", "BulkUpdate", "RunCommand");
const MAIL_SEND = set("SendOutlookMail", "SendMail", "SendSmtpMailMessage", "SendExchangeMail", "SendEmail", "SendEmailConnections", "SendMailX");
const MAIL_SERVER = set("SendMail", "SendSmtpMailMessage", "GetIMAPMailMessages", "GetPOP3MailMessages", "GetImapMailMessage",
  "GetPop3MailMessages", "SendSmtpMail");
const SELECTOR_ATTRS = set("Selector", "FullSelectorArgument", "FuzzySelectorArgument", "ScopeSelectorArgument", "FullSelector",
  "FuzzySelector", "ScopeSelector", "WindowSelector");
const SECRET_ATTRS = set("Password", "SecurePassword", "ConsumerSecret", "OAuth2Token", "OAuth1Token", "OAuth1TokenSecret", "ApiKey",
  "ClientSecret", "Token", "SecureCredentialSecret", "EditPassword", "SecureText", "AccessToken", "PrivateKey", "Passphrase");
const SKIP_SCAN_ATTRS = set("ImageBase64", "InformativeScreenshot", "IconBase64", "TargetImageBase64", "Image", "Class", "Ignorable",
  "HintSize", "DesignTimeRectangle", "ContentHash", "Guid", "DocumentationLink");
const FILE_ATTRS = set("WorkbookPath", "FilePath", "FileName", "PresentationPath", "Path", "Destination", "Folder", "DestinationFolder", "TemplatePath");
const SECRET_IN_EXPR = [
  [/NetworkCredential\(\s*"[^"]*"\s*,\s*"([^"]+)"/gi, "NetworkCredential literal password"],
  [/(?:password|pwd)\s*=\s*([^;"\s]{3,})/gi, "password in connection string"],
];
const SECRET_TOKENS = new Set(["pass", "password", "passwd", "pwd", "pin", "passphrase", "passcode", "secret", "token", "apikey", "privatekey", "clientsecret"]);
const SECRET_JOINED = /password|passwd|passphrase|apikey|privatekey|clientsecret|secret|token/;

/** Does a setting's name say its value is a credential? (SAPPass, User_Pin, ApiKey, ClientSecret...) */
/** Copy of a JSON value with every secret-named key's value redacted, at any depth. */
function redactSecretKeys(v) {
  if (Array.isArray(v)) return v.map(redactSecretKeys);
  if (!isObject(v)) return v;
  return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, isSecretName(k) && x !== null && typeof x !== "object" && x !== "" ? redacted(String(x)) : redactSecretKeys(x)]));
}

function isSecretName(name) {
  const tokens = String(name).replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/).map((t) => t.toLowerCase()).filter(Boolean);
  return tokens.some((t) => SECRET_TOKENS.has(t)) || SECRET_JOINED.test(tokens.join(""));
}
const FILE_LIKE_RE = new RegExp(`[\\\\/]|\\.[${W}]{2,5}(?=\\n?$)`, "u");

function redacted(v) {
  return `<redacted len=${Array.from(v).length}>`;
}

const CRED_KEYS = "password|passwd|pass|pwd|passphrase|secret|client_secret|clientsecret|token|access_token|refresh_token|apikey|api_key|api-key|accesskey|sharedaccesskey|sig";
// key = value, key: value, "key": "value", key='value'. A quoted value runs to its closing quote (backslash
// escapes respected); an unquoted one to & , } # or line end, and past ';' unless another key=value follows
// (connection strings: "pwd=a b;Server=x" redacts "a b", "token=a;b" redacts "a;b").
const CRED_SEGMENT = new RegExp(String.raw`((?<![A-Za-z0-9])(?:${CRED_KEYS})["']?\s*[=:]\s*)(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|((?:(?!;\s*[A-Za-z][\w .-]*=)[^&\r\n"'<>,}#])+))`, "gi");
const QUERY_PARAM = /([?&;])([^=&#\s"'<>]+)=([^&#\s"'<>]*)/g;
const CRED_KEY_EXACT = new RegExp(`^(?:${CRED_KEYS})$`, "i");
const URL_USERINFO = /(https?:\/\/)([^/@\s:]+):([^/@\s]+)@/gi;

/** Remove credential material from any value before it is stored. */
function scrub(text) {
  if (!text) return text;
  text = text.replace(CRED_SEGMENT, (m, key, dq, sq, bare) => {
    if (dq !== undefined) return dq ? `${key}"${redacted(dq)}"` : m;
    if (sq !== undefined) return sq ? `${key}'${redacted(sq)}'` : m;
    const v = bare.replace(/\s+$/, "");
    if (!v || v.startsWith("<redacted")) return m;
    return key + redacted(v) + bare.slice(v.length);
  });
  // URL parameters whose (possibly percent-encoded) name is a credential: ?%74oken=...
  if (text.includes("%")) {
    text = text.replace(QUERY_PARAM, (m, sep, name, val) => (val && !val.startsWith("<redacted") && CRED_KEY_EXACT.test(unquote(name)) ? `${sep}${name}=${redacted(val)}` : m));
  }
  return text.replace(URL_USERINFO, (m, a, b, c) => a + b + ":" + redacted(c) + "@");
}

/** 'Server=x;Database=y;...' -> 'x/y'; null when the text is not a connection string. */
function dbTarget(conn) {
  if (!conn.includes("=") || !conn.includes(";")) return null;
  const parts = {};
  for (const kv of conn.split(";")) {
    const i = kv.indexOf("=");
    if (i >= 0) parts[kv.slice(0, i).trim().toLowerCase()] = kv.slice(i + 1).trim();
  }
  const server = parts.server || parts["data source"] || parts.host || parts.dsn || parts.address;
  if (!server) return null;
  const db = parts.database || parts["initial catalog"];
  return server + (db ? `/${db}` : "");
}

// ------------------------------------------------------------------- the extractor

const lower = (s) => s.toLowerCase();
const DYN = "<dynamic>";

/** The Config key when the whole expression is a Config lookup (Config("K"), in_Config("K").ToString.Trim), else null. */
const WHOLE_CONFIG = /^\s*([A-Za-z_][\w]*)\s*(?:\(\s*"([^"]+)"\s*\)|\[\s*"([^"]+)"\s*\])(?:\s*\.\s*(?:ToString|Trim|TrimEnd|TrimStart)\s*(?:\(\s*\))?)*\s*$/;
function wholeConfigKey(expr) {
  const m = WHOLE_CONFIG.exec(expr || "");
  return m && CONFIG_NAME.test(m[1]) ? m[2] ?? m[3] : null;
}

class PackageExtractor {
  constructor(db, pkg, ver, root, configValues) {
    this.db = db;
    this.pkg = pkg;
    this.ver = ver;
    this.root = root;
    this.config = configValues; // key -> [value, label, secret, isAssetSheet]
    this.seen = new Set();
    this.edges = [];
    this.invocations = [];
    this.workflows = [];
    this.argBindings = []; // [caller file, target raw, argument name, Val, ctx]
    this.varSources = new Map(); // "file\0variable" -> [["val", Val, line] | ["asset", name, line]]
  }

  edge(kind, value, ctx, normalized = null, detail = null, stat = true, extractor = "xaml", confidence = "high") {
    if (value === null || value === undefined) return;
    value = scrub(String(value).trim());
    if (!value) return;
    if (kind === "uses_database" && normalized !== DYN) {
      const tgt = dbTarget(value);
      if (tgt) { value = tgt; normalized = tgt.toLowerCase(); }
    }
    if (normalized === null || normalized === undefined) normalized = value.toLowerCase();
    if ((kind === "targets_url" || kind === "calls_api") && normalized !== DYN && /^https?:\/\//i.test(value)) {
      normalized = normHost(value.replaceAll("*", "x")) || normalized;
    }
    if (kind === "uses_application" && normalized !== DYN) normalized = normApp(value);
    normalized = scrub(normalized);
    detail = detail ? scrub(detail) : detail;
    // One row per fact per activity; without an activity (coded lines, config rows) one per line.
    const key = [kind, normalized, value, ctx.file ?? "", ctx.activity ?? `#${ctx.line ?? ""}`].join("\u0000");
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.edges.push([this.pkg, this.ver, kind, cut(value, 4000), cut(normalized, 1000),
      cut(detail || "", 1000) || null, stat ? 1 : 0, ctx.file ?? null, ctx.line ?? null,
      ctx.activity ?? null, ctx.activity_type ?? null, extractor, confidence]);
  }

  /** Emit an edge for a property value: static, Config-resolved, or dynamic. */
  valueEdge(kind, val, ctx, extractor = "xaml", detail = null, normalize = null) {
    if (val === null) return;
    const norm = normalize || lower;
    if (val.isStatic) {
      this.edge(kind, val.static, ctx, norm(val.static), detail, true, extractor, "high");
      return;
    }
    const expr = val.expr || val.raw;
    let resolved = false;
    const whole = wholeConfigKey(expr);
    if (!whole && findall(CONFIG_REF, expr).length) {
      // Not a fact (the runtime value also depends on the rest of the expression), but a useful hint.
      const parts = findall(CONFIG_REF, expr).map((k) => {
        const hit = this.config.get(k.toLowerCase());
        if (!hit || !hit[0] || hit[2]) return `'${k}'`;
        return hit[3] ? `'${k}' (asset '${hit[0]}')` : `'${k}' (= '${cut(hit[0], 200)}')`;
      });
      detail = (detail ? detail + "; " : "") + `expression combines Config ${parts.join(", ")} with other values`;
    }
    for (const k of whole ? [whole] : []) {
      const hit = this.config.get(k.toLowerCase());
      if (hit && hit[0] && !hit[2]) {
        if (hit[3]) { // Assets sheet: the Config value names an Orchestrator asset
          const d = `value of Orchestrator asset '${hit[0]}' (Config key '${k}')` + (detail ? `; ${detail}` : "");
          this.edge(kind, expr, ctx, DYN, d, false, extractor + "+config", "medium");
        } else {
          const d = `via Config key '${k}' (${hit[1]})` + (detail ? `; ${detail}` : "");
          this.edge(kind, hit[0], ctx, norm(hit[0]), d, false, extractor + "+config", "medium");
        }
        resolved = true;
      }
    }
    if (!resolved) this.edge(kind, expr, ctx, DYN, ("dynamic: " + (detail || "")).trim(), false, extractor, "low");
  }

  xaml(rel, data) {
    let root;
    try {
      root = parseXml(data);
    } catch (e) {
      this.workflows.push([this.pkg, this.ver, rel, "xaml", 0, null, cut(String(e.message), 500), sha256(data)]);
      return;
    }
    const count = this.walk(root, rel, "xaml");
    this.workflows.push([this.pkg, this.ver, rel, "xaml", count, null, null, sha256(data)]);
  }

  walk(root, rel, extractor, activityPrefix = null) {
    let count = 0;
    const lineUse = new Map();
    // iterative DFS keeping the nearest activity (node with an IdRef) as context
    const stack = [[root, { file: rel, line: root.line, activity: activityPrefix, activity_type: null }]];
    while (stack.length) {
      const [node, parentCtx] = stack.pop();
      const idref = node.attrs["WorkflowViewState.IdRef"];
      let ctx;
      const secretProp = node.local.includes(".") && SECRET_ATTRS.has(node.local.split(".").pop()) ? node.local : null;
      if (idref || isEdgeActivity(node.local)) {
        // Without an IdRef (older Studio files) line and position keep identical activities apart.
        let label = idref;
        if (!label) {
          const base = `${node.local}@L${node.line}`;
          const k = lineUse.get(base) || 0;
          lineUse.set(base, k + 1);
          label = k ? `${base}#${k + 1}` : base;
        }
        const dn = node.attrs.DisplayName;
        ctx = { file: rel, line: node.line, activity: `${label}` + (dn ? ` (${dn})` : ""), activity_type: node.local };
        if (idref) count++;
      } else {
        ctx = { ...parentCtx, line: node.line };
      }
      if (secretProp) ctx = { ...ctx, secret: secretProp };
      this.visit(node, ctx, extractor);
      for (let i = node.children.length - 1; i >= 0; i--) stack.push([node.children[i], ctx]);
    }
    return count;
  }

  addVarSource(file, name, src) {
    const k = file + "\u0000" + name.toLowerCase();
    if (!this.varSources.has(k)) this.varSources.set(k, []);
    this.varSources.get(k).push(src);
  }

  visit(n, ctx, extractor) {
    const L = n.local;
    if (ctx.secret) { // inside Activity.Password (or another secret property): record a literal, read nothing else
      if (EXPR_NODES.has(L) || L === "Literal") {
        const text = L === "Literal" ? n.attrs.Value ?? "" : n.attrs.ExpressionText || n.text.trim();
        const lit = L === "Literal" ? [null, text] : CS_LITERAL.exec(text) || VB_LITERAL.exec(text);
        if (lit && lit[1]) this.edge("hardcoded_secret", redacted(lit[1]), ctx, ctx.secret.toLowerCase(), `literal value in ${ctx.secret}`, true, extractor, "high");
      }
      return;
    }
    if (L === "Assign") {
      const to = prop(n, "To");
      const val = prop(n, "Value");
      const target = to && (to.expr || to.raw || "").trim();
      if (target && IDENT_RE.test(target) && val !== null) this.addVarSource(ctx.file, target, ["val", val, ctx.line]);
    }
    if (ASSET_READ.has(L)) {
      const out = firstProp(n, "Value");
      const name = firstProp(n, "AssetName");
      const target = out && (out.expr || "").trim();
      if (target && name !== null && name.isStatic && IDENT_RE.test(target)) this.addVarSource(ctx.file, target, ["asset", name.static, ctx.line]);
    }
    if (ASSET_READ.has(L)) {
      this.valueEdge("reads_asset", firstProp(n, "AssetName"), ctx, extractor, this.folderOf(n));
    } else if (ASSET_WRITE.has(L)) {
      this.valueEdge("writes_asset", firstProp(n, "AssetName"), ctx, extractor, this.folderOf(n));
    } else if (CRED_READ.has(L)) {
      this.valueEdge("reads_credential", firstProp(n, "AssetName", "CredentialName", "SecretName"), ctx, extractor, this.folderOf(n));
    } else if (CRED_WRITE.has(L)) {
      this.valueEdge("writes_credential", firstProp(n, "CredentialName", "AssetName"), ctx, extractor, this.folderOf(n));
    } else if (QUEUE_ADD.has(L) || QUEUE_GET.has(L)) {
      const mode = QUEUE_ADD.has(L) ? "add" : "get";
      const f = this.folderOf(n);
      this.valueEdge("uses_queue", firstProp(n, "QueueName", "QueueType"), ctx, extractor, mode + (f ? "; " + f : ""));
    } else if (PROCESS_CALL.has(L)) {
      this.valueEdge("invokes_process", firstProp(n, "ProcessName"), ctx, extractor, this.folderOf(n));
      const v = firstProp(n, "ProcessName");
      if (v !== null) this.invocations.push([this.pkg, this.ver, ctx.file, ctx.line, ctx.activity, "invoke_process", v.static || v.expr, null, v.isStatic ? 1 : 0, extractor]);
    } else if (WORKFLOW_CALL.has(L)) {
      const v = firstProp(n, "WorkflowFileName");
      if (v !== null) {
        this.invocations.push([this.pkg, this.ver, ctx.file, ctx.line, ctx.activity, "invoke_workflow", v.static || v.expr, null, v.isStatic ? 1 : 0, extractor]);
        if (v.isStatic) {
          for (const c of n.children) {
            if (c.local !== L + ".Arguments") continue;
            for (const arg of c.children) {
              const key = arg.attrs.Key;
              if (key && (arg.local === "InArgument" || arg.local === "InOutArgument")) {
                const val = arg.children.length ? classifyChild(arg) : classifyAttr(arg.text.trim());
                if (val !== null) this.argBindings.push([ctx.file, v.static, key, val, ctx]);
              }
            }
          }
        }
      }
    } else if (L.endsWith("StorageFile") || L.endsWith("StorageText") || L.endsWith("StorageFiles")) {
      this.valueEdge("uses_bucket", firstProp(n, "StorageBucketName", "BucketName"), ctx, extractor, this.folderOf(n));
    } else if (URL_ACTIVITIES.has(L)) {
      this.valueEdge("targets_url", firstProp(n, "Url"), ctx, extractor, L, hostOr);
    } else if (L in APP_LAUNCH) {
      this.valueEdge("uses_application", firstProp(n, APP_LAUNCH[L]), ctx, extractor, L, normApp);
    } else if (L === "TargetApp") {
      this.targetApp(n, ctx, extractor);
    } else if (L.includes("Http") || L === "SoapClient" || L === "RestRequest" || L === "NetHttpRequest") {
      this.valueEdge("calls_api", firstProp(n, "EndPoint", "Endpoint", "Url", "RequestUrl", "BaseUrl", "Uri", "ServiceUrl"),
        ctx, extractor, n.attrs.Method || L, hostOr);
    }
    if (L in OFFICE) {
      const [app, pathAttr] = OFFICE[L];
      this.edge("uses_application", app, ctx, app, `${L} activity`, true, extractor, "medium");
      if (pathAttr) this.valueEdge("uses_file", prop(n, pathAttr), ctx, extractor, L, lower);
    }
    if (L in CODE_ACTIVITIES) {
      const [codeAttr, langAttr] = CODE_ACTIVITIES[L];
      const code = prop(n, codeAttr);
      const lang = langAttr ? n.attrs[langAttr] : null;
      const body = code ? (code.static || code.expr || code.raw) : "";
      this.edge("runs_code", L, ctx, L.toLowerCase(), `language=${lang || "default"}; ${Array.from(body).length} chars`, true, extractor, "high");
      this.scanText(body, ctx, extractor + ":code");
    }
    if (DB_ACTIVITIES.has(L)) this.database(n, ctx, extractor);
    if (MAIL_SEND.has(L)) {
      for (const attr of ["To", "Cc", "Bcc"]) {
        const v = prop(n, attr);
        if (v !== null && v.isStatic) {
          for (const dom of [...new Set(findall(EMAIL_DOMAIN_RE, v.static))].sort()) {
            this.edge("sends_email_to_domain", dom, ctx, dom.toLowerCase(), attr, true, extractor, "high");
          }
        }
      }
    }
    if (MAIL_SERVER.has(L)) this.valueEdge("uses_mail_server", firstProp(n, "Server", "Host"), ctx, extractor, L);
    if ("ConnectionId" in n.attrs || "ConnectorKey" in n.attrs) {
      const conn = n.attrs.ConnectorKey || n.attrs.Connector || L;
      const det = ["ConnectionName", "ConnectionAccountName", "ConnectionId", "ObjectName", "Operation"]
        .filter((k) => n.attrs[k] && !n.attrs[k].startsWith("{")).map((k) => `${k}=${n.attrs[k]}`).join("; ");
      this.edge("uses_connection", conn, ctx, conn.toLowerCase(), det || null, true, extractor, "high");
    }
    // generic attribute passes
    for (const [k, v] of Object.entries(n.attrs)) {
      if (SKIP_SCAN_ATTRS.has(k) || k.endsWith("AnnotationText") || !v) continue;
      if (SELECTOR_ATTRS.has(k)) {
        this.selector(v, ctx, extractor);
      } else if (k === "BrowserURL") {
        const c = classifyAttr(v);
        if (c !== null) this.valueEdge("targets_url", c, ctx, extractor, "BrowserURL", hostOr);
      }
      if (SECRET_ATTRS.has(k)) {
        const c = classifyAttr(v);
        if (c !== null && c.isStatic && c.static.trim()) {
          this.edge("hardcoded_secret", redacted(c.static), ctx, `${L}.${k}`.toLowerCase(), `literal value in ${L}.${k}`, true, extractor, "high");
        }
      }
      if (FILE_ATTRS.has(k) && !(L in OFFICE) && L !== "TargetApp") {
        const c = classifyAttr(v);
        if (c !== null && c.isStatic && FILE_LIKE_RE.test(c.static)) {
          this.edge("uses_file", c.static, ctx, c.static.toLowerCase(), `${L}.${k}`, true, extractor, "medium");
        }
      }
      if (SECRET_ATTRS.has(k)) continue; // a secret's value is never scanned for URLs or other facts
      if (v.includes("[") || v.includes("Config") || v.toLowerCase().includes("http")) this.scanText(v, ctx, extractor, `${L}.${k}`);
    }
    if (EXPR_NODES.has(L) && n.text.trim()) this.scanText(n.text, ctx, extractor);
  }

  folderOf(n) {
    const v = firstProp(n, "FolderPath", "OrchestratorFolderPath");
    if (v === null) return null;
    return "folder=" + (v.isStatic ? v.static : DYN);
  }

  targetApp(n, ctx, extractor) {
    const fp = prop(n, "FilePath");
    const bt = n.attrs.BrowserType;
    if (fp !== null) {
      this.valueEdge("uses_application", fp, ctx, extractor, "TargetApp.FilePath", normApp);
    } else if (bt && bt.toLowerCase() in BROWSER_EXE) {
      const exe = BROWSER_EXE[bt.toLowerCase()];
      this.edge("uses_application", exe, ctx, exe, "TargetApp.BrowserType", true, extractor, "high");
    }
    const url = prop(n, "Url");
    if (url !== null) this.valueEdge("targets_url", url, ctx, extractor, "TargetApp.Url", hostOr);
    const title = n.attrs.Title;
    if (title && !title.startsWith("[")) this.edge("window_title", title, ctx, title.toLowerCase(), "TargetApp.Title", true, extractor, "medium");
  }

  selector(raw, ctx, extractor) {
    const c = classifyAttr(raw);
    const text = c !== null && c.isStatic ? c.static : raw;
    const top = parseSelector(text || "");
    if (!top) return;
    const [tag, attrs] = top;
    const app = attrs.app;
    if (app && !app.includes("{{") && app.trim() !== "*") {
      this.edge("uses_application", app, ctx, normApp(app), `selector <${tag}>`, true, extractor + ":selector", "medium");
    }
    const url = attrs.url;
    if (url && url.trim() !== "" && url.trim() !== "*") {
      this.edge("targets_url", url, ctx, normHost(url.replaceAll("*", "x")) || url.toLowerCase(), `selector <${tag}>`,
        !url.includes("{{"), extractor + ":selector", "medium");
    }
    if (["sap", "java", "uia", "wnd", "html", "webctrl", "ctrl", "citrix", "rdp"].includes(tag)) {
      this.edge("ui_technology", tag, ctx, tag, null, true, extractor + ":selector", "medium");
    }
  }

  database(n, ctx, extractor) {
    const prov = n.attrs.ProviderName;
    const cs = prop(n, "ConnectionString");
    if (cs === null && !prov) return;
    if (cs !== null && cs.isStatic) {
      const parts = {};
      for (const kv of cs.static.split(";")) {
        const i = kv.indexOf("=");
        if (i >= 0) parts[kv.slice(0, i).trim().toLowerCase()] = kv.slice(i + 1).trim();
      }
      const val = dbTarget(cs.static) || "?";
      this.edge("uses_database", val, ctx, val.toLowerCase(), prov, true, extractor, "high");
      for (const key of ["password", "pwd"]) {
        if (parts[key]) {
          this.edge("hardcoded_secret", redacted(parts[key]), ctx, "connectionstring.password", "password in literal connection string", true, extractor, "high");
        }
      }
    } else {
      this.valueEdge("uses_database", cs, ctx, extractor, prov ?? null);
    }
  }

  scanText(text, ctx, extractor, attr = null) {
    if (!text) return;
    for (let u of findall(URL_RE, text)) {
      u = u.replace(/[).,;'"]+$/, "");
      const ul = u.toLowerCase();
      if (["http://schemas.", "https://schemas.", "http://www.w3.org", "http://schemas.microsoft.com", "https://docs.uipath.com"].some((p) => ul.startsWith(p))) continue;
      this.edge("targets_url", u, ctx, normHost(u) || ul, attr ? `literal in ${attr}` : "literal", true, extractor + ":literal", "medium");
    }
    for (const k of findall(CONFIG_REF, text)) this.edge("uses_config_key", k, ctx, k.toLowerCase(), attr, false, extractor, "high");
    for (const [rx, label] of SECRET_IN_EXPR) {
      for (const m of text.matchAll(rx)) {
        const val = m[1];
        if (val.startsWith("{") || val.startsWith("[") || ["password", "pwd"].includes(val.toLowerCase())) continue;
        this.edge("hardcoded_secret", redacted(val), ctx, label.toLowerCase(), label, true, extractor, "medium");
      }
    }
  }

  cs(rel, data) {
    const text = new TextDecoder("utf-8").decode(data);
    const lines = splitlines(text);
    lines.forEach((ln, idx) => {
      const i = idx + 1;
      const ctx = { file: rel, line: i, activity: null, activity_type: "coded" };
      for (const [rx, kind] of CS_PATTERNS) {
        for (const m of ln.matchAll(rx)) {
          if (kind.startsWith("uses_application:")) {
            const app = kind.split(":")[1];
            this.edge("uses_application", app, ctx, app, "coded workflow call", true, "cs", "medium");
            continue;
          }
          const value = m[1];
          const norm = kind === "targets_url" ? normHost(value) : value.toLowerCase();
          this.edge(kind, value, ctx, norm, "coded workflow", true, "cs", "medium");
        }
      }
      for (const m of ln.matchAll(CS_INVOKE)) this.invocations.push([this.pkg, this.ver, rel, i, null, "invoke_workflow", m[1], null, 1, "cs"]);
      for (const m of ln.matchAll(CS_DESCRIPTOR)) {
        const d = `${m[1]}.${m[2]}`;
        this.edge("uses_or_descriptor", d, ctx, d.toLowerCase(), "Object Repository descriptor", true, "cs", "medium");
      }
      this.scanText(ln, ctx, "cs");
    });
    this.workflows.push([this.pkg, this.ver, rel, "cs", lines.length, null, null, sha256(data)]);
  }

  objectRepository(files) {
    const names = new Map();
    for (const [rel, data] of files) {
      if (rel.endsWith("/.metadata")) {
        try {
          const meta = jsonLoads(data);
          names.set(rel.slice(0, -"/.metadata".length), [meta.Name, meta.Type]);
        } catch { /* unreadable metadata */ }
      }
    }
    for (const [rel, data] of files) {
      if (!rel.endsWith("/.content") || !rel.includes("/.data/")) continue;
      const owner = rel.split("/.data/")[0];
      const chain = [];
      let cur = owner;
      while (cur.includes("/")) {
        const nm = names.get(cur);
        if (nm && nm[0] && nm[1] !== "Library" && nm[1] !== "AppVersion") chain.push(nm[0]);
        cur = cur.slice(0, cur.lastIndexOf("/"));
      }
      const label = chain.length ? "OR: " + chain.reverse().join(" / ") : "OR";
      let root;
      try {
        root = parseXml(data);
      } catch {
        continue;
      }
      this.walk(root, rel, "object_repository", label);
    }
  }

  /** Last resort for packages without sources: UTF-16 strings from the compiled
   * assembly. Low confidence by design: a string in a DLL is not proof of use. */
  dllStrings(rel, data, projectName = null) {
    const ctx = { file: rel, line: null, activity: null, activity_type: "compiled" };
    const latin = data.toString("latin1");
    const found = new Set();
    for (const m of latin.matchAll(/(?:[\x20-\x7e]\x00){4,}/g)) {
      let s = "";
      for (let i = 0; i < m[0].length; i += 2) s += m[0][i];
      found.add(s);
    }
    const strings = [...found].sort();
    const ns = (projectName || "").replace(/[^A-Za-z0-9_]/g, "_") + ".";
    const wfRe = new RegExp(`^[A-Za-z_][${W}.]*$`, "u");
    for (const s of strings) {
      for (const u of findall(URL_RE, s)) {
        if (u.includes("schemas.") || u.includes("w3.org")) continue;
        this.edge("targets_url", u, ctx, normHost(u) || u.toLowerCase(), "string in compiled assembly", true, "dll_strings", "low");
      }
      if (s.includes("app=") && s.includes("<")) {
        const top = parseSelector(s);
        if (top && top[1].app) {
          this.edge("uses_application", top[1].app, ctx, normApp(top[1].app), "selector string in compiled assembly", true, "dll_strings", "low");
        }
      }
      if (/^\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_\-{}.]+)+$/.test(s)) {
        this.edge("calls_api", s, ctx, s.toLowerCase(), "relative path string in compiled assembly", true, "dll_strings", "low");
      }
      if (projectName && s.startsWith(ns) && wfRe.test(s)) {
        const wf = s.slice(ns.length).replaceAll(".", "/") + " (compiled)";
        if (!this.workflows.some((w) => w[2] === wf)) this.workflows.push([this.pkg, this.ver, wf, "compiled", null, null, null, null]);
      }
    }
    // Exact matches against Orchestrator resource names known from the crawl.
    for (const [kind, table] of [["reads_asset", "orch_asset"], ["uses_queue", "orch_queue"], ["uses_bucket", "orch_bucket"], ["invokes_process", "process"]]) {
      for (const [name] of this.db.rows(`SELECT DISTINCT name FROM ${table} WHERE length(name) >= 4`)) {
        if (found.has(name) && ![this.pkg.toLowerCase(), (projectName || "").toLowerCase()].includes(name.toLowerCase())) {
          this.edge(kind, name, ctx, name.toLowerCase(), "name of an Orchestrator object found as a string in the compiled assembly (not proof of use)",
            true, "dll_strings_match", "low");
        }
      }
    }
  }

  /** Trace dynamic values that are a bare identifier (in_QueueName, QueueName) through
   * single assignments in the same file and through the arguments each caller passes,
   * up to three hops. Static and Config-backed results become new edges (medium
   * confidence); a value read from an asset is annotated on the dynamic edge. */
  resolveArguments(filesLower) {
    const callers = new Map();
    for (const [caller, target, key, val, cctx] of this.argBindings) {
      const dest = resolveWorkflowPath(target, filesLower, caller);
      if (dest) {
        const k = dest + "\u0000" + key.toLowerCase();
        if (!callers.has(k)) callers.set(k, []);
        callers.get(k).push([caller, val, cctx]);
      }
    }
    const identOf = (text) => {
      const t = (text || "").trim().replace(/\.ToString(\(\))?$/, "");
      return IDENT_RE.test(t) ? t : null;
    };
    // beforeLine: only assignments earlier in the file than the read count (a later Assign cannot supply it).
    const trace = (ident, file, depth, hint, beforeLine) => {
      let out = [];
      if (depth > 3) return out;
      for (const src of this.varSources.get(file + "\u0000" + ident.toLowerCase()) || []) {
        const later = beforeLine !== null && beforeLine !== undefined && src[2] !== null && src[2] !== undefined && src[2] >= beforeLine;
        const res = outcome(src[0], src[1], file, depth, hint + ` <- ${ident} assigned at ${file}:${S(src[2])}`, src[2]);
        // An assignment later in the file may still run first (loops, flowcharts): keep it as a hint only.
        out = out.concat(later ? res.map(([k, v, h]) => [k === "value" ? "later" : k, v, h]) : res);
      }
      for (const [caller, val, cctx] of callers.get(file + "\u0000" + ident.toLowerCase()) || []) {
        out = out.concat(outcome("val", val, caller, depth, hint + ` <- argument ${ident} from ${caller}:${S(cctx.line)}`, cctx.line));
      }
      return out;
    };
    const outcome = (kind, src, file, depth, hint, atLine) => {
      if (kind === "asset") return [["asset", src, hint]];
      const val = src;
      if (val.isStatic) return [["value", val.static, hint]];
      let res = [];
      const whole = wholeConfigKey(val.expr);
      for (const k of whole ? [whole] : []) {
        const hit = this.config.get(k.toLowerCase());
        if (hit && hit[0] && !hit[2]) {
          if (hit[3]) res.push(["asset", hit[0], hint + ` = Config '${k}' (Assets sheet)`]);
          else res.push(["value", hit[0], hint + ` = Config '${k}' (${hit[1]})`]);
        }
      }
      if (!res.length && !whole) {
        const keys = findall(CONFIG_REF, val.expr);
        for (const k of keys) {
          const hit = this.config.get(k.toLowerCase());
          if (hit && hit[0] && !hit[2] && !hit[3]) res.push(["partial", `Config '${k}' (= '${cut(hit[0], 200)}')`, hint]);
        }
      }
      if (!res.length) {
        const nxt = identOf(val.expr);
        if (nxt) res = trace(nxt, file, depth + 1, hint, atLine);
      }
      return res;
    };
    const snapshot = this.edges.slice();
    snapshot.forEach((e, i) => {
      if (e[4] !== DYN) return;
      const ident = identOf(e[3]);
      if (!ident) return;
      const ctx = { file: e[7], line: e[8], activity: e[9], activity_type: e[10] };
      const notes = [];
      const folderPart = (/(?:^|; )(folder=[^;]+)/.exec(e[5] || "") || [])[1];
      const results = trace(ident, e[7], 1, "traced", e[8]);
      // Several different values assigned earlier in the file: which one reaches the read is not proven.
      const assigned = new Set(results.filter((r) => r[0] === "value" && / assigned at /.test(r[2].split(" <- ")[1] || "")).map((r) => r[1]));
      for (const [kind, value, hint] of results) {
        const conf = assigned.size > 1 && assigned.has(value) ? "low" : "medium";
        const det = cut(hint, 900) + (folderPart ? "; " + folderPart : "") + (conf === "low" ? "; one of several earlier assignments" : "");
        if (kind === "value") this.edge(e[2], value, ctx, value.toLowerCase(), det, false, e[11] + "+trace", conf);
        else if (kind === "partial") notes.push(`built at runtime from ${value} and other values (${hint})`);
        else if (kind === "later") notes.push(`possible value '${cut(value, 200)}' from an assignment later in the file (${hint}); execution order not proven`);
        else notes.push(`value read at runtime from asset '${value}' (${hint})`);
      }
      if (notes.length) this.edges[i] = [...e.slice(0, 5), cut(notes.join("; "), 1000), ...e.slice(6)];
    });
  }

  flush() {
    for (const e of this.edges) this.db.run("INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ...e);
    for (const i of this.invocations) this.db.run("INSERT INTO invocation VALUES (?,?,?,?,?,?,?,?,?,?)", ...i);
    for (const w of this.workflows) this.db.run("INSERT INTO workflow VALUES (?,?,?,?,?,?,?,?)", ...w);
  }
}

const CS_PATTERNS = [
  [/\bGetAsset\s*\(\s*"([^"]+)"/g, "reads_asset"],
  [/\bGetRobotAsset\s*\(\s*"([^"]+)"/g, "reads_asset"],
  [/\bSetAsset\s*\(\s*"([^"]+)"/g, "writes_asset"],
  [/\bGet(?:Robot)?Credential\s*\(\s*"([^"]+)"/g, "reads_credential"],
  [/\bSetCredential\s*\(\s*"([^"]+)"/g, "writes_credential"],
  [/\b(?:AddQueueItem|AddTransactionItem|BulkAddQueueItems|GetTransactionItem|GetQueueItems?)\s*\(\s*"([^"]+)"/g, "uses_queue"],
  [/\b(?:RunJob|StartJob|InvokeProcess)\s*\(\s*"([^"]+)"/g, "invokes_process"],
  [/\b(?:UploadStorageFile|DownloadStorageFile|ReadStorageText|WriteStorageText|ListStorageFiles)\s*\(\s*"([^"]+)"/g, "uses_bucket"],
  [/\bOpen(?:Browser)?\s*\(\s*"(https?:\/\/[^"]+)"/g, "targets_url"],
  [/\b(?:ExcelUse|UseExcelFile|ExcelOpen)\s*\(/g, "uses_application:excel.exe"],
];
const CS_INVOKE = /\b(?:RunWorkflow|InvokeWorkflow)\s*\(\s*"([^"]+)"/g;
const CS_DESCRIPTOR = new RegExp(`\\bDescriptors\\.([A-Za-z_][${W}]*)\\.([A-Za-z_][${W}]*)`, "gu");

// ------------------------------------------------------------ Config.xlsx reader

/** Minimal .xlsx reader: Map sheet -> rows of text cells. */
function readXlsx(data) {
  const out = new Map();
  const z = new Zip(data);
  const names = new Set(z.names());
  const shared = [];
  if (names.has("xl/sharedStrings.xml")) {
    const root = parseXml(z.read("xl/sharedStrings.xml"));
    for (const si of root.children) {
      if (si.local === "si") shared.push([...iterNodes(si)].filter((t) => t.local === "t").map((t) => t.text).join(""));
    }
  }
  const rels = {};
  if (names.has("xl/_rels/workbook.xml.rels")) {
    for (const r of iterNodes(parseXml(z.read("xl/_rels/workbook.xml.rels")))) {
      if (r.local === "Relationship") rels[r.attrs.Id] = r.attrs.Target ?? "";
    }
  }
  const wb = parseXml(z.read("xl/workbook.xml"));
  for (const s of iterNodes(wb)) {
    if (s.local !== "sheet") continue;
    const target = rels[s.attrs.id] ?? "";
    const p = target.startsWith("/") ? target.replace(/^\/+/, "") : normpath("xl/" + target);
    if (!names.has(p)) continue;
    const rows = [];
    let cellCount = 0;
    for (const row of iterNodes(parseXml(z.read(p), { maxNodes: MAX_SHEET_CELLS * 4 }))) {
      if (row.local !== "row") continue;
      const cells = new Map();
      for (const c of row.children) {
        if (c.local !== "c") continue;
        const ref = c.attrs.r ?? "";
        let col = 0;
        for (const ch of /^[A-Z]*/.exec(ref)[0]) col = col * 26 + (ch.charCodeAt(0) - 64);
        if (col > MAX_SHEET_COLUMNS) continue;
        if (++cellCount > MAX_SHEET_CELLS) throw new Error(`size limit: more than ${MAX_SHEET_CELLS} cells in ${p}`);
        const t = c.attrs.t;
        const vNode = c.children.find((x) => x.local === "v");
        let v = vNode ? vNode.text : "";
        if (t === "s" && /^\d+$/.test(v.trim()) && parseInt(v, 10) < shared.length) v = shared[parseInt(v, 10)];
        else if (t === "inlineStr") v = [...iterNodes(c)].filter((x) => x.local === "t").map((x) => x.text).join("");
        cells.set(Math.max(col - 1, 0), v);
      }
      if (cells.size) {
        const max = Math.max(...cells.keys());
        rows.push(Array.from({ length: max + 1 }, (_, i) => cells.get(i) ?? ""));
      }
    }
    out.set(s.attrs.name ?? p, rows);
  }
  return out;
}

function loadConfig(db, pkg, ver, rel, data, configValues) {
  let sheets;
  try {
    sheets = readXlsx(data);
  } catch (e) {
    coverage(db, pkg, ver, "config:" + rel, "parse_error", String(e.message));
    return [[], []];
  }
  const assetRows = [];
  const secretRows = [];
  for (const [sheet, rows] of sheets) {
    if (!rows.length) continue;
    const header = rows[0].map((h) => h.trim().toLowerCase());
    if (!header.includes("name")) continue;
    const ni = header.indexOf("name");
    const vh = ["value", "asset", "asset name"].find((h) => header.includes(h));
    const vi = vh !== undefined ? header.indexOf(vh) : null;
    const fh = ["folder", "assetfolder", "orchestratorassetfolder", "asset folder"].find((h) => header.includes(h));
    const fi = fh !== undefined ? header.indexOf(fh) : null;
    const isAssetSheet = sheet.trim().toLowerCase() === "assets" || (vi !== null && header[vi].startsWith("asset"));
    for (const r of rows.slice(1)) {
      const name = (ni < r.length ? r[ni] : "").trim();
      if (!name) continue;
      const value = (vi !== null && vi < r.length ? r[vi] : "").trim();
      const folder = (fi !== null && fi < r.length ? r[fi] : "").trim() || null;
      const secret = !isAssetSheet && isSecretName(name) && !!value;
      const stored = secret ? redacted(value) : scrub(value);
      db.run("INSERT INTO config_key VALUES (?,?,?,?,?,?,?,?)", pkg, ver, rel, sheet, name, stored, folder, secret ? 1 : 0);
      CRED_SEGMENT.lastIndex = 0;
      if (secret || (value && new RegExp(CRED_SEGMENT.source, "i").test(value))) secretRows.push([sheet, name, Array.from(value).length]);
      configValues.set(name.toLowerCase(), [secret ? null : value, `${sheet} sheet`, secret, isAssetSheet]);
      if (isAssetSheet && value) assetRows.push([name, value, folder, sheet]);
    }
  }
  return [assetRows, secretRows];
}

// ------------------------------------------------------------------- package input

function readNuspec(z) {
  for (const e of z.entries) {
    if (e.name.endsWith(".nuspec") && !e.name.includes("/")) {
      const root = parseXml(z.read(e));
      const vals = {};
      for (const x of iterNodes(root)) if (["id", "version", "description"].includes(x.local)) vals[x.local] = x.text.trim();
      return [vals.id ?? null, vals.version ?? null];
    }
  }
  return [null, null];
}

/** content/ for Windows/cross-platform packages, lib/net45/ for legacy. */
function projectRootInZip(names) {
  let cands = names.filter((n) => /(^|\/)project\.json$/i.test(n)).map((n) => n.slice(0, -"project.json".length));
  for (const pref of ["content/", "lib/net45/"]) if (cands.includes(pref)) return pref;
  cands = cands.filter((c) => (c.match(/\//g) || []).length <= 3);
  return cands.length ? [...cands].sort((a, b) => a.length - b.length)[0] : null;
}

function safeRel(p) {
  let r = unquote(p).replaceAll("\\", "/");
  r = normpath(r);
  if (r.startsWith("../") || r.startsWith("/") || r === ".." || /^[A-Za-z]:/.test(r)) return null;
  return r;
}

const SKIP_DIRS_IN_PROJECT = new Set([".local", ".git", ".tmh", "node_modules", ".screenshots"]);

/** Project tree as Map(rel -> Buffer), plus metadata. */
function openPackage(p) {
  if (fs.statSync(p).isDirectory()) {
    const files = new Map();
    const skipped = [];
    let total = 0;
    const realRoot = fs.realpathSync(p);
    const inside = (full) => {
      // Never follow a link out of the project: a hostile repo could point at ~/.ssh or /etc.
      try {
        const r = fs.realpathSync(full);
        return r === realRoot || r.startsWith(realRoot + path.sep);
      } catch {
        return false;
      }
    };
    const walk = (dir) => {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      const subdirs = [];
      for (const ent of entries) {
        const full = path.join(dir, ent.name);
        if (ent.isSymbolicLink() && !inside(full)) continue;
        let st;
        try { st = fs.statSync(full); } catch { continue; } // broken symlink
        if (st.isDirectory()) {
          if (ent.isSymbolicLink()) continue; // like os.walk(followlinks=False)
          // a sub-folder with its own project.json is a separate project, indexed on its own
          if (SKIP_DIRS_IN_PROJECT.has(ent.name) || hasProjectJson(full) || isExcluded(full)) continue;
          subdirs.push(full);
        } else if (st.isFile()) {
          const rel = path.relative(p, full).split(path.sep).join("/");
          if (st.size > MAX_FILE_BYTES) { skipped.push(rel); continue; }
          total += st.size;
          if (total > MAX_PACKAGE_BYTES) throw new Error(`size limit: project files exceed ${MAX_PACKAGE_BYTES} bytes`);
          files.set(rel, fs.readFileSync(full));
        }
      }
      for (const d of subdirs) walk(d);
    };
    walk(p);
    return [files, { layout: "source", dlls: new Map(), skipped }];
  }
  const z = new Zip(fs.readFileSync(p));
  const names = z.names();
  const root = projectRootInZip(names);
  const files = new Map();
  const dlls = new Map();
  const skipped = [];
  for (const e of z.entries) {
    const n = e.name;
    if (e.isDir) continue;
    if (e.usize > MAX_FILE_BYTES) { skipped.push(n); continue; }
    if (n.toLowerCase().endsWith(".dll") && n.startsWith("lib/")) {
      dlls.set(n, z.read(e));
      continue;
    }
    if (root === null || !n.startsWith(root)) continue;
    const rel = safeRel(n.slice(root.length));
    if (rel) files.set(rel, z.read(e));
  }
  const layout = root === null ? "unknown" : ({ "content/": "modern", "lib/net45/": "legacy" }[root] || "other");
  const [pid, pver] = readNuspec(z);
  return [files, { layout, dlls, nuspec_id: pid, nuspec_version: pver, skipped }];
}

function writeTree(indexDir, pkg, ver, files) {
  const dest = path.join(indexSubdir(indexDir, "src"), srcDirName(pkg, ver));
  removeEntry(dest);
  for (const [rel, data] of files) {
    if (/\.(png|jpg|jpeg|bmp|gif)$/i.test(rel)) continue;
    const full = path.join(dest, ...rel.split("/"));
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, data);
  }
  return dest;
}

function deletePackageRows(db, pkg, ver) {
  for (const t of ["edge", "invocation", "workflow", "dependency", "entry_point", "config_key", "binding", "analyzer_run", "analyzer_finding", "finding"]) {
    db.run(`DELETE FROM ${t} WHERE package_id=? AND version=?`, pkg, ver);
  }
  db.run("DELETE FROM coverage WHERE package_id=? AND version=? AND stage LIKE 'config:%'", pkg, ver);
}

/** Studio resolves WorkflowFileName against the project root (in real projects, every invoke from a
 * sub-folder does). A path that leaves the project is unresolved, never guessed. */
/** The same origin (folder or file) indexed earlier under another name or version: drop those rows. */
function forgetOtherIdentities(db, indexDir, p, origin, pkg, ver) {
  for (const [oid, over] of db.rows("SELECT package_id, version FROM package WHERE file_path=? AND origin=? AND NOT (package_id=? AND version=?)", p, origin, pkg, ver)) {
    deletePackageRows(db, oid, over);
    db.run("DELETE FROM package WHERE package_id=? AND version=?", oid, over);
    db.run("DELETE FROM coverage WHERE package_id=? AND version=?", oid, over);
    removeEntry(path.join(indexDir, "src", srcDirName(oid, over)));
  }
}

function resolveWorkflowPath(target, filesLower) {
  const r = normpath(target.replaceAll("\\", "/").trim().replace(/^\/+/, ""));
  if (r === "." || r === ".." || r.startsWith("../")) return null;
  return filesLower.get(r.toLowerCase()) ?? null;
}

const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sortedKeys = (m) => [...m.keys()].sort(cmpStr);

/** Python str() of a JSON scalar. */
function pyScalar(v) {
  if (typeof v === "boolean") return v ? "True" : "False";
  return String(v);
}

function extractOne(db, indexDir, p, kind = "process", origin = "download", force = false, label = null, solution = null) {
  const [files, info] = openPackage(p);
  const isDir = fs.statSync(p).isDirectory();
  const pjKey = [...files.keys()].find((k) => k.toLowerCase() === "project.json");
  const pjRaw = pjKey === undefined ? undefined : files.get(pjKey);
  if (pjRaw === undefined) {
    const pid = info.nuspec_id || path.basename(p);
    let ver = info.nuspec_version || "unknown";
    if (isDir) ver = label || "source";
    deletePackageRows(db, pid, ver);
    forgetOtherIdentities(db, indexDir, p, origin, pid, ver);
    db.run("INSERT OR REPLACE INTO package (package_id, version, kind, origin, file_path, layout, has_sources, extractor_version, indexed_at, solution) " +
      "VALUES (?,?,?,?,?,?,?,?,?,?)", pid, ver, kind, origin, p, info.layout, 0, EXTRACTOR_VERSION, now(), solution);
    coverage(db, pid, ver, "extract", "unsupported", "no project.json found (agent, flow, app or other package type)");
    return [pid, ver];
  }
  let pj;
  let pjError = null;
  try {
    pj = jsonLoads(pjRaw);
  } catch (e) {
    pj = {};
    pjError = `project.json unreadable: ${e.message}`;
    log(`  ${pjError} (${p})`);
  }
  if (!isObject(pj)) throw new TypeError(`project.json is not an object in ${p}`);
  let pkg, ver, digest;
  if (isDir) {
    const known = pjError ? db.one("SELECT package_id FROM package WHERE file_path=? AND origin=? LIMIT 1", p, origin) : null;
    pkg = pj.name || (known && known[0]) || path.basename(path.resolve(p));
    ver = label || "source";
    const h = crypto.createHash("sha256");
    for (const k of sortedKeys(files)) {
      h.update(Buffer.from(k, "utf8"));
      h.update(Buffer.from([0]));
      h.update(crypto.createHash("sha256").update(files.get(k)).digest());
    }
    digest = h.digest("hex");
  } else {
    pkg = info.nuspec_id || pj.name;
    ver = info.nuspec_version || pj.projectVersion || "unknown";
    digest = sha256(fs.readFileSync(p));
  }
  const prev = db.one("SELECT sha256, extractor_version FROM package WHERE package_id=? AND version=?", pkg, ver);
  if (prev && prev[0] === digest && prev[1] === EXTRACTOR_VERSION && !force) return [pkg, ver];
  deletePackageRows(db, pkg, ver);
  forgetOtherIdentities(db, indexDir, p, origin, pkg, ver);

  const sources = [...files.keys()].filter((r) => /\.(xaml|cs)$/i.test(r) && !r.startsWith(".local/") && !r.startsWith(".tmh/") && !r.includes("/.local/"));
  const hasSources = sources.length ? 1 : 0;
  const design = isObject(pj.designOptions) ? pj.designOptions : {};
  db.run("INSERT OR REPLACE INTO package VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    pkg, ver, kind, origin, p, digest, hasSources ? info.layout : "compiled", hasSources, pj.name ?? null, pj.description ?? null,
    design.outputType || (kind === "library" ? "Library" : null),
    pj.targetFramework || (info.layout === "legacy" ? "Legacy" : null),
    pj.expressionLanguage ?? null, pj.studioVersion ?? null, pj.main ?? null, EXTRACTOR_VERSION, now(), solution);

  const deps = isObject(pj.dependencies) ? pj.dependencies : {};
  for (const [n, s] of Object.entries(deps)) {
    db.run("INSERT INTO dependency VALUES (?,?,?,?,?,?)", pkg, ver, n, String(s), resolveVersion(String(s)), isActivityPack(n) ? 1 : 0);
  }

  const entries = new Map();
  const addEntry = (pth, src) => entries.set(pth + "\u0000" + src, [pth, src]);
  if (pj.main) addEntry(String(pj.main).replaceAll("\\", "/"), "project.json main");
  for (const e of Array.isArray(pj.entryPoints) ? pj.entryPoints : []) {
    if (isObject(e) && e.filePath) addEntry(e.filePath.replaceAll("\\", "/"), "project.json entryPoints");
  }
  const epRaw = files.get("entry-points.json");
  if (epRaw) {
    try {
      for (const e of jsonLoads(epRaw).entryPoints || []) {
        if (e.filePath) addEntry(e.filePath.replaceAll("\\", "/").replace(/^\/+/, ""), "entry-points.json");
      }
    } catch { /* unreadable entry points */ }
  }
  for (const fi of Array.isArray(design.fileInfoCollection) ? design.fileInfoCollection : []) {
    if (isObject(fi) && fi.fileName) addEntry(fi.fileName.replaceAll("\\", "/"), "test case");
  }
  const entryList = [...entries.values()].sort((a, b) => cmpStr(a[0], b[0]) || cmpStr(a[1], b[1]));
  for (const [pth, src] of entryList) db.run("INSERT INTO entry_point VALUES (?,?,?,?)", pkg, ver, pth, src);

  // Config files first so activities can resolve Config("Key") references.
  const configValues = new Map();
  const assetRows = [];
  const configSecrets = [];
  for (const rel of sortedKeys(files)) {
    const low = rel.toLowerCase();
    if (low.endsWith(".xlsx") && posixBasename(low).includes("config")) {
      const [assets, secrets] = loadConfig(db, pkg, ver, rel, files.get(rel), configValues);
      for (const a of assets) assetRows.push([rel, ...a]);
      for (const x of secrets) configSecrets.push([rel, ...x]);
    } else if (low.endsWith(".json") && posixBasename(low).includes("config") && !low.startsWith(".")) {
      try {
        const cfg = jsonLoads(files.get(rel));
        const flat = isObject(cfg) ? cfg : {};
        const rows = [];
        for (const [k, v] of Object.entries(flat)) {
          if (["string", "number", "boolean"].includes(typeof v)) {
            const secret = isSecretName(k);
            rows.push([pkg, ver, rel, "json", k, secret ? redacted(pyScalar(v)) : scrub(pyScalar(v)), null, secret ? 1 : 0]);
            configValues.set(k.toLowerCase(), [secret ? null : pyScalar(v), "json config", secret, false]);
          }
        }
        for (const r of rows) db.run("INSERT INTO config_key VALUES (?,?,?,?,?,?,?,?)", ...r);
      } catch { /* unreadable config */ }
    }
  }

  const ex = new PackageExtractor(db, pkg, ver, p, configValues);
  for (const [rel, sheet, name, length] of configSecrets) {
    ex.edge("hardcoded_secret", `<redacted len=${length}>`, { file: rel, line: null, activity: `${sheet}!${name}`, activity_type: "config" },
      `config.${name}`.toLowerCase(), `credential-like value in Config ${sheet} sheet key '${name}'`, true, "config_file", "medium");
  }
  for (const [rel, name, value, folder, sheet] of assetRows) {
    ex.edge("reads_asset", value, { file: rel, line: null, activity: `${sheet}!${name}`, activity_type: "config" },
      value.toLowerCase(), `Config ${sheet} sheet key '${name}'` + (folder ? `; folder=${folder}` : ""), true, "config_assets_sheet", "medium");
  }
  for (const rel of [...sources].sort(cmpStr)) {
    if (rel.toLowerCase().endsWith(".xaml")) ex.xaml(rel, files.get(rel));
    else ex.cs(rel, files.get(rel));
  }
  ex.objectRepository([...files].filter(([r]) => r.startsWith(".objects/")));

  // Studio's own application metadata (what Orchestrator shows as "used applications").
  const am = files.get("applicationsMetadata.json") || files.get(".local/content/applicationsMetadata.json");
  if (am) {
    try {
      for (const m of jsonLoads(am).Metadata || []) {
        const ctx = { file: "applicationsMetadata.json", line: null, activity: null, activity_type: "studio" };
        const exe = m.ExecutablePath || {};
        if (exe.Value) ex.edge("uses_application", exe.Value, ctx, normApp(exe.Value), m.ApplicationType ?? null, !exe.IsExpression, "studio_applications_metadata", "high");
        const url = m.Url || {};
        if (url.Value) ex.edge("targets_url", url.Value, ctx, normHost(url.Value) || url.Value.toLowerCase(), m.ApplicationType ?? null, !url.IsExpression, "studio_applications_metadata", "high");
      }
    } catch { /* unreadable metadata */ }
  }

  // Package bindings (present even in compiled-only packages).
  const kindMap = { asset: "reads_asset", queue: "uses_queue", process: "invokes_process", bucket: "uses_bucket",
    connection: "uses_connection", eventtrigger: "uses_connection", app: "uses_app", index: "uses_context_index" };
  for (const bname of ["bindings_v2.json", "bindings.json", ".local/content/bindings_v2.json"]) {
    const b = files.get(bname);
    if (!b) continue;
    let doc;
    try {
      doc = jsonLoads(b);
    } catch {
      continue;
    }
    if (!isObject(doc)) throw new TypeError(`${bname} is not an object`);
    for (const r of doc.resources || []) {
      if (!isObject(r)) continue;
      const res = String(r.resource || "").toLowerCase();
      const val = isObject(r.value) ? r.value : {};
      const meta = isObject(r.metadata) ? r.metadata : {};
      const dv = (k) => (isObject(val[k]) ? val[k].defaultValue ?? null : null);
      const name = dv("name") || meta.Connector || r.key || null;
      const folder = dv("folderPath");
      const safeMeta = redactSecretKeys(meta);
      db.run("INSERT INTO binding VALUES (?,?,?,?,?,?,?,?)", pkg, ver, res, name, folder, r.key ?? null, r.workflowRelativePath ?? null, cut(pyDumps(safeMeta), 2000));
      if (res in kindMap && name) {
        const ctx = { file: bname, line: null, activity: meta.ActivityName ?? null, activity_type: "binding" };
        ex.edge(kindMap[res], name, ctx, String(name).toLowerCase(), folder ? `folder=${folder}` : null, true, "package_bindings", "high");
      }
    }
    break;
  }

  if (!hasSources) for (const [rel, data] of info.dlls) ex.dllStrings(rel, data, pj.name ?? null);

  const filesLower = new Map([...files.keys()].map((r) => [r.toLowerCase(), r]));
  ex.resolveArguments(filesLower);
  ex.flush();

  // Reachability from entry points over static invocations.
  const graph = new Map();
  const dynamicIn = new Set();
  for (const inv of ex.invocations) {
    const [, , frm, , , kind_, target, , stat] = inv;
    if (kind_ !== "invoke_workflow") continue;
    if (!stat) { dynamicIn.add(frm); continue; }
    const dest = resolveWorkflowPath(target, filesLower, frm);
    if (dest) {
      if (!graph.has(frm)) graph.set(frm, new Set());
      graph.get(frm).add(dest);
      db.run("UPDATE invocation SET target_path=? WHERE package_id=? AND version=? AND from_path=? AND target_raw=?", dest, pkg, ver, frm, target);
    }
  }
  const isLibrary = design.outputType === "Library" || kind === "library";
  const priv = new Set(((isObject(design.libraryOptions) && design.libraryOptions.privateWorkflows) || []).map((x) => String(x).replaceAll("\\", "/").toLowerCase()));
  const starts = new Set();
  for (const [pth] of entryList) {
    const hit = filesLower.get(pth.toLowerCase().replace(/^\/+/, ""));
    if (hit) starts.add(hit);
  }
  if (isLibrary) for (const r of sources) if (!priv.has(r.toLowerCase())) starts.add(r);
  const reach = new Set();
  const todo = [...starts];
  while (todo.length) {
    const cur = todo.pop();
    if (reach.has(cur)) continue;
    reach.add(cur);
    for (const nx of graph.get(cur) || []) todo.push(nx);
  }
  // Unknown, not dead, when something reachable could not be read or calls by a runtime value.
  const unreadable = new Set(ex.workflows.filter((w) => w[6]).map((w) => w[2]));
  const partial = !!pjError || [...dynamicIn].some((f) => reach.has(f)) || [...unreadable].some((f) => reach.has(f)) || starts.size === 0;
  for (const r of sources) {
    const flag = reach.has(r) ? 1 : (partial || r.toLowerCase().endsWith(".cs") ? null : 0);
    db.run("UPDATE workflow SET reachable=? WHERE package_id=? AND version=? AND path=?", flag, pkg, ver, r);
  }

  writeTree(indexDir, pkg, ver, files);
  const skippedNote = info.skipped && info.skipped.length
    ? `; ${info.skipped.length} file(s) over ${MAX_FILE_BYTES} bytes not read: ${info.skipped.slice(0, 5).join(", ")}${info.skipped.length > 5 ? ", ..." : ""}` : "";
  coverage(db, pkg, ver, "extract", pjError || skippedNote ? "partial" : hasSources ? "indexed" : "compiled_only",
    (pjError ? `${pjError}; dependencies, entry points and project settings unknown; ${sources.length} source files read`
      : hasSources ? `${sources.length} source files`
      : "no .xaml/.cs in package (published without sources); facts limited to project.json, bindings, applicationsMetadata and low-confidence DLL strings") + skippedNote);
  return [pkg, ver];
}

// -------------------------------------------------------------------------- crawl

class UipError extends Error {}

function which(cmd) {
  const exts = process.platform === "win32" ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    for (const ext of exts) {
      const full = path.join(dir, cmd + ext);
      try {
        if (fs.statSync(full).isFile()) return full;
      } catch { /* not here */ }
    }
  }
  return null;
}

class Uip {
  constructor(binary = null, profile = null, timeout = 300) {
    const bin = binary || which("uip");
    if (!bin) throw new Exit("uip CLI not found on PATH. Install it or pass --uip PATH.");
    this.profile = profile;
    this.timeout = timeout;
    // Never through a shell: arguments include names that come from Orchestrator data.
    if (/\.(c|m)?js$/i.test(bin)) {
      this.cmd = [process.execPath, bin];
    } else if (process.platform === "win32" && /\.(cmd|bat)$/i.test(bin)) {
      // npm shim: run node on the CLI script it points to (a .cmd file can only be run by a shell).
      const shim = fs.readFileSync(bin, "utf8");
      const m = /"%~?dp0%?\\([^"]+\.(?:c|m)?js)"/i.exec(shim);
      if (!m) throw new Exit(`Cannot find the CLI script inside ${bin}. Pass --uip with the path of the uip CLI's .js entry point.`);
      this.cmd = [process.execPath, path.join(path.dirname(bin), m[1])];
    } else {
      this.cmd = [bin];
    }
  }

  run(...args) {
    const full = [...args, "--output", "json", ...(this.profile ? ["--profile", this.profile] : [])];
    const r = spawnSync(this.cmd[0], [...this.cmd.slice(1), ...full], { encoding: "utf8", timeout: this.timeout * 1000, maxBuffer: 1 << 28 });
    const label = args.slice(0, 3).join(" ");
    if (r.error) throw new UipError(`${label}: ${r.error.code === "ETIMEDOUT" ? "timed out" : r.error.message}`);
    const out = (r.stdout || "").trim();
    let doc;
    try {
      doc = out ? JSON.parse(out) : {};
    } catch {
      throw new UipError(`${label}: non-JSON output: ${out.slice(0, 300)} ${(r.stderr || "").slice(-300)}`);
    }
    if (doc.Result !== "Success") throw new UipError(`${label}: ${doc.Message || doc.ErrorCode || (r.stderr || "").slice(-300)}`);
    return doc;
  }

  *paged(args, limit = null) {
    let offset = 0;
    let total = 0;
    for (;;) {
      const doc = this.run(...args, "--limit", String(PAGE), "--offset", String(offset));
      const items = doc.Data || [];
      for (const it of items) {
        yield it;
        total++;
        if (limit && total >= limit) return;
      }
      if (!(doc.Pagination || {}).HasMore || !items.length) return;
      offset += items.length;
    }
  }
}

function scalars(d, drop = ["Argument", "Password", "Secret", "Credential", "ConnectionString", "Value"]) {
  const keep = {};
  for (const [k, v] of Object.entries(d)) {
    if (v !== null && typeof v === "object") continue;
    if (drop.some((x) => k.toLowerCase().includes(x.toLowerCase())) && k !== "ValueType" && k !== "ValueScope") continue;
    keep[k] = v;
  }
  return cut(pyDumps(keep, true), 4000);
}

function cmdCrawl(a) {
  const db = connect(a.indexDir);
  const uip = new Uip(a.uip, a.profile);
  const st = uip.run("login", "status").Data || {};
  if (st.Status !== "Logged in") {
    throw new Exit("Not logged in. A human runs `uip login` (ideally with a read-only external app); this tool never logs in by itself.");
  }
  setMeta(db, "crawl_started", now());
  setMeta(db, "crawl_tenant", `${S(st.Organization)}/${S(st.Tenant)}`);
  db.commit();
  const errors = [];
  const attempt = (label, fn) => {
    try {
      return fn();
    } catch (e) {
      if (!(e instanceof UipError)) throw e;
      errors.push(`${label}: ${e.message}`);
      log(`  ! ${label}: ${e.message}`);
      return null;
    }
  };

  log("folders...");
  // Every inventory is replaced only when all of its list calls succeed; otherwise the previous rows
  // stay and meta.inventory_<type>_ok = 0 tells the review rules not to trust it.
  const folders = new Map();
  const folderList = attempt("folders list", () => [...uip.paged(["or", "folders", "list", "--all"])]);
  for (const f of folderList || []) {
    folders.set(String(f.Key || "").toLowerCase(), [f.Path || f.Name || null, f.Type ?? null, f.FeedType ?? null, "folders list"]);
  }

  log("processes...");
  const procs = attempt("processes list", () => [...uip.paged(["or", "processes", "list", "--all-folders", "--all-fields"])]);
  if (procs !== null) {
    db.run("DELETE FROM process");
    for (const p of procs) {
      const fk = String(p.FolderKey || "").toLowerCase();
      const fpath = p.OrganizationUnitFullyQualifiedName ?? null;
      if (fk && !folders.has(fk)) folders.set(fk, [fpath, p.OrganizationUnitType ?? null, null, "from process"]);
      db.run("INSERT OR REPLACE INTO process VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        String(p.Key || "").toUpperCase(), p.Name ?? null, fk, fpath, p.ProcessKey ?? null, p.ProcessVersion ?? null,
        p.ProcessType ?? null, p.TargetFramework ?? null, p.FeedId ?? null, p.IsLatestVersion ? 1 : 0, p.AutoUpdate ? 1 : 0,
        p.Description ?? null, pyDumps(p.Tags || []), p.LastModificationTime ?? null, p.CreationTime ?? null);
    }
  }
  setMeta(db, "inventory_process_ok", procs !== null ? "1" : "0");
  if (folderList === null) {
    // Folder discovery failed: keep every folder we knew, so their resources are still crawled and kept.
    for (const [k, p, t, ft, src] of db.rows("SELECT key, path, type, feed_type, source FROM folder")) if (!folders.has(k)) folders.set(k, [p, t, ft, src]);
  }
  db.run("DELETE FROM folder");
  for (const [k, v] of folders) db.run("INSERT OR REPLACE INTO folder VALUES (?,?,?,?,?)", k, ...v);
  db.commit();

  log(`per-folder resources across ${folders.size} folders...`);
  const staged = { asset: [], queue: [], bucket: [], trigger: [] };
  // Without a complete folder list, no per-folder inventory can be complete either.
  const ok = { asset: folderList !== null, queue: folderList !== null, bucket: folderList !== null, trigger: folderList !== null };
  const list = (type, label, args) => {
    const r = attempt(label, () => [...uip.paged(args)]);
    if (r === null) ok[type] = false;
    return r || [];
  };
  for (const [fk, [fpath]] of folders) {
    for (const x of list("asset", `assets list ${S(fpath)}`, ["or", "assets", "list", "--folder-key", fk])) {
      staged.asset.push([x.Key ?? null, fk, fpath, x.Name ?? null, x.ValueType ?? null, x.ValueScope ?? null, x.HasDefaultValue ? 1 : 0, x.Description ?? null]);
    }
    for (const x of list("queue", `queues list ${S(fpath)}`, ["or", "queues", "list", "--folder-key", fk])) staged.queue.push([x.Key ?? null, fk, fpath, x.Name ?? null, scalars(x)]);
    for (const x of list("bucket", `buckets list ${S(fpath)}`, ["or", "buckets", "list", "--folder-key", fk])) staged.bucket.push([x.Key ?? null, fk, fpath, x.Name ?? null, scalars(x)]);
    for (const tt of ["time", "queue", "api"]) {
      for (const x of list("trigger", `triggers list ${tt} ${S(fpath)}`, ["or", "triggers", "list", "--type", tt, "--folder-key", fk, "--all-fields"])) {
        staged.trigger.push([x.Key || S(x.Id), tt, fk, fpath, x.Name ?? null, x.ReleaseName || x.ProcessName || null, x.ReleaseKey || x.ProcessKey || null, x.Enabled ? 1 : 0, scalars(x)]);
      }
    }
  }
  const tables = { asset: ["orch_asset", 8], queue: ["orch_queue", 5], bucket: ["orch_bucket", 5], trigger: ["orch_trigger", 9] };
  for (const [type, [table, n]] of Object.entries(tables)) {
    if (ok[type]) {
      db.run(`DELETE FROM ${table}`);
      for (const row of staged[type]) db.run(`INSERT OR REPLACE INTO ${table} VALUES (${Array(n).fill("?").join(",")})`, ...row);
    }
    setMeta(db, `inventory_${type}_ok`, ok[type] ? "1" : "0");
  }
  db.commit();

  log("machines...");
  const machines = attempt("machines list", () => [...uip.paged(["or", "machines", "list"])]);
  if (machines !== null) {
    db.run("DELETE FROM orch_machine");
    for (const x of machines) db.run("INSERT OR REPLACE INTO orch_machine VALUES (?,?,?,?,?)", x.Key ?? null, x.Name ?? null, x.Type ?? null, x.Scope ?? null, scalars(x));
  }
  db.commit();

  if (!a.skipJobs) crawlJobs(db, uip, a, attempt);

  log("downloading deployed packages...");
  const pkgDir = indexSubdir(a.indexDir, "packages");
  const seen = new Set();
  for (const [pid, ver, feed, fk] of db.rows("SELECT DISTINCT package_id, version, feed_id, folder_key FROM process WHERE package_id IS NOT NULL AND version IS NOT NULL")) {
    const key = pid + "\u0000" + ver;
    if (seen.has(key)) continue;
    seen.add(key);
    const dest = path.join(pkgDir, safeName(`${pid}@${ver}`) + ".nupkg");
    try { if (fs.lstatSync(dest).isSymbolicLink()) fs.unlinkSync(dest); } catch { /* absent */ }
    if (fs.existsSync(dest) && fs.statSync(dest).size > 0) continue;
    const tries = [];
    if (feed) tries.push(["--feed-id", feed]);
    if (fk) tries.push(["--folder-key", fk]);
    tries.push([]);
    let ok = false;
    let last = "";
    for (const extra of tries) {
      try {
        uip.run("or", "packages", "download", `${pid}:${ver}`, ...extra, "-d", dest);
        ok = true;
        break;
      } catch (e) {
        if (!(e instanceof UipError)) throw e;
        last = e.message;
      }
    }
    coverage(db, pid, ver, "download", ok ? "ok" : "download_failed", ok ? "" : last);
    log(`  ${ok ? "ok " : "ERR"} ${pid}@${ver}`);
  }
  db.commit();

  if (!a.skipLibraries) downloadLibraries(db, uip, pkgDir, attempt);

  setMeta(db, "crawl_finished", now());
  setMeta(db, "crawl_errors", pyDumps(errors.slice(-200)));
  db.commit();
  log(`crawl done: ${procs === null ? "unknown (list failed)" : procs.length} processes, ${folders.size} folders, ${errors.length} errors (see \`status\`).`);
}

function isoNoMs(d) {
  return d.toISOString().replace(/\.\d{3}Z$/, "");
}

function crawlJobs(db, uip, a, attempt) {
  const nowMs = Date.now();
  const since = isoNoMs(new Date(nowMs - a.jobsDays * DAY_MS)) + "Z";
  const cut30 = isoNoMs(new Date(nowMs - 30 * DAY_MS));
  log(`jobs since ${since} (aggregated; no job payloads are stored)...`);
  const summ = new Map();
  const ident = new Map();
  let n = 0;
  const maxStr = (x, y) => (x === null ? y : y === null ? x : x > y ? x : y);
  const jobs = attempt("jobs list", () => [...uip.paged(["or", "jobs", "list", "--all-folders", "--all-fields", "--created-after", since], a.maxJobs)]);
  if (jobs === null) {
    setMeta(db, "inventory_jobs_ok", "0");
    db.commit();
    return; // keep the previous run counts; status shows the error
  }
  setMeta(db, "inventory_jobs_ok", "1");
  for (const j of jobs) {
    n++;
    const pk = String((j.Release || {}).Key || "").toUpperCase();
    const rn = j.ReleaseName || "";
    const fp = j.OrganizationUnitFullyQualifiedName || "";
    const key = [pk, rn, fp];
    const k = key.join("\u0000");
    if (!summ.has(k)) summ.set(k, { key, runs: 0, r30: 0, faulted: 0, stopped: 0, ok: 0, last: null, lastOk: null });
    const s = summ.get(k);
    const st = j.State;
    const t = j.StartTime || j.CreationTime || null;
    s.runs++;
    s.r30 += (t || "") >= cut30 ? 1 : 0;
    s.faulted += st === "Faulted" ? 1 : 0;
    s.stopped += st === "Stopped" ? 1 : 0;
    s.ok += st === "Successful" ? 1 : 0;
    s.last = maxStr(s.last, t);
    if (st === "Successful") s.lastOk = maxStr(s.lastOk, t);
    const ik = [...key, j.LocalSystemAccount || "", j.HostMachineName || ""];
    const ikey = ik.join("\u0000");
    if (!ident.has(ikey)) ident.set(ikey, { key: ik, runs: 0, last: null, job: j.Key ?? null });
    const i = ident.get(ikey);
    i.runs++;
    if ((t || "") > (i.last || "")) { i.last = t; i.job = j.Key ?? null; }
  }
  db.run("DELETE FROM job_summary");
  db.run("DELETE FROM job_identity");
  for (const s of summ.values()) {
    db.run("INSERT INTO job_summary VALUES (?,?,?,?,?,?,?,?,?,?,?)", ...s.key, s.runs, s.r30, s.faulted, s.stopped, s.ok, s.last, s.lastOk, a.jobsDays);
  }
  for (const v of ident.values()) {
    let orch = null;
    if (a.resolveIdentities && v.job) {
      const d = attempt(`jobs get ${v.job}`, () => uip.run("or", "jobs", "get", v.job));
      orch = ((d || {}).Data || {}).OrchestratorUserIdentity ?? null;
    }
    db.run("INSERT INTO job_identity VALUES (?,?,?,?,?,?,?,?,?)", ...v.key, orch, v.runs, v.last, v.job);
  }
  setMeta(db, "jobs_window_days", String(a.jobsDays));
  setMeta(db, "jobs_read", String(n));
  if (a.maxJobs && n >= a.maxJobs) setMeta(db, "jobs_truncated", `stopped at --max-jobs ${a.maxJobs}; run counts are lower bounds`);
  db.commit();
}

/** Download custom libraries referenced by the downloaded packages (pinned versions only). */
function downloadLibraries(db, uip, pkgDir, attempt) {
  log("libraries referenced by deployed packages...");
  const wanted = new Map();
  for (const fn of fs.readdirSync(pkgDir)) {
    if (!fn.endsWith(".nupkg")) continue;
    let pj;
    try {
      const z = new Zip(fs.readFileSync(path.join(pkgDir, fn)));
      const root = projectRootInZip(z.names());
      if (root === null) continue;
      pj = jsonLoads(z.read(z.names().find((n) => n.toLowerCase() === (root + "project.json").toLowerCase())));
    } catch {
      continue;
    }
    for (const [name, spec] of Object.entries(isObject(pj.dependencies) ? pj.dependencies : {})) {
      if (!isActivityPack(name)) wanted.set([name, resolveVersion(String(spec)), String(spec)].join("\u0000"), [name, resolveVersion(String(spec)), String(spec)]);
    }
  }
  const feedDoc = attempt("feeds list", () => uip.run("or", "feeds", "list")) || {};
  const feeds = (feedDoc.Data || []).filter((f) => isObject(f) && f.Id).map((f) => f.Id);
  const list = [...wanted.values()].sort((x, y) => cmpStr(x[0], y[0]) || cmpStr(x[1] || "", y[1] || ""));
  for (const [name, ver, spec] of list) {
    if (ver === null) {
      coverage(db, name, spec, "download", "download_failed", `version range '${spec}' not pinned; not resolved`);
      continue;
    }
    const dest = path.join(pkgDir, "lib__" + safeName(`${name}@${ver}`) + ".nupkg");
    try { if (fs.lstatSync(dest).isSymbolicLink()) fs.unlinkSync(dest); } catch { /* absent */ }
    if (fs.existsSync(dest) && fs.statSync(dest).size > 0) continue;
    let ok = false;
    let last = "";
    for (const extra of [[], ...feeds.map((f) => ["--feed-id", f])]) {
      try {
        uip.run("or", "libraries", "download", `${name}:${ver}`, ...extra, "-d", dest);
        ok = true;
        break;
      } catch (e) {
        if (!(e instanceof UipError)) throw e;
        last = e.message;
      }
    }
    coverage(db, name, ver, "download", ok ? "ok" : "download_failed", ok ? "" : last);
    log(`  ${ok ? "ok " : "ERR"} library ${name}@${ver}`);
  }
  db.commit();
}

// ------------------------------------------------------------------------ extract

/** Nearest folder at or above `p` (inside `root`) that holds a .uipx solution file. */
function solutionOf(p, root) {
  let cur = path.resolve(p);
  root = path.resolve(root);
  for (;;) {
    let listing;
    try {
      listing = fs.readdirSync(cur);
    } catch {
      return null;
    }
    if (listing.some((f) => f.endsWith(".uipx"))) return path.relative(path.dirname(root), cur).split(path.sep).join("/");
    if (cur === root || path.dirname(cur) === cur) return null;
    cur = path.dirname(cur);
  }
}

const SKIP_DIRS_IN_SOURCE = new Set([".local", ".git", "node_modules", ".objects", ".settings", ".tmh", ".screenshots", ".venv", "__pycache__"]);

function cmdExtract(a) {
  const db = connect(a.indexDir);
  indexSubdir(a.indexDir, "src"); // refuse a linked src/ before anything is written
  EXCLUDED_REAL.add(fs.realpathSync(a.indexDir)); // an index inside a source tree is never read as source
  const inputs = [];
  const pkgDir = path.join(a.indexDir, "packages");
  if (fs.existsSync(pkgDir) && fs.statSync(pkgDir).isDirectory()) {
    for (const fn of fs.readdirSync(pkgDir).sort(cmpStr)) {
      if (fn.endsWith(".nupkg")) inputs.push([path.join(pkgDir, fn), fn.startsWith("lib__") ? "library" : "process", "download", null, null]);
    }
  }
  for (const d of a.nupkg || []) {
    if (!fs.existsSync(d)) throw new Exit(`No such file or folder: ${d}`);
    if (fs.statSync(d).isFile()) {
      inputs.push([d, "process", "supplied", null, null]);
      continue;
    }
    const walk = (dir) => {
      const ents = fs.readdirSync(dir, { withFileTypes: true });
      for (const f of ents.filter((e) => !e.isDirectory()).map((e) => e.name).sort(cmpStr)) {
        if (f.endsWith(".nupkg")) inputs.push([path.join(dir, f), "process", "supplied", null, null]);
      }
      for (const e of ents) if (e.isDirectory()) walk(path.join(dir, e.name));
    };
    walk(d);
  }
  const other = [];
  for (const d of a.source || []) {
    if (!fs.existsSync(d)) throw new Exit(`No such folder: ${d}`);
    const root = path.resolve(d);
    const base = path.dirname(root);
    const walk = (dp) => {
      const ents = fs.readdirSync(dp, { withFileTypes: true });
      const fn = ents.filter((e) => !e.isDirectory()).map((e) => e.name);
      // label = path from the parent of --source, so two projects with the same name never collide
      const label = "source:" + path.relative(base, dp).split(path.sep).join("/");
      const solution = solutionOf(dp, root);
      if (fn.some((f) => f.toLowerCase() === "project.json")) inputs.push([dp, "process", "source", label, solution]);
      else if (["project.uiproj", "agent.json", "uipath.json", "langgraph.json"].some((f) => fn.includes(f)) || fn.some((f) => f.endsWith(".flow") || f.endsWith(".bpmn"))) {
        other.push([dp, label, solution]);
      }
      const subs = ents.filter((e) => e.isDirectory() && !SKIP_DIRS_IN_SOURCE.has(e.name) && !isExcluded(path.join(dp, e.name))).map((e) => e.name).sort(cmpStr);
      for (const s of subs) walk(path.join(dp, s));
    };
    walk(root);
  }
  if (!inputs.length && !other.length) throw new Exit("Nothing to extract. Run `crawl` first, or pass --nupkg / --source.");
  for (const [dp, label, solution] of other) {
    const name = path.basename(dp);
    db.run("INSERT OR REPLACE INTO package (package_id, version, kind, origin, file_path, layout, has_sources, extractor_version, indexed_at, solution) " +
      "VALUES (?,?,?,?,?,?,?,?,?,?)", name, label, "other", "source", dp, "source", 0, EXTRACTOR_VERSION, now(), solution);
    coverage(db, name, label, "extract", "unsupported", "project without project.json (agent, flow, BPMN, app or API workflow); not indexed by this version");
    log(`  unsupported ${name} (${label})`);
  }
  db.commit();
  let ok = 0;
  let bad = 0;
  for (const [p, kind, origin, label, solution] of inputs) {
    try {
      const [pkg, ver] = extractOne(db, a.indexDir, p, kind, origin, a.force, label, solution);
      db.commit();
      ok++;
      log(`  indexed ${pkg}@${ver}`);
    } catch (e) {
      db.rollback();
      bad++;
      coverage(db, path.basename(p), "?", "extract", "parse_error", `${e.constructor.name}: ${e.message}`);
      db.commit();
      log(`  ERR ${p}: ${e.message}`);
    }
  }
  setMeta(db, "extract_finished", now());
  db.commit();
  refreshClosure(db);
  db.exec("ANALYZE"); // planner statistics, so ad-hoc queries over large indexes stay fast
  log(`extract done: ${ok} packages read, ${bad} failed.`);
}

// ------------------------------------------------------------------------ analyze

function cmdAnalyze(a) {
  const db = connect(a.indexDir);
  const uip = new Uip(a.uip, a.profile, a.timeout);
  let rows = db.rows("SELECT package_id, version, target_framework, has_sources FROM package WHERE has_sources=1 ORDER BY package_id");
  if (a.only) rows = rows.filter((r) => fnmatch(r[0], a.only));
  const work = indexSubdir(a.indexDir, "work");
  for (const [pkg, ver, tfRaw] of rows) {
    const done = db.one("SELECT status FROM analyzer_run WHERE package_id=? AND version=?", pkg, ver);
    if (done && done[0] === "ok" && !a.force) continue;
    const tf = (tfRaw || "").toLowerCase();
    if ((tf === "windows" || tf === "legacy") && process.platform !== "win32") {
      recordAnalyzer(db, pkg, ver, "skipped_platform", `${tf || "unknown"}-target project; uip rpa analyze refuses Windows projects on this OS ` +
        "('Cannot execute Windows projects on Linux platform'). Run analyze on a Windows host.", a.policyFile);
      continue;
    }
    const src = path.join(a.indexDir, "src", srcDirName(pkg, ver));
    if (!fs.existsSync(src)) {
      recordAnalyzer(db, pkg, ver, "skipped_no_source_tree", "run extract first", a.policyFile);
      continue;
    }
    const copy = fs.mkdtempSync(path.join(work, safeName(pkg).slice(0, 40) + "-"));
    try {
      fs.cpSync(src, path.join(copy, "p"), { recursive: true });
      const args = ["rpa", "analyze", path.join(copy, "p")];
      if (a.policyFile) args.push("--policy-file", a.policyFile, "--policy-file-type", a.policyFileType);
      if (a.governanceProduct) args.push("--governance-product", a.governanceProduct);
      log(`  analyze ${pkg}@${ver}`);
      let doc;
      try {
        doc = uip.run(...args);
      } catch (e) {
        if (!(e instanceof UipError)) throw e;
        recordAnalyzer(db, pkg, ver, "failed", cut(e.message, 1500), a.policyFile);
        continue;
      }
      const data = doc.Data || {};
      const summ = data.Summary || {};
      db.run("DELETE FROM analyzer_finding WHERE package_id=? AND version=?", pkg, ver);
      const projDir = path.join(copy, "p");
      // The analyzer reports paths inside the throwaway copy; keep them relative to the project.
      const relFile = (f) => {
        if (!f) return f ?? null;
        if (!path.isAbsolute(f)) return f.split(path.sep).join("/");
        const r = path.relative(projDir, f);
        return r.startsWith("..") ? f : r.split(path.sep).join("/");
      };
      for (const f of data.Findings || []) {
        db.run("INSERT INTO analyzer_finding VALUES (?,?,?,?,?,?,?,?,?,?)", pkg, ver, f.Level ?? null, f.RuleName ?? null, f.ErrorCode ?? null,
          relFile(f.FilePath), f.ActivityIdRef ?? null, f.ActivityDisplayName ?? null, f.Description ?? null, f.Recommendation ?? null);
      }
      db.run("INSERT OR REPLACE INTO analyzer_run VALUES (?,?,?,?,?,?,?,?,?)", pkg, ver, "ok", null, (summ.Error || 0) + (summ.Critical || 0),
        summ.Warning || 0, summ.Information || 0, a.policyFile || "tenant/default", now());
    } finally {
      fs.rmSync(copy, { recursive: true, force: true });
    }
    db.commit();
  }
  db.commit();
  log("analyze done (see `status`).");
}

function recordAnalyzer(db, pkg, ver, status, detail, policy) {
  db.run("INSERT OR REPLACE INTO analyzer_run VALUES (?,?,?,?,?,?,?,?,?)", pkg, ver, status, detail, null, null, null, policy || "tenant/default", now());
  db.commit();
}

// ------------------------------------------------------------------------- review

// Each rule returns rows of [package_id, version, process_key, subject, detail, evidence].
const RULES = [
  ["AI-COV-001", "Warning", "Package could not be read from source", (db) => db.rows(
    `SELECT package_id, version, NULL, package_id || '@' || version, status || ': ' || detail, NULL
     FROM coverage WHERE stage IN ('extract','download') AND status NOT IN ('indexed','ok')`)],
  ["AI-SEC-001", "Critical", "Hard-coded secret in source", (db) => db.rows(
    `SELECT package_id, version, NULL, normalized, detail || ' ' || value,
            file || COALESCE(':' || line, '') || COALESCE(' [' || activity || ']', '')
     FROM edge WHERE kind='hardcoded_secret'`)],
  ["AI-RES-001", "Warning", "Asset referenced in code does not exist in Orchestrator", (db) => resourceRule(db, "asset", "missing")],
  ["AI-RES-002", "Info", "Asset exists, but not in the folder of the process that reads it", (db) => resourceRule(db, "asset", "folder")],
  ["AI-RES-003", "Warning", "Queue referenced in code does not exist in Orchestrator", (db) => resourceRule(db, "queue", "missing")],
  ["AI-RES-004", "Info", "Orchestrator asset not referenced by any indexed package", (db) => {
    const gaps = db.one("SELECT count(*) FROM coverage WHERE stage IN ('extract','download') AND status NOT IN ('indexed','ok')")[0];
    const dyn = db.one("SELECT count(*) FROM edge WHERE kind IN ('reads_asset','reads_credential') AND normalized='<dynamic>'")[0];
    const caveat = gaps || dyn ? `coverage gaps: ${gaps} packages unreadable, ${dyn} dynamic asset reads` : "full coverage";
    return db.rows(`SELECT NULL, NULL, NULL, a.name, 'folder ' || a.folder_path || '; type ' || a.value_type || '; ' || ?, NULL
       FROM orch_asset a WHERE NOT EXISTS
       (SELECT 1 FROM edge e WHERE e.kind IN ('reads_asset','reads_credential','writes_asset','writes_credential')
        AND e.normalized=lower(a.name))`, caveat);
  }],
  ["AI-RUN-001", "Warning", "Deployed process with no runs in the job window", (db) => {
    if (!db.one("SELECT value FROM meta WHERE key='jobs_window_days'")) return [];
    return db.rows(`SELECT package_id, version, process_key, process_name,
           'folder ' || folder_path || '; type ' || COALESCE(process_type,'') || '; window ' ||
           (SELECT value FROM meta WHERE key='jobs_window_days') || ' days', NULL
       FROM v_automation WHERE runs = 0 AND COALESCE(process_type,'') NOT IN ('Agent','Flow')`);
  }],
  ["AI-RUN-002", "Warning", "Process failed in more than 20% of runs", (db) => db.rows(
    `SELECT package_id, version, process_key, process_name,
            faulted || ' faulted of ' || runs || ' runs; last success ' || COALESCE(last_success,'never'), NULL
     FROM v_automation WHERE runs >= 5 AND faulted * 1.0 / runs > 0.2`)],
  ["AI-LIB-001", "Info", "Same library deployed at several versions", (db) => db.rows(
    `SELECT NULL, NULL, NULL, d.dep_name,
            count(DISTINCT d.dep_spec) || ' versions: ' || group_concat(DISTINCT d.dep_spec), NULL
     FROM dependency d JOIN process p ON p.package_id=d.package_id AND p.version=d.version
     WHERE d.is_activity_pack=0 GROUP BY d.dep_name HAVING count(DISTINCT d.dep_spec) > 1`)],
  ["AI-LIB-002", "Info", "Activity package at several versions across deployed processes", (db) => db.rows(
    `SELECT NULL, NULL, NULL, d.dep_name,
            count(DISTINCT d.dep_spec) || ' versions: ' || group_concat(DISTINCT d.dep_spec), NULL
     FROM dependency d JOIN process p ON p.package_id=d.package_id AND p.version=d.version
     WHERE d.is_activity_pack=1 GROUP BY d.dep_name HAVING count(DISTINCT d.dep_spec) > 2`)],
  ["AI-SRC-001", "Info", "Workflow not reachable from any entry point", (db) => db.rows(
    `SELECT package_id, version, NULL, path, 'kind ' || kind || '; ' || activities || ' activities', path
     FROM workflow WHERE reachable = 0`)],
  ["AI-SRC-002", "Info", "Target resolved only at runtime (dynamic URL, asset, queue or process)", (db) => db.rows(
    `SELECT package_id, version, NULL, kind, count(*) || ' values built at runtime; first: ' || substr(min(value), 1, 160),
            min(file || COALESCE(':' || line, '') || COALESCE(' [' || activity || ']', ''))
     FROM edge WHERE normalized='<dynamic>' GROUP BY package_id, version, kind`)],
  ["AI-SRC-003", "Warning", "Workflow file could not be parsed", (db) => db.rows(
    "SELECT package_id, version, NULL, path, parse_error, path FROM workflow WHERE parse_error IS NOT NULL")],
  ["AI-WA-001", "Warning", "Workflow Analyzer errors in the deployed package", (db) => db.rows(
    `SELECT f.package_id, f.version, NULL, COALESCE(NULLIF(f.rule,''), f.error_code),
            f.description, f.file || COALESCE(' [' || NULLIF(f.activity_id,'') || ']', '')
     FROM analyzer_finding f WHERE f.level IN ('Error','Critical')`)],
  ["AI-WA-002", "Info", "Workflow Analyzer could not run on this package", (db) => db.rows(
    "SELECT package_id, version, NULL, status, detail, NULL FROM analyzer_run WHERE status <> 'ok'")],
  ["AI-GOV-001", "Critical", "Application or URL outside the restriction list", (db, ctx) => restrictionRule(db, ctx)],
];

/** Was the Orchestrator inventory of this resource type read completely? */
function inventoryUsable(db, type) {
  const flag = db.one("SELECT value FROM meta WHERE key=?", `inventory_${type}_ok`);
  if (flag) return flag[0] === "1";
  return !!db.one(`SELECT 1 FROM orch_${type} LIMIT 1`); // indexes from before the flag existed
}

/** Folder an activity names, resolved like Orchestrator does: "." and ".." are relative to the process's folder. */
function resolveFolder(processFolder, named) {
  const n = named.replaceAll("\\", "/").replace(/\/+$/, "");
  if (!/^\.\.?(\/|$)/.test(n)) return n;
  const parts = String(processFolder || "").split("/").filter(Boolean);
  for (const seg of n.split("/")) {
    if (seg === "..") parts.pop();
    else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

/** Resource checks that honour the folder an activity names (FolderPath), grouped per process and resource. */
function resourceRule(db, type, check) {
  if (!inventoryUsable(db, type)) return [];
  const inv = new Map();
  for (const [name, fpath, fkey] of db.rows(`SELECT name, folder_path, folder_key FROM orch_${type}`)) {
    const k = String(name || "").toLowerCase();
    if (!inv.has(k)) inv.set(k, []);
    inv.get(k).push({ path: String(fpath || ""), key: fkey });
  }
  const kinds = type === "queue" ? "('uses_queue')" : "('reads_asset','reads_credential')";
  const groups = new Map();
  for (const [pkg, ver, pkey, pfolder, pfkey, kind, value, norm, detail, file, line, via] of db.rows(
    `SELECT e.root_package_id, e.root_version, p.key, p.folder_path, p.folder_key, e.kind, e.value, e.normalized, e.detail, e.file, e.line, e.via
     FROM process p JOIN v_edge_all e ON e.root_package_id=p.package_id AND e.root_version=p.version
     WHERE e.kind IN ${kinds} AND e.normalized <> '<dynamic>' ORDER BY p.key, e.file, e.line`)) {
    const named = (/folder=([^;]+)/.exec(detail || "") || [])[1]?.trim();
    const unknownFolder = named === DYN; // set at runtime: no claim about which folder
    const folder = named && !unknownFolder ? resolveFolder(pfolder, named) : null;
    const hits = inv.get(norm) || [];
    const where = [...new Set(hits.map((h) => h.path))].join(", ");
    let problem = null;
    if (check === "missing") {
      if (!hits.length) problem = "not found in any folder the crawl could see";
      else if (folder && !hits.some((h) => h.path.toLowerCase() === folder.toLowerCase())) problem = `not in folder '${folder}' named by the activity; exists in: ${where}`;
    } else if (hits.length && !folder && !unknownFolder && !hits.some((h) => h.key === pfkey)) {
      problem = `process folder ${S(pfolder)}; ${type} found in: ${where}`;
    }
    if (!problem) continue;
    const k = [pkey, kind, norm, problem].join("\u0000");
    if (!groups.has(k)) groups.set(k, { row: [pkg, ver, pkey, value], kind, problem, pfolder, via, n: 0, first: `${file}${line ? ":" + line : ""}` });
    groups.get(k).n++;
  }
  return [...groups.values()].map((g) => [...g.row,
    `kind=${g.kind}; ${g.problem}; process folder ${S(g.pfolder)}${g.via ? "; via " + g.via : ""}; ${g.n} references`, g.first]);
}

function restrictionRule(db, ctx) {
  const pol = ctx.restrictions;
  if (!pol) return [];
  const allowed = (value, section) => {
    const sec = pol[section] || {};
    const v = (value || "").toLowerCase();
    if ((sec.deny || []).some((p) => fnmatch(v, p.toLowerCase()))) return [false, "matches deny list"];
    const allow = sec.allow;
    if (allow !== undefined && allow !== null && !allow.some((p) => fnmatch(v, p.toLowerCase()))) return [false, "not on allow list"];
    return [true, ""];
  };
  const groups = new Map();
  for (const [pkg, ver, kind, value, norm, f, line, act, extractor] of db.rows(
    "SELECT package_id, version, kind, value, normalized, file, line, activity, extractor FROM edge " +
    "WHERE kind IN ('uses_application','targets_url','calls_api') AND normalized <> '<dynamic>' ORDER BY package_id, file, line")) {
    const section = kind === "uses_application" ? "applications" : "urls";
    const candidates = section === "applications" ? [norm] : [norm, (value || "").toLowerCase()];
    const verdicts = candidates.filter(Boolean).map((c) => allowed(c, section));
    const denied = verdicts.filter(([ok, w]) => !ok && w === "matches deny list").map(([, w]) => w);
    if (denied.length || (verdicts.length && verdicts.every(([ok]) => !ok))) {
      const why = denied.length ? denied[0] : verdicts[0][1];
      const k = [pkg, ver, norm, kind, why].join("\u0000");
      if (!groups.has(k)) groups.set(k, { key: [pkg, ver, norm, kind, why], n: 0, ex: new Set(), first: null });
      const g = groups.get(k);
      g.n++;
      g.ex.add(extractor);
      g.first = g.first || `${f}${line ? ":" + line : ""}${act ? " [" + act + "]" : ""}`;
    }
  }
  return [...groups.values()].map((g) => {
    const [pkg, ver, norm, kind, why] = g.key;
    return [pkg, ver, null, norm, `${kind} ${why}; ${g.n} occurrences; extractors ${[...g.ex].sort(cmpStr).join(", ")}`, g.first];
  });
}

function md(x) {
  return cut((x === null || x === undefined ? "" : String(x)).replaceAll("|", "\\|").replaceAll("\n", " "), 300);
}

function cmdReview(a) {
  const db = connect(a.indexDir);
  const ctx = {};
  if (a.restrictions) ctx.restrictions = JSON.parse(fs.readFileSync(a.restrictions, "utf8"));
  db.run("DELETE FROM finding");
  const ts = now();
  const procsByPkg = new Map();
  for (const [key, pkg, ver] of db.rows("SELECT key, package_id, version FROM process")) {
    const k = pkg + "\u0000" + ver;
    if (!procsByPkg.has(k)) procsByPkg.set(k, []);
    procsByPkg.get(k).push(key);
  }
  const counts = [];
  for (const [rid, sev, title, fn] of RULES) {
    const rows = fn(db, ctx);
    for (const [pkg, ver, pkey, subject, detail, evidence] of rows) {
      db.run("INSERT INTO finding VALUES (?,?,?,?,?,?,?,?,?)", rid, sev, pkg, ver,
        pkey || (procsByPkg.get(pkg + "\u0000" + ver) || []).join(",") || null, subject, detail, evidence, ts);
    }
    counts.push([rid, sev, title, rows.length]);
  }
  db.commit();
  const out = path.join(indexSubdir(a.indexDir, "reports"), `review-${ts.slice(0, 10)}.md`);
  removeEntry(out);
  const w = [];
  w.push(`# Automation review — ${ts.slice(0, 10)}\n\n`);
  w.push("Generated by automation-index from the index database. Every row cites the file, line or " +
    "Orchestrator object it came from. Nothing in this report was written by a language model.\n\n");
  w.push(coverageText(db) + "\n\n## Findings by rule\n\n| Rule | Severity | Finding | Count |\n|---|---|---|---|\n");
  for (const [rid, sev, title, c] of counts) w.push(`| ${rid} | ${sev} | ${title} | ${c} |\n`);
  for (const [rid, sev, title, c] of counts) {
    if (!c) continue;
    w.push(`\n## ${rid} — ${title} (${sev}, ${c})\n\n| Package | Process | Subject | Detail | Evidence |\n|---|---|---|---|---|\n`);
    for (const [pkg, ver, pk, subj, det, ev] of db.rows("SELECT package_id, version, process_key, subject, detail, evidence FROM finding WHERE rule=? LIMIT ?", rid, a.maxRows)) {
      let pname = "";
      if (pk) {
        const keys = pk.split(",");
        pname = db.rows(`SELECT name FROM process WHERE key IN (${keys.map(() => "?").join(",")})`, ...keys).map((r) => r[0]).join(", ");
      }
      w.push("| " + [pkg ? `${pkg}@${ver}` : "", pname, subj, det, ev].map(md).join(" | ") + " |\n");
    }
    if (c > a.maxRows) w.push(`\n_${c - a.maxRows} more rows: \`query "SELECT * FROM finding WHERE rule='${rid}'"\`_\n`);
  }
  fs.writeFileSync(out, w.join(""), "utf8");
  console.log(out);
}

// -------------------------------------------------------------------------- cards

function coverageText(db) {
  const q = (sql) => db.one(sql)[0];
  const ext = "SELECT count(*) FROM coverage WHERE stage='extract' AND status";
  const lines = [
    "## Coverage", "",
    `- Processes in Orchestrator: ${q("SELECT count(*) FROM process")}`,
    `- Packages indexed from source: ${q(ext + "='indexed'")}`,
    `- Packages without sources (compiled only): ${q(ext + "='compiled_only'")}`,
    `- Packages not readable (unsupported type or parse error): ${q(ext + " IN ('unsupported','parse_error')")}`,
    `- Packages read only partly (unreadable project.json): ${q(ext + "='partial'")}`,
    `- Downloads failed: ${q("SELECT count(*) FROM coverage WHERE stage='download' AND status='download_failed'")}`,
    `- Deployed processes whose package is not indexed: ${q("SELECT count(*) FROM process p WHERE NOT EXISTS (SELECT 1 FROM package k WHERE k.package_id=p.package_id AND k.version=p.version)")}`,
    `- Workflow Analyzer: ${q("SELECT count(*) FROM analyzer_run WHERE status='ok'")} analyzed, ${q("SELECT count(*) FROM analyzer_run WHERE status<>'ok'")} skipped or failed`,
  ];
  for (const k of ["crawl_tenant", "crawl_finished", "extract_finished", "jobs_window_days", "jobs_truncated"]) {
    const r = db.one("SELECT value FROM meta WHERE key=?", k);
    if (r) lines.push(`- ${k.replaceAll("_", " ")}: ${S(r[0])}`);
  }
  return lines.join("\n");
}

const EDGE_SECTIONS = [
  ["Applications", ["uses_application"]],
  ["URLs and APIs", ["targets_url", "calls_api"]],
  ["Assets and credentials", ["reads_asset", "reads_credential", "writes_asset", "writes_credential"]],
  ["Queues", ["uses_queue"]],
  ["Storage buckets", ["uses_bucket"]],
  ["Processes invoked", ["invokes_process"]],
  ["Integration Service connections", ["uses_connection"]],
  ["Databases", ["uses_database"]],
  ["Email", ["sends_email_to_domain", "uses_mail_server"]],
  ["Code executed", ["runs_code"]],
  ["UI technologies", ["ui_technology"]],
  ["Files", ["uses_file"]],
];

function cardFor(db, pkg, ver, proc = null) {
  const out = [];
  const k = db.objects("SELECT * FROM package WHERE package_id=? AND version=?", pkg, ver)[0] || {};
  const get = (x) => (k[x] === undefined ? null : k[x]);
  out.push(`# ${proc ? proc.name : pkg}\n`);
  out.push("> Generated from the automation index. Facts below cite their source; nothing here is written by a language model unless marked **AI summary**.\n");
  out.push("| Property | Value |\n|---|---|");
  const rows = [["Package", `${pkg}@${ver}`], ["Folder", proc ? proc.folder_path : "—"],
    ["Process type", proc ? proc.process_type : get("output_type")],
    ["Target framework", get("target_framework")], ["Studio version", get("studio_version")],
    ["Expression language", get("expression_language")], ["Sources in package", get("has_sources") ? "yes" : "**no**"],
    ["Solution", get("solution")], ["Read from", get("origin") === "source" ? get("file_path") : get("origin")],
    ["Description", get("description")]];
  for (const [name, val] of rows) out.push(`| ${name} | ${md(val) || "—"} |`);
  out.push("");
  if (proc) {
    const js = db.one("SELECT sum(runs), sum(runs_30d), sum(faulted), max(last_run), max(last_success), max(window_days) FROM job_summary WHERE process_key=upper(?)", proc.key);
    out.push("## Runtime\n");
    if (js && js[0]) {
      out.push(`- Runs in last ${S(js[5])} days: ${S(js[0])} (${S(js[1])} in last 30), faulted: ${S(js[2])}`);
      out.push(`- Last run: ${S(js[3])}; last success: ${js[4] || "never"}`);
    } else {
      out.push("- No runs in the job window, or jobs were not crawled.");
    }
    const ids = db.rows("SELECT local_account, host_machine, orchestrator_identity, runs, last_seen FROM job_identity WHERE process_key=upper(?) ORDER BY runs DESC", proc.key);
    if (ids.length) {
      out.push(`- Identities seen (${ids.length}):`);
      for (const [la, hm, oi, r, ls] of ids) out.push(`  - account \`${la || "?"}\`` + (oi ? ` / Orchestrator \`${oi}\`` : "") + ` on \`${hm || "?"}\` — ${S(r)} runs, last ${S(ls)}`);
    }
    for (const [tt, nm, en] of db.rows("SELECT trigger_type, name, enabled FROM orch_trigger WHERE process_key=? OR (process_name=? AND folder_key=?)", proc.key, proc.name, proc.folder_key)) {
      out.push(`- Trigger: ${tt} \`${S(nm)}\` (${en ? "enabled" : "disabled"})`);
    }
    out.push("");
  }
  for (const [heading, kinds] of EDGE_SECTIONS) {
    const rows2 = db.rows(`SELECT kind, normalized, min(value), group_concat(DISTINCT extractor), count(*),
        min(file || COALESCE(':' || line, '')), max(static), min(confidence), group_concat(DISTINCT NULLIF(via,''))
      FROM v_edge_all WHERE root_package_id=? AND root_version=? AND kind IN (${kinds.map(() => "?").join(",")})
      GROUP BY kind, normalized ORDER BY kind, normalized`, pkg, ver, ...kinds);
    if (!rows2.length) continue;
    out.push(`## ${heading}\n\n| Kind | Value | Evidence | Count | Extractors | Confidence | Via library |\n|---|---|---|---|---|---|---|`);
    for (const [kind, norm, val, exs, cnt, ev, stat, conf, via] of rows2) {
      const shown = norm === DYN ? val : kind === "uses_application" ? norm : val;
      out.push(`| ${kind} | ${md(shown)}${!stat ? " _(dynamic)_" : ""} | ${md(ev)} | ${cnt} | ${md(exs)} | ${S(conf)} | ${md(via) || ""} |`);
    }
    out.push("");
  }
  const deps = db.rows(`SELECT dep_name, dep_spec, is_activity_pack, (SELECT status FROM coverage c WHERE c.package_id=d.dep_name
      AND c.version=d.dep_version AND c.stage='extract') FROM dependency d WHERE package_id=? AND version=? ORDER BY is_activity_pack, dep_name`, pkg, ver);
  if (deps.length) {
    out.push("## Dependencies\n\n| Package | Version | Kind | Indexed |\n|---|---|---|---|");
    for (const [n, s, ap, stt] of deps) out.push(`| ${n} | ${s} | ${ap ? "activity pack" : "library"} | ${stt || (ap ? "—" : "not indexed")} |`);
    out.push("");
  }
  const wf = db.one("SELECT count(*), sum(reachable=1), sum(reachable=0), sum(reachable IS NULL), sum(activities) FROM workflow WHERE package_id=? AND version=? AND kind<>'compiled'", pkg, ver);
  const cw = db.one("SELECT count(*) FROM workflow WHERE package_id=? AND version=? AND kind='compiled'", pkg, ver)[0];
  if (cw) out.push(`## Workflows\n\n- ${cw} workflow names recovered from the compiled assembly (no source to read; activities and reachability unknown)\n`);
  if (wf && wf[0]) {
    out.push(`## Workflows\n\n- ${wf[0]} files, ${wf[4] || 0} activities; reachable from entry points: ${wf[1] || 0}, unreachable: ${wf[2] || 0}, unknown (dynamic invokes or coded): ${wf[3] || 0}`);
    const eps = db.rows("SELECT DISTINCT path FROM entry_point WHERE package_id=? AND version=?", pkg, ver).map((r) => r[0]);
    if (eps.length) out.push("- Entry points: " + eps.map((e) => `\`${e}\``).join(", "));
    out.push("");
  }
  const ar = db.one("SELECT status, errors, warnings, detail FROM analyzer_run WHERE package_id=? AND version=?", pkg, ver);
  if (ar) {
    out.push("## Workflow Analyzer\n");
    out.push(ar[0] === "ok" ? `- ${ar[0]}: ${S(ar[1])} errors, ${S(ar[2])} warnings` : `- ${ar[0]}: ${S(ar[3])}`);
    out.push("");
  }
  const fnd = db.rows("SELECT rule, severity, subject, detail FROM finding WHERE package_id=? AND version=? ORDER BY CASE severity WHEN 'Critical' THEN 0 WHEN 'Warning' THEN 1 ELSE 2 END LIMIT 40", pkg, ver);
  if (fnd.length) {
    out.push("## Findings\n\n| Rule | Severity | Subject | Detail |\n|---|---|---|---|");
    for (const r of fnd) out.push("| " + r.map(md).join(" | ") + " |");
    out.push("");
  }
  const s = db.one("SELECT model, text, source_sha256, written_at FROM ai_summary WHERE package_id=? AND version=?", pkg, ver);
  if (s) {
    const stale = s[2] !== get("sha256");
    out.push(`## AI summary (non-authoritative${stale ? "; STALE — package changed since" : ""})\n`);
    out.push(`_Written by ${S(s[0])} on ${S(s[3])}. Verify against the facts above before relying on it._\n`);
    out.push(String(s[1]).trim() + "\n");
  }
  out.push("## Provenance\n");
  for (const [stg, stt, det] of db.rows("SELECT stage, status, detail FROM coverage WHERE package_id=? AND version=?", pkg, ver)) {
    out.push(`- ${stg}: ${stt}` + (det ? ` — ${det}` : ""));
  }
  out.push(`- Source tree: \`src/${srcDirName(pkg, ver)}/\`; extractor v${EXTRACTOR_VERSION}; indexed ${S(get("indexed_at"))}`);
  return out.join("\n") + "\n";
}

function cmdCards(a) {
  const db = connect(a.indexDir);
  const d = path.join(a.indexDir, "cards");
  removeEntry(d);
  fs.mkdirSync(d, { recursive: true });
  const index = ["# Automation index\n", coverageText(db), "", "## Automations\n", "| Automation | Folder | Package | Sources | Runs | Card |", "|---|---|---|---|---|---|"];
  const done = new Set();
  for (const proc of db.objects("SELECT * FROM process ORDER BY folder_path, name")) {
    const fn = safeName(`${proc.folder_path || "nofolder"}__${proc.name}`) + ".md";
    fs.writeFileSync(path.join(d, fn), cardFor(db, proc.package_id, proc.version, proc), "utf8");
    done.add(proc.package_id + "\u0000" + proc.version);
    const hs = db.one("SELECT has_sources FROM package WHERE package_id=? AND version=?", proc.package_id, proc.version);
    const runs = db.one("SELECT sum(runs) FROM job_summary WHERE process_key=upper(?)", proc.key)[0];
    index.push(`| ${md(proc.name)} | ${md(proc.folder_path)} | ${md(proc.package_id)}@${md(proc.version)} | ${hs === null ? "—" : hs[0] ? "yes" : "no"} | ${runs || 0} | [card](cards/${fn}) |`);
  }
  const rest = db.rows("SELECT package_id, version, kind FROM package ORDER BY kind, package_id").filter((r) => !done.has(r[0] + "\u0000" + r[1]));
  if (rest.length) {
    index.push("", "## Packages not deployed as a process (libraries, supplied packages, source folders)\n", "| Package | Kind | Card |", "|---|---|---|");
    for (const [pkg, ver, kind] of rest) {
      const fn = safeName(`pkg__${pkg}@${ver}`) + ".md";
      fs.writeFileSync(path.join(d, fn), cardFor(db, pkg, ver), "utf8");
      index.push(`| ${md(pkg)}@${md(ver)} | ${S(kind)} | [card](cards/${fn}) |`);
    }
  }
  removeEntry(path.join(a.indexDir, "INDEX.md"));
  fs.writeFileSync(path.join(a.indexDir, "INDEX.md"), index.join("\n") + "\n", "utf8");
  console.log(path.join(a.indexDir, "INDEX.md"));
}

// ----------------------------------------------------------------- query / status

function csvField(v) {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\r\n]/.test(s) ? '"' + s.replaceAll('"', '""') + '"' : s;
}

/** Strip comments and string literals so statement structure can be checked. */
function sqlSkeleton(sql) {
  return sql.replace(/--[^\n]*|\/\*[\s\S]*?(\*\/|$)|'(?:[^']|'')*'?|"(?:[^"]|"")*"?|`[^`]*`?|\[[^\]]*\]?/g, " ");
}

const READ_PRAGMAS = new Set(["table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
  "foreign_key_list", "collation_list", "function_list", "pragma_list", "compile_options", "user_version", "schema_version"]);

/** One read-only statement: SELECT, WITH, VALUES, EXPLAIN, or a PRAGMA from READ_PRAGMAS. */
function checkReadOnlySql(sql) {
  const sk = sqlSkeleton(sql).trim().replace(/;\s*$/, "");
  if (sk.includes(";")) throw new Exit("query runs exactly one statement; remove the extra ';'-separated statements.");
  const first = (/^[A-Za-z]+/.exec(sk) || [""])[0].toUpperCase();
  const pragma = first === "PRAGMA" ? ((/^PRAGMA\s+(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)/i.exec(sk) || [])[1] || "").toLowerCase() : null;
  if (!["SELECT", "WITH", "VALUES", "EXPLAIN", "PRAGMA"].includes(first) || (pragma !== null && (!READ_PRAGMAS.has(pragma) || sk.includes("=")))) {
    throw new Exit(`query is read-only: ${pragma ? "PRAGMA " + pragma : first || "this statement"} is not allowed. Use SELECT, WITH, VALUES, EXPLAIN or one of these PRAGMAs: ${[...READ_PRAGMAS].join(", ")}.`);
  }
}

function cmdQuery(a) {
  const file = path.join(a.indexDir, "index.db");
  if (!fs.existsSync(file)) throw new Exit(`No index at ${file}.`);
  const sql = a.sql !== "-" ? a.sql : fs.readFileSync(0, "utf8");
  checkReadOnlySql(sql);
  const db = new DB(file, { readOnly: true });
  const st = db.db.prepare(sql);
  let cols = typeof st.columns === "function" ? st.columns().map((c) => c.name) : null;
  let rows;
  const take = (it) => {
    const out = [];
    for (const r of it) {
      out.push(r);
      if (a.limit && out.length >= a.limit) break;
    }
    return out;
  };
  if (typeof st.setReturnArrays === "function") {
    st.setReturnArrays(true);
    rows = take(typeof st.iterate === "function" ? st.iterate() : st.all());
  } else {
    // Older Node: objects only. Wrap as a subquery so duplicate column names stay distinct.
    let objs;
    const first = (/^[A-Za-z]+/.exec(sqlSkeleton(sql).trim()) || [""])[0].toUpperCase();
    if (first === "PRAGMA" || first === "EXPLAIN") {
      objs = take(typeof st.iterate === "function" ? st.iterate() : st.all()); // bounded output; cannot be wrapped
    } else {
      const body = sql.replace(/;(\s|--[^\n]*)*$/, "");
      let wrapped = null;
      try {
        wrapped = db.db.prepare(`SELECT * FROM (\n${body}\n)` + (a.limit ? ` LIMIT ${a.limit}` : ""));
      } catch {
        // Every read can be wrapped; one that cannot is a write in disguise (WITH ... INSERT).
        // Run it as written so the read-only connection refuses it with SQLite's own error.
      }
      objs = wrapped ? take(typeof wrapped.iterate === "function" ? wrapped.iterate() : wrapped.all()) : take(st.all());
    }
    cols = objs.length ? Object.keys(objs[0]) : cols || [];
    rows = objs.map((o) => Object.values(o));
  }
  cols = cols || [];
  // Duplicate names stay distinct (x, x:1, x:2) without colliding with a column already called "x:1".
  const taken = new Set(cols);
  const emitted = new Set();
  cols = cols.map((c) => {
    if (!emitted.has(c)) {
      emitted.add(c);
      return c;
    }
    let i = 1;
    while (taken.has(`${c}:${i}`) || emitted.has(`${c}:${i}`)) i++;
    emitted.add(`${c}:${i}`);
    taken.add(`${c}:${i}`);
    return `${c}:${i}`;
  });
  if (a.format === "json") {
    console.log(JSON.stringify(rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, typeof r[i] === "bigint" ? String(r[i]) : r[i]]))), null, 1));
  } else if (a.format === "csv") {
    const lines = [cols.map(csvField).join(","), ...rows.map((r) => r.map(csvField).join(","))];
    process.stdout.write(lines.join("\r\n") + "\r\n");
  } else {
    console.log("| " + cols.join(" | ") + " |");
    console.log("|" + "---|".repeat(cols.length));
    for (const r of rows) console.log("| " + r.map(md).join(" | ") + " |");
    console.log(`\n${rows.length} rows` + (a.limit && rows.length === a.limit ? " (limit reached)" : ""));
  }
}

function cmdStatus(a) {
  const db = connect(a.indexDir);
  console.log(coverageText(db));
  console.log("\n## Coverage detail\n");
  for (const [stage, status, n] of db.rows("SELECT stage, status, count(*) FROM coverage WHERE stage NOT LIKE 'config:%' GROUP BY stage, status ORDER BY stage, status")) {
    console.log(`- ${stage} / ${status}: ${n}`);
  }
  const un = db.one("SELECT COALESCE(sum(runs),0) FROM job_summary WHERE process_key=''")[0];
  if (un) console.log(`- jobs without a process key (Studio debug runs, deleted processes): ${un}`);
  const errs = db.one("SELECT value FROM meta WHERE key='crawl_errors'");
  if (errs && errs[0] !== "[]" && errs[0] !== "") {
    console.log("\n## Crawl errors (last 200)\n");
    for (const e of JSON.parse(errs[0])) console.log(`- ${e}`);
  }
  const bad = db.rows("SELECT package_id, version, stage, status, detail FROM coverage WHERE status NOT IN ('indexed','ok') AND stage NOT LIKE 'config:%' ORDER BY stage LIMIT 100");
  if (bad.length) {
    console.log("\n## Not fully read\n");
    for (const r of bad) console.log(`- ${S(r[0])}@${S(r[1])} [${S(r[2])}] ${S(r[3])}: ${S(r[4])}`);
  }
}

function cmdSummary(a) {
  const db = connect(a.indexDir);
  if (a.action === "list") {
    for (const r of db.rows("SELECT s.package_id, s.version, s.model, s.written_at, s.source_sha256 = k.sha256 FROM ai_summary s LEFT JOIN package k USING (package_id, version)")) {
      console.log(`${r[0]}@${r[1]}  ${r[2]}  ${r[3]}  ${r[4] ? "current" : "STALE"}`);
    }
    return;
  }
  if (!(a.package && a.version && a.model && a.file)) throw new Exit("summary set needs --package --version --model --file");
  const k = db.one("SELECT sha256 FROM package WHERE package_id=? AND version=?", a.package, a.version);
  if (!k) throw new Exit("package not in index");
  const text = fs.readFileSync(a.file, "utf8");
  db.run("INSERT OR REPLACE INTO ai_summary VALUES (?,?,?,?,?,?)", a.package, a.version, a.model, text, k[0], now());
  db.commit();
  console.log("stored (labelled non-authoritative)");
}

// ---------------------------------------------------------------------------- CLI

const COMMANDS = {
  crawl: {
    help: "read Orchestrator metadata and download deployed packages",
    fn: cmdCrawl,
    options: {
      uip: { type: "string", help: "path to the uip CLI" }, profile: { type: "string", help: "uip login profile" },
      "jobs-days": { type: "string", default: "90", int: true, help: "job window in days (default 90)" },
      "max-jobs": { type: "string", default: String(200 * 1000), int: true, help: "stop reading jobs after this many" },
      "skip-jobs": { type: "boolean" }, "skip-libraries": { type: "boolean" },
      "resolve-identities": { type: "boolean", help: "one `jobs get` per distinct identity to read the Orchestrator robot identity" },
    },
  },
  extract: {
    help: "parse packages into the index",
    fn: cmdExtract,
    options: {
      nupkg: { type: "string", multiple: true, help: "a .nupkg file, or a folder searched recursively for them (repeatable)" },
      "nupkg-dir": { type: "string", multiple: true, help: "alias of --nupkg" },
      source: { type: "string", multiple: true, help: "a project folder, or any tree (repo, solution, monorepo) searched for project.json (repeatable)" },
      force: { type: "boolean", help: "re-read packages already indexed" },
    },
  },
  analyze: {
    help: "run uip rpa analyze on throwaway copies",
    fn: cmdAnalyze,
    options: {
      uip: { type: "string" }, profile: { type: "string" }, "policy-file": { type: "string" },
      "policy-file-type": { type: "string", default: "AutomationOps", choices: ["Default", "AutomationOps", "Studio"] },
      "governance-product": { type: "string" }, only: { type: "string", help: "glob on package id" },
      timeout: { type: "string", default: "900", int: true }, force: { type: "boolean" },
    },
  },
  review: {
    help: "compute findings and write a report",
    fn: cmdReview,
    options: { restrictions: { type: "string", help: "JSON allow/deny list for applications and URLs" }, "max-rows": { type: "string", default: "200", int: true } },
  },
  cards: { help: "write per-automation Markdown cards and INDEX.md", fn: cmdCards, options: {} },
  query: {
    help: "read-only SQL",
    fn: cmdQuery,
    positional: "sql",
    options: { format: { type: "string", default: "table", choices: ["table", "json", "csv"] }, limit: { type: "string", default: "500", int: true } },
  },
  status: { help: "coverage report", fn: cmdStatus, options: {} },
  summary: {
    help: "store or list AI-written summaries",
    fn: cmdSummary,
    positional: "action",
    positionalChoices: ["set", "list"],
    options: { package: { type: "string" }, version: { type: "string" }, model: { type: "string" }, file: { type: "string" } },
  },
};

function usage() {
  const lines = ["usage: automation-index.mjs [--index-dir DIR] <command> [options]", "",
    "Read-only, deterministic inventory of UiPath automations.", "", "commands:"];
  for (const [name, c] of Object.entries(COMMANDS)) lines.push(`  ${name.padEnd(9)} ${c.help}`);
  lines.push("", "options:", "  --index-dir DIR   index folder (default ./automation-index)", "",
    "Run `automation-index.mjs <command> --help` for a command's options.");
  return lines.join("\n");
}

function commandUsage(name) {
  const c = COMMANDS[name];
  const lines = [`usage: automation-index.mjs [--index-dir DIR] ${name}${c.positional ? ` <${c.positional}>` : ""} [options]`, "", c.help, ""];
  for (const [opt, spec] of Object.entries(c.options)) {
    const arg = spec.type === "string" ? " VALUE" : "";
    lines.push(`  --${opt}${arg}`.padEnd(30) + (spec.help || "") + (spec.choices ? ` (${spec.choices.join("|")})` : ""));
  }
  return lines.join("\n");
}

function parseCli(argv) {
  let indexDir = "automation-index";
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--index-dir") {
      if (i + 1 >= argv.length) throw new Exit("--index-dir needs a value");
      indexDir = argv[++i];
    } else if (argv[i].startsWith("--index-dir=")) indexDir = argv[i].slice(12);
    else rest.push(argv[i]);
  }
  const cmdIdx = rest.findIndex((x) => !x.startsWith("-"));
  if (cmdIdx < 0 || rest.slice(0, cmdIdx).some((x) => x === "-h" || x === "--help")) {
    console.log(usage());
    process.exit(cmdIdx < 0 && !rest.some((x) => x === "-h" || x === "--help") ? 2 : 0);
  }
  const name = rest[cmdIdx];
  const cmd = COMMANDS[name];
  if (!cmd) throw new Exit(`unknown command '${name}'\n\n${usage()}`);
  const args = [...rest.slice(0, cmdIdx), ...rest.slice(cmdIdx + 1)];
  if (args.includes("-h") || args.includes("--help")) {
    console.log(commandUsage(name));
    process.exit(0);
  }
  const options = {};
  for (const [k, v] of Object.entries(cmd.options)) options[k] = { type: v.type, ...(v.multiple ? { multiple: true } : {}) };
  let parsed;
  try {
    parsed = parseArgs({ args, options, allowPositionals: true, strict: true });
  } catch (e) {
    throw new Exit(`${e.message}\n\n${commandUsage(name)}`);
  }
  const out = { indexDir, cmd: name, fn: cmd.fn };
  for (const [k, spec] of Object.entries(cmd.options)) {
    let v = parsed.values[k];
    if (v === undefined) v = spec.default;
    if (spec.type === "boolean") v = !!v;
    if (v !== undefined && spec.int) {
      if (!/^-?\d+$/.test(String(v))) throw new Exit(`--${k} must be an integer`);
      v = parseInt(v, 10);
    }
    if (v !== undefined && spec.choices && !spec.choices.includes(v)) throw new Exit(`--${k} must be one of ${spec.choices.join(", ")}`);
    out[k.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v ?? null;
  }
  if (name === "extract") out.nupkg = [...(out.nupkg || []), ...(out.nupkgDir || [])];
  const pos = parsed.positionals;
  if (cmd.positional) {
    if (pos.length !== 1) throw new Exit(`${name} needs exactly one <${cmd.positional}>\n\n${commandUsage(name)}`);
    if (cmd.positionalChoices && !cmd.positionalChoices.includes(pos[0])) throw new Exit(`${cmd.positional} must be one of ${cmd.positionalChoices.join(", ")}`);
    out[cmd.positional] = pos[0];
  } else if (pos.length) {
    throw new Exit(`unexpected argument '${pos[0]}'\n\n${commandUsage(name)}`);
  }
  return out;
}

export { parseXml, iterNodes, scrub, dbTarget, resolveVersion, normHost, normApp, parseSelector, classifyAttr, Zip, readXlsx, fnmatch, normpath, unquote, safeRel };

const isMain = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) try {
  const a = parseCli(process.argv.slice(2));
  a.fn(a);
} catch (e) {
  if (e instanceof Exit) {
    process.stderr.write(e.message + "\n");
    process.exit(1);
  }
  process.stderr.write(`error: ${e && e.message ? e.message : e}\n`);
  if (process.env.AUTOMATION_INDEX_DEBUG) process.stderr.write((e && e.stack) + "\n");
  process.exit(1);
}
