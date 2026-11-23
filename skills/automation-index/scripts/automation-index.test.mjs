// Regression tests for automation-index.mjs, built from synthetic packages only.
// Run: node --test scripts/automation-index.test.mjs
// On Node 22.5-22.12 node:sqlite needs a flag: node --experimental-sqlite --test scripts/automation-index.test.mjs
// Set AUTOMATION_INDEX_SCRIPT to test another copy of the indexer.

import { test, before, describe } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { DatabaseSync } from "node:sqlite";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = process.env.AUTOMATION_INDEX_SCRIPT || path.join(HERE, "automation-index.mjs");
const SECRET = "Sup3rS3cretValue";

const NS = 'xmlns="http://schemas.microsoft.com/netfx/2009/xaml/activities" ' +
  'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" ' +
  'xmlns:sap2010="http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation" ' +
  'xmlns:ui="http://schemas.uipath.com/workflow/activities" ' +
  'xmlns:uix="http://schemas.uipath.com/workflow/activities/uix"';

const MAIN = `<Activity x:Class="Main" ${NS}>
  <Sequence sap2010:WorkflowViewState.IdRef="Sequence_1">
    <ui:InvokeWorkflowFile DisplayName="Add item" WorkflowFileName="Framework\\Sub.xaml" sap2010:WorkflowViewState.IdRef="InvokeWorkflowFile_1">
      <ui:InvokeWorkflowFile.Arguments>
        <InArgument x:TypeArguments="x:String" x:Key="in_Queue">[Config("QueueKey").ToString]</InArgument>
      </ui:InvokeWorkflowFile.Arguments>
    </ui:InvokeWorkflowFile>
    <ui:GetRobotCredential AssetName="Cred_Portal" DisplayName="Get portal login" sap2010:WorkflowViewState.IdRef="GetRobotCredential_1" />
    <uix:NApplicationCard DisplayName="Use Portal" sap2010:WorkflowViewState.IdRef="NApplicationCard_1">
      <uix:NApplicationCard.TargetApp>
        <uix:TargetApp BrowserType="Edge" Url="https://portal.example.com/login" Title="Portal" />
      </uix:NApplicationCard.TargetApp>
    </uix:NApplicationCard>
    <uix:NClick DisplayName="Click submit" sap2010:WorkflowViewState.IdRef="NClick_1">
      <uix:NClick.Target>
        <uix:TargetAnchorable FullSelectorArgument="&lt;html app='msedge.exe' title='Portal' url='https://portal.example.com/*' /&gt;&lt;webctrl id='submit' /&gt;" />
      </uix:NClick.Target>
    </uix:NClick>
    <uix:NClick DisplayName="Click desktop" sap2010:WorkflowViewState.IdRef="NClick_2" >
      <uix:NClick.Target>
        <uix:TargetAnchorable ScopeSelectorArgument="&lt;wnd app='legacyerp.exe' title='ERP' /&gt;" />
      </uix:NClick.Target>
    </uix:NClick>
    <ui:HttpClient DisplayName="Call API" EndPoint="https://api.example.org/v1/orders" Method="GET" sap2010:WorkflowViewState.IdRef="HttpClient_1" />
    <ui:DatabaseConnect DisplayName="Connect" ProviderName="System.Data.SqlClient" ConnectionString="Server=db01.example.net;Database=Orders;User Id=svc;Password=${SECRET};" sap2010:WorkflowViewState.IdRef="DatabaseConnect_1" />
    <ui:InvokeWorkflowFile DisplayName="Dynamic" WorkflowFileName="[in_Name]" sap2010:WorkflowViewState.IdRef="InvokeWorkflowFile_2" />
  </Sequence>
</Activity>`;

const SUB = `<Activity x:Class="Sub" ${NS}>
  <Sequence sap2010:WorkflowViewState.IdRef="Sequence_2">
    <ui:AddQueueItem DisplayName="Add queue item" QueueName="[in_Queue]" sap2010:WorkflowViewState.IdRef="AddQueueItem_1" />
  </Sequence>
</Activity>`;

const DEAD = `<Activity x:Class="Dead" ${NS}>
  <Sequence sap2010:WorkflowViewState.IdRef="Sequence_3">
    <ui:GetRobotAsset AssetName="Never_Used_Asset" sap2010:WorkflowViewState.IdRef="GetRobotAsset_9" />
  </Sequence>
</Activity>`;

const PROJECT = JSON.stringify({
  name: "Synthetic.Process", main: "Main.xaml", targetFramework: "Windows", expressionLanguage: "VisualBasic",
  studioVersion: "25.10.0", dependencies: { "UiPath.System.Activities": "[25.10.0]", "Shared.Library": "[1.2.0]" },
  designOptions: { outputType: "Process" },
});

const BINDINGS = JSON.stringify({ version: "2.0", resources: [{ resource: "asset", key: "Shared.Bound_Asset",
  value: { name: { defaultValue: "Bound_Asset" }, folderPath: { defaultValue: "Shared" } }, metadata: { ActivityName: "Get bound asset" } }] });

const APPMETA = JSON.stringify({ Version: "1.0", Metadata: [{ ApplicationType: "WindowsApplication",
  ExecutablePath: { Value: "legacyerp.exe", IsExpression: false }, Url: null }] });

const SIMPLE = `<Activity x:Class="Main" ${NS}>
  <ui:GetRobotAsset AssetName="ASSET_NAME" sap2010:WorkflowViewState.IdRef="GetRobotAsset_1" />
</Activity>`;

/** Minimal zip writer: [[name, Buffer|string], ...] -> Buffer (deflate). */
export function zip(entries) {
  const locals = [];
  const central = [];
  let offset = 0;
  for (const [name, content] of entries) {
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8");
    const comp = zlib.deflateRawSync(data);
    const nameBuf = Buffer.from(name, "utf8");
    const crc = zlib.crc32(data);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(0x800, 6); lh.writeUInt16LE(8, 8);
    lh.writeUInt32LE(crc, 14); lh.writeUInt32LE(comp.length, 18); lh.writeUInt32LE(data.length, 22); lh.writeUInt16LE(nameBuf.length, 26);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(20, 4); ch.writeUInt16LE(20, 6); ch.writeUInt16LE(0x800, 8); ch.writeUInt16LE(8, 10);
    ch.writeUInt32LE(crc, 16); ch.writeUInt32LE(comp.length, 20); ch.writeUInt32LE(data.length, 24); ch.writeUInt16LE(nameBuf.length, 28);
    ch.writeUInt32LE(offset, 42);
    locals.push(lh, nameBuf, comp);
    central.push(ch, nameBuf);
    offset += 30 + nameBuf.length + comp.length;
  }
  const cd = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(cd.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}

export function xlsx(sheets) {
  const shared = [];
  const idx = new Map();
  const sid = (v) => {
    if (!idx.has(v)) { idx.set(v, shared.length); shared.push(v); }
    return idx.get(v);
  };
  const files = [];
  const wb = [];
  const rels = [];
  Object.entries(sheets).forEach(([name, rows], i) => {
    const n = i + 1;
    wb.push(`<sheet name="${name}" sheetId="${n}" r:id="rId${n}"/>`);
    rels.push(`<Relationship Id="rId${n}" Target="worksheets/sheet${n}.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>`);
    const body = rows.map((row, r) => `<row r="${r + 1}">` + row.map((v, c) => `<c r="${String.fromCharCode(65 + c)}${r + 1}" t="s"><v>${sid(v)}</v></c>`).join("") + "</row>").join("");
    files.push([`xl/worksheets/sheet${n}.xml`, `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${body}</sheetData></worksheet>`]);
  });
  files.push(["xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + wb.join("") + "</sheets></workbook>"]);
  files.push(["xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + rels.join("") + "</Relationships>"]);
  files.push(["xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + shared.map((s) => `<si><t>${s}</t></si>`).join("") + "</sst>"]);
  return zip(files);
}

export function nupkg(file, pid, ver, files, dlls = {}) {
  const entries = [[`${pid}.nuspec`, `<package><metadata><id>${pid}</id><version>${ver}</version></metadata></package>`]];
  for (const [rel, data] of Object.entries(files)) entries.push(["content/" + rel, data]);
  for (const [rel, data] of Object.entries(dlls)) entries.push([rel, data]);
  fs.writeFileSync(file, zip(entries));
}

export function run(index, ...args) {
  return spawnSync(process.execPath, [SCRIPT, "--index-dir", index, ...args], { encoding: "utf8" });
}

function write(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text);
}

const openDb = (index) => new DatabaseSync(path.join(index, "index.db"));

describe("extraction from a synthetic package", () => {
  let tmp, index, db;
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-test-"));
    index = path.join(tmp, "index");
    const supplied = path.join(tmp, "supplied");
    fs.mkdirSync(supplied);
    const config = xlsx({
      Settings: [["Name", "Value"], ["QueueKey", "Q_Invoices"], ["ApiPassword", SECRET], ["PortalUrl", "https://portal.example.com"],
        ["ReportDb", `Server=rpt.example.net;Database=Rpt;Password=${SECRET}`]],
      Assets: [["Name", "Asset"], ["OrchUrl", "Url_Asset"]],
    });
    nupkg(path.join(supplied, "Synthetic.Process.1.0.0.nupkg"), "Synthetic.Process", "1.0.0", {
      "project.json": PROJECT, "Main.xaml": MAIN, "Framework/Sub.xaml": SUB, "Dead.xaml": DEAD,
      "Data/Config.xlsx": config, "bindings_v2.json": BINDINGS, "applicationsMetadata.json": APPMETA,
    });
    const u16 = (s) => Buffer.from(s, "utf16le");
    const dll = Buffer.concat([u16("https://compiled.example.com/api"), Buffer.alloc(2), u16("/api/orders/submit"), Buffer.alloc(2),
      u16("Compiled_Process.Framework.InitAllSettings")]);
    nupkg(path.join(supplied, "Compiled.Process.2.0.0.nupkg"), "Compiled.Process", "2.0.0", {
      "project.json": JSON.stringify({ name: "Compiled.Process", main: "Main.xaml", targetFramework: "Portable" }), "bindings_v2.json": BINDINGS,
    }, { "lib/net8.0/Compiled.Process.dll": dll });
    let r = run(index, "extract", "--nupkg", supplied);
    assert.equal(r.status, 0, r.stderr);
    r = run(index, "review");
    assert.equal(r.status, 0, r.stderr);
    db = openDb(index);
  });

  const edges = (kind, pkg = "Synthetic.Process") =>
    db.prepare("SELECT value, normalized, extractor, confidence, detail FROM edge WHERE package_id=? AND kind=?").all(pkg, kind);
  const norms = (kind, pkg) => new Set(edges(kind, pkg).map((r) => r.normalized));

  test("applications from selectors, TargetApp and Studio metadata", () => {
    assert.ok(norms("uses_application").has("msedge.exe"));
    assert.ok(norms("uses_application").has("legacyerp.exe"));
    const ex = new Set(edges("uses_application").filter((r) => r.normalized === "legacyerp.exe").map((r) => r.extractor));
    assert.ok(ex.has("studio_applications_metadata"));
    assert.ok(ex.has("xaml:selector"));
  });

  test("URLs are normalized to hosts", () => {
    assert.ok(norms("targets_url").has("portal.example.com"));
    assert.ok(norms("calls_api").has("api.example.org"));
  });

  test("static credential and bound asset", () => {
    assert.ok(norms("reads_credential").has("cred_portal"));
    assert.ok(norms("reads_asset").has("bound_asset"));
  });

  test("Config Assets sheet values are read as asset names", () => {
    const rows = edges("reads_asset").filter((r) => r.normalized === "url_asset");
    assert.equal(rows[0]?.extractor, "config_assets_sheet");
  });

  test("queue traced through argument and Config", () => {
    const rows = edges("uses_queue").filter((r) => r.normalized === "q_invoices");
    assert.ok(rows.length, JSON.stringify(edges("uses_queue")));
    assert.match(rows[0].extractor, /trace/);
    assert.match(rows[0].detail, /Config 'QueueKey'/);
  });

  test("database target without credentials", () => {
    assert.ok(norms("uses_database").has("db01.example.net/orders"));
  });

  test("secrets are never stored in clear", () => {
    for (const { name: table } of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()) {
      for (const { name: col } of db.prepare(`PRAGMA table_info(${table})`).all()) {
        const hit = db.prepare(`SELECT count(*) n FROM ${table} WHERE CAST(${col} AS TEXT) LIKE ?`).get(`%${SECRET}%`).n;
        assert.equal(hit, 0, `secret found in ${table}.${col}`);
      }
    }
  });

  test("hard-coded secrets are findings", () => {
    const details = edges("hardcoded_secret").map((r) => r.detail);
    assert.ok(details.some((d) => d.includes("connection string")), details.join("|"));
    assert.ok(details.some((d) => d.includes("ApiPassword")), details.join("|"));
    assert.ok(db.prepare("SELECT count(*) n FROM finding WHERE rule='AI-SEC-001'").get().n >= 2);
  });

  test("reachability", () => {
    const rows = Object.fromEntries(db.prepare("SELECT path, reachable FROM workflow WHERE package_id='Synthetic.Process'").all().map((r) => [r.path, r.reachable]));
    assert.equal(rows["Main.xaml"], 1);
    assert.equal(rows["Framework/Sub.xaml"], 1);
    assert.equal(rows["Dead.xaml"], null); // Main has a dynamic invoke: unknown, not dead
  });

  test("every edge has provenance", () => {
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE file IS NULL OR extractor IS NULL OR confidence IS NULL").get().n, 0);
  });

  test("compiled package is marked and low confidence", () => {
    assert.equal(db.prepare("SELECT status FROM coverage WHERE package_id='Compiled.Process' AND stage='extract'").get().status, "compiled_only");
    assert.deepEqual([...new Set(edges("calls_api", "Compiled.Process").map((r) => r.confidence))], ["low"]);
    assert.ok(norms("reads_asset", "Compiled.Process").has("bound_asset"));
    assert.ok(db.prepare("SELECT count(*) n FROM workflow WHERE package_id='Compiled.Process' AND kind='compiled'").get().n > 0);
  });

  test("query is read-only", () => {
    const edgesBefore = db.prepare("SELECT count(*) n FROM edge").get().n;
    const stray = path.join(tmp, "vacuum-copy.db");
    // Layer 1: statement filter.
    for (const sql of ["DELETE FROM edge", "SELECT 1; DELETE FROM edge", `VACUUM INTO '${stray}'`, `ATTACH '${stray}' AS x`,
      "PRAGMA query_only = OFF", "DROP TABLE edge"]) {
      const r = run(index, "query", sql);
      assert.notEqual(r.status, 0, sql);
      assert.match(r.stderr, /read-only|one statement/, sql);
    }
    // Layer 2: SQLite itself, for a write that passes the filter.
    const r = run(index, "query", "WITH x AS (SELECT 1) INSERT INTO meta VALUES ('k', 'v')");
    assert.notEqual(r.status, 0);
    assert.match(r.stderr.replace(/\s/g, "").toLowerCase(), /readonly/);
    assert.equal(db.prepare("SELECT count(*) n FROM edge").get().n, edgesBefore);
    assert.equal(db.prepare("SELECT count(*) n FROM meta WHERE key='k'").get().n, 0);
    assert.ok(!fs.existsSync(stray));
    // Reads still work, including ';' inside strings and comments.
    const ok = run(index, "query", "/* c */ SELECT ';' AS s, count(*) FROM edge -- x; y", "--format", "json");
    assert.equal(ok.status, 0, ok.stderr);
    assert.equal(JSON.parse(ok.stdout)[0].s, ";");
  });

  test("restrictions", () => {
    const pol = path.join(tmp, "restrict.json");
    fs.writeFileSync(pol, JSON.stringify({ applications: { allow: ["msedge.exe"] }, urls: { deny: ["*.example.org"] } }));
    const r = run(index, "review", "--restrictions", pol);
    assert.equal(r.status, 0, r.stderr);
    const subjects = new Set(openDb(index).prepare("SELECT subject FROM finding WHERE rule='AI-GOV-001'").all().map((x) => x.subject));
    assert.ok(subjects.has("legacyerp.exe"));
    assert.ok(subjects.has("api.example.org"));
    assert.ok(!subjects.has("msedge.exe"));
  });

  test("cards", () => {
    const r = run(index, "cards");
    assert.equal(r.status, 0, r.stderr);
    assert.match(fs.readFileSync(path.join(index, "INDEX.md"), "utf8"), /Synthetic\.Process/);
  });

  test("helpers", async () => {
    const m = await import(pathToFileURL(SCRIPT).href);
    assert.ok(!m.scrub("Server=a;Password=pw123;").includes("pw123"));
    assert.ok(!m.scrub("https://user:pw123@host/x").includes("pw123"));
    assert.equal(m.resolveVersion("[1.2.3]"), "1.2.3");
    assert.equal(m.resolveVersion("[1.0, 2.0)"), null);
  });
});

describe("layouts: one project, a repo of many, nested solutions", () => {
  let tmp, index, db;
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-layout-"));
    const repo = path.join(tmp, "repo");
    const project = (rel, name, asset, deps = {}) => {
      write(path.join(repo, rel, "project.json"), JSON.stringify({ name, main: "Main.xaml", targetFramework: "Windows", dependencies: deps }));
      write(path.join(repo, rel, "Main.xaml"), SIMPLE.replace("ASSET_NAME", asset));
    };
    write(path.join(repo, "SolutionA", "SolutionA.uipx"), "{}");
    project("SolutionA/Dispatcher", "Dispatcher", "A_Dispatcher", { "Shared.Library": "[1.0.0]" });
    project("SolutionA/Performer", "Performer", "A_Performer");
    write(path.join(repo, "nested", "deeper", "SolutionB", "SolutionB.uipx"), "{}");
    project("nested/deeper/SolutionB/copy1", "Duplicate", "Dup_One");
    project("nested/deeper/SolutionB/copy2", "Duplicate", "Dup_Two");
    project("outer", "Outer", "Outer_Asset");
    project("outer/inner", "Inner", "Inner_Asset");
    write(path.join(repo, "libs", "Shared.Library", "project.json"), JSON.stringify({ name: "Shared.Library", main: "Login.xaml", targetFramework: "Windows", designOptions: { outputType: "Library" } }));
    write(path.join(repo, "libs", "Shared.Library", "Login.xaml"), `<Activity x:Class="Login" ${NS}>\n  <ui:GetRobotCredential AssetName="Lib_Credential" sap2010:WorkflowViewState.IdRef="GetRobotCredential_7" />\n</Activity>`);
    write(path.join(repo, "agents", "Helper", "agent.json"), "{}");
    write(path.join(repo, "agents", "Helper", "project.uiproj"), "{}");
    index = path.join(tmp, "index");
    const r = run(index, "extract", "--source", repo);
    assert.equal(r.status, 0, r.stderr);
    db = openDb(index);
  });

  test("every project indexed once", () => {
    const names = db.prepare("SELECT package_id FROM package WHERE kind <> 'other' ORDER BY package_id").all().map((r) => r.package_id);
    assert.deepEqual(names, ["Dispatcher", "Duplicate", "Duplicate", "Inner", "Outer", "Performer", "Shared.Library"]);
  });

  test("duplicate names kept apart", () => {
    assert.equal(new Set(db.prepare("SELECT version FROM package WHERE package_id='Duplicate'").all().map((r) => r.version)).size, 2);
    assert.deepEqual(new Set(db.prepare("SELECT normalized FROM edge WHERE package_id='Duplicate'").all().map((r) => r.normalized)), new Set(["dup_one", "dup_two"]));
  });

  test("nested project not counted in outer", () => {
    assert.deepEqual(db.prepare("SELECT DISTINCT normalized FROM edge WHERE package_id='Outer'").all().map((r) => r.normalized), ["outer_asset"]);
    assert.deepEqual(db.prepare("SELECT path FROM workflow WHERE package_id='Outer'").all().map((r) => r.path), ["Main.xaml"]);
  });

  test("solution recorded", () => {
    const sol = Object.fromEntries(db.prepare("SELECT package_id, solution FROM package WHERE package_id IN ('Dispatcher','Outer')").all().map((r) => [r.package_id, r.solution]));
    assert.match(sol.Dispatcher, /SolutionA$/);
    assert.equal(sol.Outer, null);
    const dup = [...new Set(db.prepare("SELECT solution FROM package WHERE package_id='Duplicate'").all().map((r) => r.solution))];
    assert.equal(dup.length, 1);
    assert.match(dup[0], /nested\/deeper\/SolutionB$/);
  });

  test("non-RPA project reported, not skipped", () => {
    assert.equal(db.prepare("SELECT status FROM coverage WHERE package_id='Helper'").get().status, "unsupported");
  });

  test("library from source is inherited", () => {
    const rows = db.prepare("SELECT normalized, via FROM v_edge_all WHERE root_package_id='Dispatcher'").all();
    assert.ok(rows.some((r) => r.normalized === "lib_credential" && r.via.startsWith("Shared.Library@source")));
  });

  test("single nupkg file", () => {
    const pkg = path.join(tmp, "One.Process.1.0.0.nupkg");
    nupkg(pkg, "One.Process", "1.0.0", { "project.json": JSON.stringify({ name: "One.Process", main: "Main.xaml" }), "Main.xaml": SIMPLE.replace("ASSET_NAME", "One_Asset") });
    const idx = path.join(tmp, "index-one");
    const r = run(idx, "extract", "--nupkg", pkg);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(openDb(idx).prepare("SELECT package_id, version FROM package").all().map((x) => ({ ...x })), [{ package_id: "One.Process", version: "1.0.0" }]);
  });

  test("re-extract is incremental and stable", () => {
    const before = db.prepare("SELECT count(*) n FROM edge").get().n;
    const r = run(index, "extract", "--source", path.join(tmp, "repo"));
    assert.equal(r.status, 0, r.stderr);
    const db2 = openDb(index);
    assert.equal(db2.prepare("SELECT count(*) n FROM edge").get().n, before);
    assert.equal(db2.prepare("SELECT count(*) n FROM package").get().n, 8);
  });
});

describe("hostile and broken input", () => {
  let tmp, index, db, result;
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-hostile-"));
    const pk = path.join(tmp, "pkgs");
    fs.mkdirSync(pk);
    const ok = SIMPLE.replace("ASSET_NAME", "Fine_Asset");
    const pj = (n) => JSON.stringify({ name: n, main: "Main.xaml" });
    const spec = (id) => [`${id}.nuspec`, `<package><metadata><id>${id}</id><version>1.0.0</version></metadata></package>`];
    fs.writeFileSync(path.join(pk, "slip.nupkg"), zip([spec("Slip"), ["content/project.json", pj("Slip")], ["content/Main.xaml", ok],
      ["content/../../../escaped.txt", "X"], ["content/..%2f..%2fescaped2.txt", "X"], ["/abs-escaped.txt", "X"], ["content/C:/escaped4.txt", "X"]]));
    fs.writeFileSync(path.join(pk, "empty.nupkg"), Buffer.alloc(0));
    fs.writeFileSync(path.join(pk, "notzip.nupkg"), "not a zip");
    const bl = `<!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><Activity ${NS}><ui:GetRobotAsset AssetName="&b;"/></Activity>`;
    const deep = "<a>".repeat(100 * 1000) + "</a>".repeat(100 * 1000);
    fs.writeFileSync(path.join(pk, "xml.nupkg"), zip([spec("Xml"), ["content/project.json", pj("Xml")], ["content/Main.xaml", ok],
      ["content/laughs.xaml", bl], ["content/deep.xaml", deep], ["content/broken.xaml", "<Activity><a></Activity>"],
      ["content/utf16.xaml", Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(`<Activity ${NS}><ui:GetRobotAsset AssetName="Wide_Asset"/></Activity>`, "utf16le")])]]));
    const src = path.join(tmp, "src", "P1");
    fs.mkdirSync(src, { recursive: true });
    fs.writeFileSync(path.join(src, "project.json"), pj("P1"));
    fs.writeFileSync(path.join(src, "Main.xaml"), ok);
    fs.writeFileSync(path.join(src, "inside.txt"), "INSIDE");
    fs.writeFileSync(path.join(tmp, "outside.txt"), "OUTSIDE_SECRET");
    fs.symlinkSync(path.join(tmp, "outside.txt"), path.join(src, "leak.txt"));
    fs.symlinkSync(path.join(src, "inside.txt"), path.join(src, "inner-link.txt"));
    fs.symlinkSync(path.join(tmp, "src"), path.join(src, "loop"));
    index = path.join(tmp, "index");
    const t0 = Date.now();
    result = run(index, "extract", "--nupkg", pk, "--source", path.join(tmp, "src"));
    result.seconds = (Date.now() - t0) / 1000;
    db = openDb(index);
  });

  test("the run survives and finishes quickly", () => {
    assert.equal(result.status, 0, result.stderr);
    assert.ok(result.seconds < 20, `took ${result.seconds}s`);
  });

  test("nothing escapes the index folder", () => {
    for (const f of ["escaped.txt", "escaped2.txt", "abs-escaped.txt", "escaped4.txt"]) {
      assert.ok(!fs.existsSync(path.join(tmp, f)) && !fs.existsSync(path.join(path.dirname(tmp), f)) && !fs.existsSync("/" + f), f);
    }
    const hits = [];
    const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) walk(p); else if (/escaped/.test(e.name)) hits.push(p); } };
    walk(index);
    assert.deepEqual(hits, []);
  });

  test("symlinks never pull files from outside the project", () => {
    const tree = fs.readdirSync(path.join(index, "src")).map((d) => path.join(index, "src", d)).find((d) => d.includes("P1"));
    const files = fs.readdirSync(tree);
    assert.ok(!files.includes("leak.txt"));
    assert.ok(files.includes("inner-link.txt")); // a link that stays inside the project is fine
    const grep = (d) => fs.readdirSync(d, { withFileTypes: true }).some((e) => { const p = path.join(d, e.name); return e.isDirectory() ? grep(p) : fs.readFileSync(p, "utf8").includes("OUTSIDE_SECRET"); });
    assert.ok(!grep(index.replace(/index$/, "index/src")));
  });

  test("review rows keep their columns aligned (rules select NULL more than once)", () => {
    const r = run(index, "review");
    assert.equal(r.status, 0, r.stderr);
    const f = openDb(index).prepare("SELECT package_id, version, subject, detail FROM finding WHERE rule='AI-COV-001' AND package_id='empty.nupkg'").get();
    assert.equal(f.version, "?");
    assert.equal(f.subject, "empty.nupkg@?");
    assert.match(f.detail, /^parse_error: /);
    // AI-RES-004 selects NULL, NULL, NULL, name, detail, NULL: the subject must be the asset name.
    const w = openDb(index);
    w.prepare("INSERT INTO orch_asset VALUES ('k1', 'f1', 'Shared', 'Unused_Asset_Name', 'Text', 'Global', 0, '')").run();
    w.close();
    const r2 = run(index, "review");
    assert.equal(r2.status, 0, r2.stderr);
    const g = openDb(index).prepare("SELECT package_id, version, subject, detail FROM finding WHERE rule='AI-RES-004'").get();
    assert.equal(g.package_id, null);
    assert.equal(g.version, null);
    assert.equal(g.subject, "Unused_Asset_Name");
    assert.match(g.detail, /^folder Shared; type Text/);
  });

  test("broken packages are recorded, not fatal", () => {
    const st = Object.fromEntries(db.prepare("SELECT package_id, status FROM coverage WHERE stage='extract'").all().map((r) => [r.package_id, r.status]));
    assert.equal(st["empty.nupkg"], "parse_error");
    assert.equal(st["notzip.nupkg"], "parse_error");
    assert.equal(st.Slip, "indexed");
    assert.equal(st.Xml, "indexed");
  });

  test("entities are not expanded and broken XML is a parse error", () => {
    const wf = Object.fromEntries(db.prepare("SELECT path, parse_error FROM workflow WHERE package_id='Xml'").all().map((r) => [r.path, r.parse_error]));
    assert.match(wf["laughs.xaml"], /DTD|entity/i);
    assert.match(wf["broken.xaml"], /mismatched/);
    assert.equal(wf["deep.xaml"], null);
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE value LIKE 'aaaaaaaaaa%'").get().n, 0);
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE normalized='wide_asset'").get().n, 1);
  });
});

describe("regressions found in review", () => {
  const SEC = "Zq9SecretVal";
  let tmp, db, m;
  before(async () => {
    m = await import(pathToFileURL(SCRIPT).href);
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-review-"));
    const main = `<Activity ${NS}><Sequence sap2010:WorkflowViewState.IdRef="S1">
 <ui:InvokeCode Code="Dim a = &quot;https://h.example.com/one&quot;&#xA;Dim b = &quot;https://h.example.com/two&quot;" sap2010:WorkflowViewState.IdRef="IC1"/>
 <ui:InvokeWorkflowFile WorkflowFileName="[&quot;Sub.xaml?pass=${SEC}&quot;]" sap2010:WorkflowViewState.IdRef="IW1"/>
 <ui:HttpClient EndPoint="https://api.example.com/x?client_secret=${SEC}&amp;pass=${SEC}&amp;sig=${SEC}" sap2010:WorkflowViewState.IdRef="H2"/>
</Sequence></Activity>`;
    const cfg = xlsx({ Settings: [["Name", "Value"], ["SAPPass", SEC], ["UserPass_Dev", SEC], ["DbPassphrase", SEC], ["Pin", SEC], ["PassportUrl", "https://p.example.com"]] });
    const cs = `class C {\n var a = system.GetAsset("Asset_One");\n var b = system.GetAsset("Asset_One");\n}`;
    nupkg(path.join(tmp, "p.nupkg"), "P", "1.0.0", { "project.json": JSON.stringify({ name: "P", main: "Main.xaml" }), "Main.xaml": main,
      "Data/Config.xlsx": cfg, "Coded.cs": cs, "appconfig.json": JSON.stringify({ Pass: SEC }),
      "bindings_v2.json": JSON.stringify({ resources: [{ resource: "asset", value: { name: { defaultValue: "B" } }, metadata: { ActivityName: `pwd=${SEC}` } }] }) });
    const r = run(path.join(tmp, "ix"), "extract", "--nupkg", path.join(tmp, "p.nupkg"));
    assert.equal(r.status, 0, r.stderr);
    db = openDb(path.join(tmp, "ix"));
  });

  test("credential-like names and URL parameters are never stored in clear", () => {
    for (const { name: t } of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()) {
      for (const { name: c } of db.prepare(`PRAGMA table_info(${t})`).all()) {
        assert.equal(db.prepare(`SELECT count(*) n FROM ${t} WHERE CAST(${c} AS TEXT) LIKE ?`).get(`%${SEC}%`).n, 0, `${t}.${c}`);
      }
    }
    assert.equal(db.prepare("SELECT value FROM config_key WHERE name='PassportUrl'").get().value, "https://p.example.com"); // not a secret
    assert.ok(m.scrub("a?pass=x1").includes("redacted"));
    for (const t of ['Password="my secret value"', "{'password': 'two words here'}", "pwd=abc def;Server=x", 'Server=s;Password="p;q r";Database=d']) {
      for (const w of ["secret", "value", "words", "def", "q r"]) assert.ok(!(m.scrub(t).includes(w) && t.includes(w)), `${t} -> ${m.scrub(t)}`);
    }
    assert.equal(m.scrub("pwd=abc def;Server=x"), "pwd=<redacted len=7>;Server=x");
    assert.ok(!m.scrub("bypass=yes").includes("redacted"));
  });

  test("distinct values and per-line evidence are kept", () => {
    assert.deepEqual(db.prepare("SELECT value FROM edge WHERE activity LIKE 'IC1%' AND kind='targets_url' ORDER BY value").all().map((r) => r.value),
      ["https://h.example.com/one", "https://h.example.com/two"]);
    assert.deepEqual(db.prepare("SELECT line FROM edge WHERE file='Coded.cs' AND kind='reads_asset' ORDER BY line").all().map((r) => r.line), [2, 3]);
  });

  test("nested namespace declarations stay linear", () => {
    let s = "";
    const n = 20 * 1000;
    for (let i = 0; i < n; i++) s += `<p${i}:e xmlns:p${i}="urn:${i}">`;
    for (let i = n - 1; i >= 0; i--) s += `</p${i}:e>`;
    const t0 = Date.now();
    const root = m.parseXml(Buffer.from(s));
    assert.ok(Date.now() - t0 < 3000, `took ${Date.now() - t0} ms`);
    assert.equal(root.ns, "urn:0");
    assert.throws(() => m.parseXml(Buffer.from('<a xmlns:p="urn:x"><p:b/></a><!-- --><q:c/>')));
    assert.throws(() => m.parseXml(Buffer.from('<a><p:b xmlns:p="urn:x"/><p:c/></a>')), /unbound prefix/); // scope ends with the element
  });
});

describe("second review: findings reproduced as tests", () => {
  const SEC = "DEMO_SECRET";
  let tmp, m;
  const extract = (index, ...args) => {
    const r = run(index, "extract", ...args);
    return r;
  };
  const proj = (dir, name, files, extra = {}) => {
    write(path.join(dir, "project.json"), JSON.stringify({ name, main: "Main.xaml", ...extra }));
    for (const [rel, txt] of Object.entries(files)) write(path.join(dir, rel), txt);
  };
  const act = (body) => `<Activity ${NS}>${body}</Activity>`;
  before(async () => {
    m = await import(pathToFileURL(SCRIPT).href);
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-r2-"));
  });

  test("F1 secrets in metadata, quoted values, encoded names and secret properties are redacted", () => {
    const d = path.join(tmp, "f1");
    fs.mkdirSync(d, { recursive: true });
    nupkg(path.join(d, "p.nupkg"), "F1", "1.0.0", {
      "project.json": JSON.stringify({ name: "F1", main: "Main.xaml", description: `Password="${SEC}"` }),
      "Main.xaml": act(`<ui:HttpClient EndPoint="https://a.example.com/?%74oken=${SEC}" sap2010:WorkflowViewState.IdRef="H1"/>
        <ui:SendMail Password="https://${SEC}.example.com/" sap2010:WorkflowViewState.IdRef="M1"/>`),
      "bindings_v2.json": JSON.stringify({ resources: [{ resource: "asset", value: { name: { defaultValue: "B" } }, metadata: { Password: SEC, ActivityName: "x" } }] }),
    });
    const r = extract(path.join(d, "ix"), "--nupkg", path.join(d, "p.nupkg"));
    assert.equal(r.status, 0, r.stderr);
    const db = openDb(path.join(d, "ix"));
    for (const { name: t } of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()) {
      for (const { name: c } of db.prepare(`PRAGMA table_info(${t})`).all()) {
        assert.equal(db.prepare(`SELECT count(*) n FROM ${t} WHERE CAST(${c} AS TEXT) LIKE ?`).get(`%${SEC}%`).n, 0, `${t}.${c}`);
      }
    }
  });

  test("F2 a symlinked folder inside the index never redirects writes outside it", () => {
    const d = path.join(tmp, "f2");
    const outside = path.join(d, "outside");
    fs.mkdirSync(outside, { recursive: true });
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    fs.mkdirSync(path.join(d, "ix"), { recursive: true });
    fs.symlinkSync(outside, path.join(d, "ix", "src"));
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    assert.deepEqual(fs.readdirSync(outside), []);
  });

  test("F3 the dependency closure has one row per reachable package, even with diamonds and cycles", () => {
    const d = path.join(tmp, "f3");
    const lib = (n, deps, asset) => proj(path.join(d, "repo", n), n, asset ? { "Main.xaml": SIMPLE.replace("ASSET_NAME", asset) } : {}, { dependencies: deps });
    lib("A", { B: "[1.0.0]", C: "[1.0.0]" });
    lib("B", { D: "[1.0.0]" });
    lib("C", { D: "[1.0.0]" });
    lib("D", {}, "D_Asset");
    lib("X", { Y: "[1.0.0]" });
    lib("Y", { X: "[1.0.0]" });
    const r = extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    assert.equal(r.status, 0, r.stderr);
    const db = openDb(path.join(d, "ix"));
    assert.equal(db.prepare("SELECT count(*) n FROM v_edge_all WHERE root_package_id='A' AND normalized='d_asset'").get().n, 1);
    assert.ok(db.prepare("SELECT count(*) n FROM dep_closure WHERE root_id='X'").get().n <= 2);
  });

  test("F4 size limits cover every archive read and spreadsheet dimensions", () => {
    const d = path.join(tmp, "f4");
    fs.mkdirSync(d, { recursive: true });
    fs.writeFileSync(path.join(d, "big.nupkg"), zip([["Big.nuspec", Buffer.alloc(51 * 1024 * 1024, 0x20)], ["content/project.json", "{}"]]));
    const wide = zip([["xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Settings" sheetId="1" r:id="rId1"/></sheets></workbook>'],
      ["xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>'],
      ["xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="ZZZZ1" t="inlineStr"><is><t>x</t></is></c></row></sheetData></worksheet>']]);
    nupkg(path.join(d, "wide.nupkg"), "Wide", "1.0.0", { "project.json": JSON.stringify({ name: "Wide" }), "Data/Config.xlsx": wide });
    const t0 = Date.now();
    const r = extract(path.join(d, "ix"), "--nupkg", d);
    assert.equal(r.status, 0, r.stderr);
    assert.ok(Date.now() - t0 < 10000);
    const db = openDb(path.join(d, "ix"));
    assert.match(db.prepare("SELECT detail FROM coverage WHERE package_id='big.nupkg'").get().detail, /limit/i);
    assert.notEqual(db.prepare("SELECT status FROM coverage WHERE package_id='Wide' AND stage='extract'").get().status, "parse_error");
    assert.equal(db.prepare("SELECT count(*) n FROM coverage WHERE package_id='Wide' AND stage LIKE 'config:%'").get().n, 0);
    assert.throws(() => m.readXlsx(Buffer.from("x")));
  });

  test("F5 a long single-line document parses in linear time", () => {
    const s = "<r>" + "<a/>".repeat(400 * 1000) + "</r>";
    const t0 = Date.now();
    const root = m.parseXml(Buffer.from(s));
    assert.ok(Date.now() - t0 < 2500, `took ${Date.now() - t0} ms`);
    assert.equal(root.children.length, 400 * 1000);
  });

  test("F6 query accepts only read-only PRAGMAs", () => {
    const d = path.join(tmp, "f6");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    for (const sql of ["PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA journal_mode", "PRAGMA optimize", "PRAGMA incremental_vacuum"]) {
      const r = run(path.join(d, "ix"), "query", sql);
      assert.notEqual(r.status, 0, sql);
    }
    assert.equal(run(path.join(d, "ix"), "query", "PRAGMA table_info(edge)").status, 0);
  });

  test("F8 renaming a source file re-indexes the project", () => {
    const d = path.join(tmp, "f8");
    const p = path.join(d, "repo", "P");
    proj(p, "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A"), "Sub.xaml": SIMPLE.replace("ASSET_NAME", "B") });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    fs.renameSync(path.join(p, "Sub.xaml"), path.join(p, "Renamed.xaml"));
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const paths = openDb(path.join(d, "ix")).prepare("SELECT path FROM workflow ORDER BY path").all().map((r) => r.path);
    assert.deepEqual(paths, ["Main.xaml", "Renamed.xaml"]);
  });

  test("F9 a changed package drops analyzer results and stale facts", () => {
    const d = path.join(tmp, "f9");
    const p = path.join(d, "repo", "P");
    proj(p, "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    const ix = path.join(d, "ix");
    extract(ix, "--source", path.join(d, "repo"));
    const w = openDb(ix);
    const ver = w.prepare("SELECT version FROM package WHERE package_id='P'").get().version;
    w.prepare("INSERT INTO analyzer_run VALUES ('P', ?, 'ok', NULL, 1, 0, 0, 'x', 'now')").run(ver);
    w.prepare("INSERT INTO analyzer_finding VALUES ('P', ?, 'Error', 'R', 'E', 'Main.xaml', '', '', 'old', '')").run(ver);
    w.close();
    write(path.join(p, "Main.xaml"), SIMPLE.replace("ASSET_NAME", "Changed"));
    extract(ix, "--source", path.join(d, "repo"));
    const db = openDb(ix);
    assert.equal(db.prepare("SELECT count(*) n FROM analyzer_run").get().n, 0);
    assert.equal(db.prepare("SELECT count(*) n FROM analyzer_finding").get().n, 0);
    // replaced by a package without project.json: old facts go
    const pk = path.join(d, "pk");
    fs.mkdirSync(pk, { recursive: true });
    nupkg(path.join(pk, "q.nupkg"), "Q", "1.0.0", { "project.json": JSON.stringify({ name: "Q", main: "Main.xaml" }), "Main.xaml": SIMPLE.replace("ASSET_NAME", "QA") });
    extract(ix, "--nupkg", pk);
    fs.writeFileSync(path.join(pk, "q.nupkg"), zip([["Q.nuspec", "<package><metadata><id>Q</id><version>1.0.0</version></metadata></package>"], ["content/flow.json", "{}"]]));
    extract(ix, "--nupkg", pk);
    assert.equal(openDb(ix).prepare("SELECT count(*) n FROM edge WHERE package_id='Q'").get().n, 0);
  });

  test("F10 an index inside the source tree is not read back as source", () => {
    const d = path.join(tmp, "f10");
    proj(path.join(d, "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    extract(path.join(d, "P", "automation-index"), "--source", path.join(d, "P"));
    extract(path.join(d, "P", "automation-index"), "--source", path.join(d, "P"));
    assert.equal(openDb(path.join(d, "P", "automation-index")).prepare("SELECT count(*) n FROM package").get().n, 1);
  });

  test("F11 folder names that sanitize alike keep separate source trees", () => {
    const d = path.join(tmp, "f11");
    proj(path.join(d, "repo", "a", "b"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "One") });
    proj(path.join(d, "repo", "a_b"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "Two") });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    assert.equal(fs.readdirSync(path.join(d, "ix", "src")).length, 2);
  });

  test("F12/F13 Config inside a larger expression and later assignments are not resolved", () => {
    const d = path.join(tmp, "f12");
    const cfg = xlsx({ Settings: [["Name", "Value"], ["QueueKey", "Base"]] });
    proj(path.join(d, "repo", "P"), "P", {
      "Main.xaml": act(`<Sequence sap2010:WorkflowViewState.IdRef="S1">
        <ui:AddQueueItem QueueName="[Config(&quot;QueueKey&quot;) &amp; &quot;_Suffix&quot;]" sap2010:WorkflowViewState.IdRef="Q1"/>
        <ui:AddQueueItem QueueName="[q]" sap2010:WorkflowViewState.IdRef="Q2"/>
        <Assign sap2010:WorkflowViewState.IdRef="A1"><Assign.To><OutArgument x:TypeArguments="x:String">[q]</OutArgument></Assign.To><Assign.Value><InArgument x:TypeArguments="x:String">Future</InArgument></Assign.Value></Assign>
      </Sequence>`),
    });
    fs.writeFileSync(path.join(d, "repo", "P", "Config.xlsx"), cfg);
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const rows = openDb(path.join(d, "ix")).prepare("SELECT normalized, detail FROM edge WHERE kind='uses_queue'").all();
    const qs = rows.map((r) => r.normalized);
    assert.ok(!qs.includes("base"), qs.join(","));
    assert.ok(!qs.includes("future"), qs.join(","));
    // kept as hints on the dynamic facts, not as values
    assert.ok(rows.some((r) => /combines Config 'QueueKey' \(= 'Base'\)/.test(r.detail || "")), JSON.stringify(rows));
    assert.ok(rows.some((r) => /possible value 'Future'/.test(r.detail || "")), JSON.stringify(rows));
  });

  test("F14/F15/F16 activity identity, parent-relative calls and reachability after parse errors", () => {
    const d = path.join(tmp, "f14");
    proj(path.join(d, "repo", "P"), "P", {
      "Main.xaml": "<Activity><broken></Activity>",
      "Child.xaml": SIMPLE.replace("ASSET_NAME", "C"),
      "A/Sub/Caller.xaml": act(`<ui:InvokeWorkflowFile WorkflowFileName="..\\Common.xaml"/>\n<ui:AddQueueItem QueueName="Q"/>\n<ui:AddQueueItem QueueName="Q"/>`),
      "A/Common.xaml": SIMPLE.replace("ASSET_NAME", "Common"),
    });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const db = openDb(path.join(d, "ix"));
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE kind='uses_queue' AND file='A/Sub/Caller.xaml'").get().n, 2);
    // Studio resolves WorkflowFileName from the project root (19,526 of 19,526 sub-folder invokes in real projects did):
    // "..\\Common.xaml" leaves the project, so it is unresolved rather than guessed.
    assert.equal(db.prepare("SELECT target_path FROM invocation WHERE from_path='A/Sub/Caller.xaml'").get().target_path, null);
    assert.equal(db.prepare("SELECT reachable FROM workflow WHERE path='Child.xaml'").get().reachable, null);
  });

  test("F17/F18/F19 XML attribute safety, declarations and DTDs", () => {
    const r = m.parseXml(Buffer.from('<r __proto__="text" a="1"/>'));
    assert.equal(Object.getOwnPropertyDescriptor(r.attrs, "__proto__")?.value, "text");
    assert.equal(m.parseXml(Buffer.from('<?xml version="1.0"\n encoding="utf-8"?>\n<r/>')).line, 3);
    const latin = Buffer.concat([Buffer.from('<?xml version="1.0" encoding="ISO-8859-1"?><r>'), Buffer.from([0xc3, 0xa9]), Buffer.from("</r>")]);
    assert.equal(m.parseXml(latin).text, "Ã©");
    assert.throws(() => m.parseXml(Buffer.from('<!DOCTYPE r [<!ATTLIST r a CDATA "default">]><r/>')));
  });

  test("F20 zip entries with a wrong CRC are rejected", () => {
    const z = zip([["x.txt", "GOOD"]]);
    const cd = z.lastIndexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
    z.writeUInt32LE(0, cd + 16);
    z.writeUInt32LE(0, 14);
    assert.throws(() => new m.Zip(z).read("x.txt"), /CRC/);
  });

  test("F21 resource rules respect the folder an activity names", () => {
    const d = path.join(tmp, "f21");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": act('<ui:GetRobotAsset AssetName="A" FolderPath="Missing"/>') });
    const ix = path.join(d, "ix");
    extract(ix, "--source", path.join(d, "repo"));
    const w = openDb(ix);
    const pkg = w.prepare("SELECT package_id, version FROM package").get();
    w.prepare("INSERT INTO process VALUES ('K1','P','f1','Shared',?,?,'Process','Windows',NULL,1,0,NULL,'[]',NULL,NULL)").run(pkg.package_id, pkg.version);
    w.prepare("INSERT INTO orch_asset VALUES ('a1','f2','Elsewhere','A','Text','Global',0,'')").run();
    w.close();
    assert.equal(run(ix, "review").status, 0);
    assert.equal(openDb(ix).prepare("SELECT count(*) n FROM finding WHERE rule='AI-RES-001' AND subject='A'").get().n, 1);
  });

  test("F22 file and package names are matched case-insensitively", () => {
    const d = path.join(tmp, "f22");
    fs.mkdirSync(d, { recursive: true });
    fs.writeFileSync(path.join(d, "c.nupkg"), zip([["Case.nuspec", "<package><metadata><id>Case</id><version>1.0.0</version></metadata></package>"],
      ["content/Project.JSON", JSON.stringify({ name: "Case", main: "Main.xaml", dependencies: { "shared.lib": "[1.0.0]" } })], ["content/Main.xaml", SIMPLE.replace("ASSET_NAME", "M")]]));
    proj(path.join(d, "repo", "Shared.Lib"), "Shared.Lib", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "LibAsset") }, { designOptions: { outputType: "Library" } });
    extract(path.join(d, "ix"), "--nupkg", path.join(d, "c.nupkg"), "--source", path.join(d, "repo"));
    const db = openDb(path.join(d, "ix"));
    assert.equal(db.prepare("SELECT status FROM coverage WHERE package_id='Case' AND stage='extract'").get()?.status, "indexed");
    assert.equal(db.prepare("SELECT count(*) n FROM v_edge_all WHERE root_package_id='Case' AND normalized='libasset'").get().n, 1);
  });

  test("F23 a failed list call keeps the previous inventory", () => {
    const d = path.join(tmp, "f23");
    fs.mkdirSync(d, { recursive: true });
    const fake = path.join(d, "fake-uip.mjs");
    fs.writeFileSync(fake, `const a = process.argv.slice(2).join(" ");
const ok = (Data, more = false) => console.log(JSON.stringify({ Result: "Success", Data, Pagination: { HasMore: more } }));
if (a.startsWith("login status")) ok({ Status: "Logged in", Organization: "o", Tenant: "t" });
else if (a.startsWith("or processes list")) { console.log(JSON.stringify({ Result: "Failure", Message: "simulated outage" })); }
else ok([]);`);
    const ix = path.join(d, "ix");
    fs.mkdirSync(ix);
    const w = new DatabaseSync(path.join(ix, "index.db"));
    w.close();
    run(ix, "status");
    const w2 = openDb(ix);
    w2.prepare("INSERT INTO process VALUES ('K1','Old','f','F','P','1',NULL,NULL,NULL,1,0,NULL,'[]',NULL,NULL)").run();
    w2.close();
    const r = run(ix, "crawl", "--uip", fake, "--skip-jobs", "--skip-libraries");
    assert.equal(r.status, 0, r.stderr);
    const db = openDb(ix);
    assert.equal(db.prepare("SELECT count(*) n FROM process").get().n, 1);
    assert.match(run(ix, "status").stdout, /simulated outage/);
  });

  test("F25 an unreadable project.json is reported as partial", () => {
    const d = path.join(tmp, "f25");
    write(path.join(d, "repo", "P", "project.json"), "{broken");
    write(path.join(d, "repo", "P", "Main.xaml"), SIMPLE.replace("ASSET_NAME", "A"));
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const row = openDb(path.join(d, "ix")).prepare("SELECT status, detail FROM coverage WHERE stage='extract'").get();
    assert.equal(row.status, "partial");
    assert.match(row.detail, /project\.json/);
  });

  test("F24 --limit stops an unbounded query on every Node version", () => {
    const d = path.join(tmp, "f24");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const r = spawnSync(process.execPath, [SCRIPT, "--index-dir", path.join(d, "ix"), "query",
      "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT x FROM n", "--limit", "3", "--format", "json"], { encoding: "utf8", timeout: 20000 });
    assert.equal(r.status, 0, r.stderr || String(r.error));
    assert.deepEqual(JSON.parse(r.stdout).map((x) => x.x), [1, 2, 3]);
  });

  test("F26 JSON query output keeps duplicate column names apart", () => {
    const d = path.join(tmp, "f26");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    extract(path.join(d, "ix"), "--source", path.join(d, "repo"));
    const r = run(path.join(d, "ix"), "query", "SELECT 1 AS x, 2 AS x", "--format", "json");
    assert.deepEqual(JSON.parse(r.stdout), [{ x: 1, "x:1": 2 }]);
  });
});

describe("third review: findings reproduced as tests", () => {
  const SEC = "DEMO_SECRET";
  let tmp, m;
  const proj = (dir, name, files, extra = {}) => {
    write(path.join(dir, "project.json"), JSON.stringify({ name, main: "Main.xaml", ...extra }));
    for (const [rel, txt] of Object.entries(files)) write(path.join(dir, rel), txt);
  };
  const act = (body) => `<Activity ${NS}>${body}</Activity>`;
  const noSecret = (ix) => {
    const db = openDb(ix);
    for (const { name: t } of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()) {
      for (const { name: c } of db.prepare(`PRAGMA table_info(${t})`).all()) {
        assert.equal(db.prepare(`SELECT count(*) n FROM ${t} WHERE CAST(${c} AS TEXT) LIKE ?`).get(`%${SEC}%`).n, 0, `${t}.${c}`);
      }
    }
  };
  before(async () => {
    m = await import(pathToFileURL(SCRIPT).href);
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-r3-"));
  });

  test("R1 secrets in nested secret properties, escaped quotes and URL values are redacted", () => {
    const d = path.join(tmp, "r1");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": act(`<ui:SendMail sap2010:WorkflowViewState.IdRef="M1"><ui:SendMail.Password><InArgument x:TypeArguments="x:String"><VisualBasicValue x:TypeArguments="x:String" ExpressionText="&quot;https://${SEC}.example.com/&quot;" /></InArgument></ui:SendMail.Password></ui:SendMail>
      <ui:HttpClient EndPoint="https://api.example.com/x?token=alpha;${SEC}" sap2010:WorkflowViewState.IdRef="H1"/>`),
      "bindings_v2.json": JSON.stringify({ resources: [{ resource: "asset", value: { name: { defaultValue: "B" } }, metadata: { nested: { Password: `ab"${SEC}` } } }] }) });
    write(path.join(d, "repo", "P", "appconfig.json"), JSON.stringify({ Note: `{"Password":"ab\\"${SEC}"}` }));
    assert.equal(run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo")).status, 0);
    noSecret(path.join(d, "ix"));
    assert.ok(!m.scrub('{"nested":{"Password":"ab\\"' + SEC + '"}}').includes(SEC));
    assert.ok(!m.scrub(`?token=alpha;${SEC}`).includes(SEC));
    assert.equal(m.scrub("pwd=abc def;Server=x"), "pwd=<redacted len=7>;Server=x");
  });

  test("R2 a linked index.db is refused", () => {
    const d = path.join(tmp, "r2");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    fs.mkdirSync(path.join(d, "ix"), { recursive: true });
    const outside = path.join(d, "outside.db");
    new DatabaseSync(outside).close();
    const before = fs.statSync(outside).size;
    fs.symlinkSync(outside, path.join(d, "ix", "index.db"));
    const r = run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo"));
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /link/i);
    assert.equal(fs.statSync(outside).size, before);
  });

  test("R5 a failed folder listing keeps every per-folder inventory", () => {
    const d = path.join(tmp, "r5");
    fs.mkdirSync(d, { recursive: true });
    const fake = path.join(d, "fake-uip.mjs");
    fs.writeFileSync(fake, `const a = process.argv.slice(2).join(" ");
const ok = (Data) => console.log(JSON.stringify({ Result: "Success", Data, Pagination: { HasMore: false } }));
if (a.startsWith("login status")) ok({ Status: "Logged in" });
else if (a.startsWith("or folders list")) console.log(JSON.stringify({ Result: "Failure", Message: "folders unavailable" }));
else if (a.startsWith("or processes list")) ok([{ Key: "k1", Name: "Proc", FolderKey: "f-active", OrganizationUnitFullyQualifiedName: "Active", ProcessKey: "P", ProcessVersion: "1" }]);
else ok([]);`);
    const ix = path.join(d, "ix");
    run(ix, "status");
    const w = openDb(ix);
    w.prepare("INSERT INTO folder VALUES ('f-res', 'ResourceOnly', 'Standard', NULL, 'folders list')").run();
    w.prepare("INSERT INTO orch_asset VALUES ('a1', 'f-res', 'ResourceOnly', 'Kept_Asset', 'Text', 'Global', 0, '')").run();
    w.close();
    assert.equal(run(ix, "crawl", "--uip", fake, "--skip-jobs", "--skip-libraries").status, 0);
    const db = openDb(ix);
    assert.equal(db.prepare("SELECT count(*) n FROM orch_asset WHERE name='Kept_Asset'").get().n, 1);
    assert.equal(db.prepare("SELECT value FROM meta WHERE key='inventory_asset_ok'").get().value, "0");
  });

  test("R6 a trailing comment does not break --limit", () => {
    const d = path.join(tmp, "r6");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo"));
    const r = spawnSync(process.execPath, [SCRIPT, "--index-dir", path.join(d, "ix"), "query",
      "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT x FROM n -- note", "--limit", "2", "--format", "json"], { encoding: "utf8", timeout: 20000 });
    assert.equal(r.status, 0, r.stderr || String(r.error));
    assert.deepEqual(JSON.parse(r.stdout).map((x) => x.x), [1, 2]);
  });

  test("R9/R10 a renamed or broken project keeps one identity and no stale findings", () => {
    const d = path.join(tmp, "r9");
    const p = path.join(d, "repo", "P");
    proj(p, "Logical", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A") });
    const ix = path.join(d, "ix");
    run(ix, "extract", "--source", path.join(d, "repo"));
    const w = openDb(ix);
    const k = w.prepare("SELECT package_id, version FROM package").get();
    w.prepare("INSERT INTO finding VALUES ('AI-WA-001','Warning',?,?,NULL,'old','old analyzer error',NULL,'t')").run(k.package_id, k.version);
    w.close();
    write(path.join(p, "project.json"), "{broken");
    run(ix, "extract", "--source", path.join(d, "repo"));
    const db = openDb(ix);
    assert.equal(db.prepare("SELECT count(*) n FROM package").get().n, 1);
    assert.equal(db.prepare("SELECT status FROM coverage WHERE stage='extract'").get().status, "partial");
    assert.equal(db.prepare("SELECT count(*) n FROM finding WHERE detail='old analyzer error'").get().n, 0);
  });

  test("R7 folder names: aliases, relative paths, dynamic folders and traced values", () => {
    const d = path.join(tmp, "r7");
    proj(path.join(d, "repo", "P"), "P", { "Main.xaml": act(`<Sequence sap2010:WorkflowViewState.IdRef="S1">
      <ui:GetRobotCredential AssetName="CredA" OrchestratorFolderPath="Elsewhere" sap2010:WorkflowViewState.IdRef="C1"/>
      <ui:GetRobotAsset AssetName="Rel" FolderPath=".." sap2010:WorkflowViewState.IdRef="G1"/>
      <ui:GetRobotAsset AssetName="Dyn" FolderPath="[f]" sap2010:WorkflowViewState.IdRef="G2"/>
      <Assign sap2010:WorkflowViewState.IdRef="A1"><Assign.To><OutArgument x:TypeArguments="x:String">[q]</OutArgument></Assign.To><Assign.Value><InArgument x:TypeArguments="x:String">Traced_Q</InArgument></Assign.Value></Assign>
      <ui:AddQueueItem QueueName="[q]" FolderPath="Elsewhere" sap2010:WorkflowViewState.IdRef="Q1"/>
    </Sequence>`) });
    const ix = path.join(d, "ix");
    run(ix, "extract", "--source", path.join(d, "repo"));
    const w = openDb(ix);
    const k = w.prepare("SELECT package_id, version FROM package").get();
    w.prepare("INSERT INTO process VALUES ('K1','P','f-child','Shared/Child',?,?,'Process','Windows',NULL,1,0,NULL,'[]',NULL,NULL)").run(k.package_id, k.version);
    for (const [key, fk, fp, name] of [["a1", "f-shared", "Shared", "Rel"], ["a2", "f-else", "Elsewhere", "Dyn"], ["a3", "f-else", "Elsewhere", "CredA"]]) {
      w.prepare("INSERT INTO orch_asset VALUES (?,?,?,?, 'Text','Global',0,'')").run(key, fk, fp, name);
    }
    w.prepare("INSERT INTO orch_queue VALUES ('q1','f-x','Other','Traced_Q','{}')").run();
    w.close();
    assert.equal(run(ix, "review").status, 0);
    const db = openDb(ix);
    const f = db.prepare("SELECT rule, subject, detail FROM finding WHERE rule LIKE 'AI-RES-%'").all();
    assert.ok(db.prepare("SELECT count(*) n FROM edge WHERE kind='reads_credential' AND detail LIKE '%folder=Elsewhere%'").get().n === 1);
    assert.ok(!f.some((x) => x.subject === "Rel"), JSON.stringify(f)); // ".." from Shared/Child is Shared, where Rel exists
    assert.ok(!f.some((x) => x.subject === "Dyn"), JSON.stringify(f)); // unknown folder: no claim either way
    assert.ok(f.some((x) => x.rule === "AI-RES-003" && x.subject === "Traced_Q" && /Elsewhere/.test(x.detail)), JSON.stringify(f));
  });

  test("R8 a Project.JSON source folder is found", () => {
    const d = path.join(tmp, "r8");
    write(path.join(d, "repo", "P", "Project.JSON"), JSON.stringify({ name: "Upper", main: "Main.xaml" }));
    write(path.join(d, "repo", "P", "Main.xaml"), SIMPLE.replace("ASSET_NAME", "A"));
    run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo"));
    assert.equal(openDb(path.join(d, "ix")).prepare("SELECT package_id FROM package").get()?.package_id, "Upper");
  });

  test("R12/R13/R14/R15 identity, competing assignments, Config dictionaries, partial metadata", () => {
    const d = path.join(tmp, "r12");
    const cfg = xlsx({ Settings: [["Name", "Value"], ["K", "Base"]] });
    proj(path.join(d, "repo", "P"), "P", {
      "Main.xaml": act(`<Sequence sap2010:WorkflowViewState.IdRef="S1"><ui:AddQueueItem QueueName="Same"/><ui:AddQueueItem QueueName="Same"/>
<Assign sap2010:WorkflowViewState.IdRef="A1"><Assign.To><OutArgument x:TypeArguments="x:String">[q]</OutArgument></Assign.To><Assign.Value><InArgument x:TypeArguments="x:String">Old</InArgument></Assign.Value></Assign>
<Assign sap2010:WorkflowViewState.IdRef="A2"><Assign.To><OutArgument x:TypeArguments="x:String">[q]</OutArgument></Assign.To><Assign.Value><InArgument x:TypeArguments="x:String">New</InArgument></Assign.Value></Assign>
<ui:AddQueueItem QueueName="[q]" sap2010:WorkflowViewState.IdRef="Q9"/>
<ui:AddQueueItem QueueName="[io_DictConfig(&quot;K&quot;)]" sap2010:WorkflowViewState.IdRef="Q8"/></Sequence>`),
    });
    fs.writeFileSync(path.join(d, "repo", "P", "Config.xlsx"), cfg);
    proj(path.join(d, "repo", "Q"), "Q", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "A"), "Child.xaml": SIMPLE.replace("ASSET_NAME", "B"), "entry-points.json": JSON.stringify({ entryPoints: [{ filePath: "Main.xaml" }] }) });
    write(path.join(d, "repo", "Q", "project.json"), "{broken");
    run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo"));
    const db = openDb(path.join(d, "ix"));
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE kind='uses_queue' AND normalized='same'").get().n, 2);
    const traced = db.prepare("SELECT normalized, confidence FROM edge WHERE kind='uses_queue' AND activity LIKE 'Q9%' AND normalized IN ('old','new')").all();
    assert.ok(traced.every((r) => r.confidence === "low"), JSON.stringify(traced));
    // Every real settings dictionary name ends in Config (in_Config, io_DictConfig, DictionaryConfig...): resolved.
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE kind='uses_queue' AND normalized='base' AND activity LIKE 'Q8%'").get().n, 1);
    assert.equal(db.prepare("SELECT reachable FROM workflow WHERE package_id='Q' AND path='Child.xaml'").get().reachable, null);
  });

  test("R16/R17/R18 column names, folder-name collisions, long declarations", () => {
    const d = path.join(tmp, "r16");
    proj(path.join(d, "repo", "a", "Foo"), "Foo", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "One") });
    proj(path.join(d, "repo", "b", "foo"), "foo", { "Main.xaml": SIMPLE.replace("ASSET_NAME", "Two") });
    run(path.join(d, "ix"), "extract", "--source", path.join(d, "repo"));
    const dirs = fs.readdirSync(path.join(d, "ix", "src"));
    assert.equal(new Set(dirs.map((x) => x.toLowerCase())).size, 2, dirs.join(","));
    const r = run(path.join(d, "ix"), "query", 'SELECT 1 AS x, 2 AS x, 3 AS "x:1"', "--format", "json");
    assert.deepEqual(Object.values(JSON.parse(r.stdout)[0]).sort(), [1, 2, 3]);
    const decl = Buffer.concat([Buffer.from(`<?xml version="1.0"${" ".repeat(300)}encoding="ISO-8859-1"?><r>`), Buffer.from([0xc3, 0xa9]), Buffer.from("</r>")]);
    assert.equal(m.parseXml(decl).text, "\u00c3\u00a9");
  });
});

describe("process topology, reviewed summaries and fingerprints", () => {
  let tmp, ix, db;
  const act = (body) => `<Activity ${NS}><Sequence sap2010:WorkflowViewState.IdRef="S1">${body}</Sequence></Activity>`;
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-topo-"));
    const repo = path.join(tmp, "repo");
    const proj = (dir, name, main, extra = {}) => {
      write(path.join(repo, dir, "project.json"), JSON.stringify({ name, main: "Main.xaml", ...extra }));
      write(path.join(repo, dir, "Main.xaml"), main);
    };
    proj("Dispatcher", "Dispatcher", act(`<ui:AddQueueItem QueueName="Orders" sap2010:WorkflowViewState.IdRef="Q1"/>
      <ui:AddQueueItem QueueName="Audit" sap2010:WorkflowViewState.IdRef="Q2"/>
      <ui:InvokeProcess ProcessName="Reporter" sap2010:WorkflowViewState.IdRef="P1"/>
      <If sap2010:WorkflowViewState.IdRef="If_1"/><If sap2010:WorkflowViewState.IdRef="If_2"/>
      <TryCatch sap2010:WorkflowViewState.IdRef="TryCatch_1"/><ui:CreateFormTask sap2010:WorkflowViewState.IdRef="T1"/>`));
    proj("Performer", "Performer", act(`<ui:GetTransactionItem QueueType="Orders" sap2010:WorkflowViewState.IdRef="G1"/>
      <ui:GetQueueItem QueueName="Inbound" sap2010:WorkflowViewState.IdRef="G2"/>`));
    proj("Reporter", "Reporter", act('<ui:GetRobotAsset AssetName="ReportPath" sap2010:WorkflowViewState.IdRef="A1"/>'));
    for (const f of ["InitAllSettings", "GetTransactionData", "SetTransactionStatus"]) write(path.join(repo, "Performer", "Framework", `${f}.xaml`), act(""));
    // a Flow package calling the Dispatcher through its bindings
    fs.mkdirSync(path.join(tmp, "pk"), { recursive: true });
    nupkg(path.join(tmp, "pk", "flow.nupkg"), "Orders.Flow", "1.0.0", {
      "OrdersFlow.bpmn": "<definitions/>",
      "bindings_v2.json": JSON.stringify({ version: "2.0", resources: [{ resource: "process", key: "Shared.Dispatcher",
        value: { name: { defaultValue: "Dispatcher" }, folderPath: { defaultValue: "Shared" } }, metadata: { ActivityName: "Start dispatcher" } }] }),
    });
    ix = path.join(tmp, "ix");
    let r = run(ix, "extract", "--source", repo, "--nupkg", path.join(tmp, "pk"));
    assert.equal(r.status, 0, r.stderr);
    const w = openDb(ix);
    let n = 0;
    for (const { package_id, version } of w.prepare("SELECT package_id, version FROM package").all()) {
      const name = package_id === "Orders.Flow" ? "OrdersFlow" : package_id;
      w.prepare("INSERT INTO process VALUES (?,?,'f1','Shared',?,?,'Process',NULL,NULL,1,0,NULL,'[]',NULL,NULL)").run(`K${++n}`, name, package_id, version);
    }
    w.prepare("INSERT OR REPLACE INTO meta VALUES ('inventory_process_ok', '1')").run();
    w.close();
    r = run(ix, "review");
    assert.equal(r.status, 0, r.stderr);
    db = openDb(ix);
  });

  const nameOf = (key) => db.prepare("SELECT name FROM process WHERE key=?").get(key).name;

  test("links: queue handover, process call, and a Flow calling RPA through bindings", () => {
    const links = db.prepare("SELECT from_key, to_key, via, resource FROM v_process_link").all().map((l) => `${nameOf(l.from_key)} -${l.via}:${l.resource}-> ${nameOf(l.to_key)}`).sort();
    assert.deepEqual(links, ["Dispatcher -process:reporter-> Reporter", "Dispatcher -queue:orders-> Performer", "OrdersFlow -process:dispatcher-> Dispatcher"]);
  });

  test("queue handover gaps are findings", () => {
    const f = Object.fromEntries(db.prepare("SELECT rule, group_concat(subject) s FROM finding WHERE rule LIKE 'AI-LNK-%' GROUP BY rule").all().map((r) => [r.rule, r.s]));
    assert.equal(f["AI-LNK-001"], "audit");
    assert.equal(f["AI-LNK-002"], "inbound");
  });

  test("cards show upstream and downstream automations", () => {
    assert.equal(run(ix, "cards").status, 0);
    const card = fs.readFileSync(path.join(ix, "cards", fs.readdirSync(path.join(ix, "cards")).find((f) => f.endsWith("__Dispatcher.md"))), "utf8");
    assert.match(card, /upstream \(feeds this\) \| OrdersFlow \| process/);
    assert.match(card, /downstream \(fed by this\) \| Performer \| queue \| orders/);
  });

  test("a summary is stored only after every claim is reviewed as supported", () => {
    const pkg = db.prepare("SELECT package_id, version FROM package WHERE package_id='Dispatcher'").get();
    const sum = path.join(tmp, "s.md");
    fs.writeFileSync(sum, "Dispatches orders to a queue and starts the reporter.");
    const rev = path.join(tmp, "r.md");
    const set = (...extra) => run(ix, "summary", "set", "--package", pkg.package_id, "--version", pkg.version, "--model", "test-model", "--file", sum, ...extra);
    assert.notEqual(set().status, 0); // no review
    fs.writeFileSync(rev, "| Claim | Verdict | Evidence |\n|---|---|---|\n| Adds to Orders | SUPPORTED | Main.xaml:1 |\n| Sends email | UNSUPPORTED | none |");
    let r = set("--review", rev);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /UNSUPPORTED/);
    fs.writeFileSync(rev, "| Claim | Verdict | Evidence |\n|---|---|---|\n| Adds to Orders | SUPPORTED | Main.xaml:1 |\n| Starts Reporter | SUPPORTED | Main.xaml:3 |");
    r = set("--review", rev);
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /2 claims checked/);
  });

  test("export writes one stable fingerprint per package", () => {
    const r = run(ix, "export");
    assert.equal(r.status, 0, r.stderr);
    const fps = Object.fromEntries(r.stdout.trim().split("\n").map((l) => JSON.parse(l)).map((f) => [f.package_id, f]));
    assert.equal(Object.keys(fps).length, 4);
    const d = fps.Dispatcher;
    assert.equal(d.schema, "automation-index/fingerprint@1");
    assert.deepEqual(d.queues, { writes: ["audit", "orders"], reads: [] });
    assert.deepEqual(d.invokes_processes, ["reporter"]);
    assert.equal(d.complexity.decisions, 2);
    assert.equal(d.complexity.try_catch, 1);
    assert.equal(d.complexity.human_tasks, 1);
    assert.equal(d.activity_counts.If, 2);
    assert.deepEqual(d.downstream.map((x) => x.automation).sort(), ["Performer", "Reporter"]);
    assert.equal(d.summary.authoritative, false);
    assert.equal(fps.Performer.framework, "REFramework");
    assert.deepEqual(fps.Performer.queues.reads, ["inbound", "orders"]);
    assert.ok(fps["Orders.Flow"].coverage.some((c) => c.startsWith("extract:unsupported")));
    assert.deepEqual(fps["Orders.Flow"].invokes_processes, ["dispatcher"]);
  });
});

describe("disabled code, shipped documents, connector operations", () => {
  let tmp, ix, db;
  const NSX = NS + ' xmlns:isactr="http://schemas.uipath.com/workflow/integration-service-activities/isactr"';
  const docx = (paras) => zip([["word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
    paras.map((p) => `<w:p><w:r><w:t>${p}</w:t></w:r></w:p>`).join("") + "</w:body></w:document>"]]);
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-docs-"));
    const repo = path.join(tmp, "repo");
    const cfg = zlib.gzipSync(Buffer.from(JSON.stringify({ InstanceParameters: { ConnectorKey: "uipath-salesforce-sfdc" }, ObjectName: "Account",
      Operation: "Create", HttpMethod: "POST", Path: "/Account", CachedLookupValues: { ObjectName: [{ Id: "SampleCustomerRecord" }] } }))).toString("base64");
    write(path.join(repo, "P", "project.json"), JSON.stringify({ name: "P", main: "Main.xaml" }));
    write(path.join(repo, "P", "Main.xaml"), `<Activity ${NSX}><Sequence sap2010:WorkflowViewState.IdRef="S1">
      <ui:CommentOut sap2010:WorkflowViewState.IdRef="C1"><ui:CommentOut.Body><Sequence sap2010:WorkflowViewState.IdRef="S2">
        <ui:InvokeWorkflowFile WorkflowFileName="Old.xaml" sap2010:WorkflowViewState.IdRef="I1"/>
        <ui:GetRobotAsset AssetName="Disabled_Asset" sap2010:WorkflowViewState.IdRef="G1"/>
      </Sequence></ui:CommentOut.Body></ui:CommentOut>
      <ui:GetRobotAsset AssetName="Live_Asset" sap2010:WorkflowViewState.IdRef="G2"/>
      <isactr:ConnectorActivity Configuration="${cfg}" ConnectionId="c-1" sap2010:WorkflowViewState.IdRef="CA1"/>
    </Sequence></Activity>`);
    write(path.join(repo, "P", "Old.xaml"), SIMPLE.replace("ASSET_NAME", "Old_Asset"));
    write(path.join(repo, "P", "README.md"), "# Invoice dispatcher\n\nReads invoices and adds queue items.");
    write(path.join(repo, "P", "AGENTS.md"), "# Project context\n");
    fs.mkdirSync(path.join(repo, "P", "Documentation"), { recursive: true });
    fs.writeFileSync(path.join(repo, "P", "Documentation", "REFramework Documentation-EN.pdf"), "%PDF-1.4 template");
    fs.writeFileSync(path.join(repo, "P", "Documentation", "PDD_Invoices_v1.docx"), docx(["Process Definition Document", "Applications Used", "SAP GUI", "Service account Password=Zq7Secret"]));
    write(path.join(repo, "Bare", "project.json"), JSON.stringify({ name: "Bare", main: "Main.xaml" }));
    write(path.join(repo, "Bare", "Main.xaml"), SIMPLE.replace("ASSET_NAME", "Bare_Asset"));
    ix = path.join(tmp, "ix");
    let r = run(ix, "extract", "--source", repo);
    assert.equal(r.status, 0, r.stderr);
    const w = openDb(ix);
    let n = 0;
    for (const { package_id, version } of w.prepare("SELECT package_id, version FROM package").all()) {
      w.prepare("INSERT INTO process VALUES (?,?,'f1','Shared',?,?,'Process',NULL,NULL,1,0,NULL,'[]',NULL,NULL)").run(`K${++n}`, package_id, package_id, version);
    }
    w.close();
    r = run(ix, "review");
    assert.equal(r.status, 0, r.stderr);
    db = openDb(ix);
  });

  test("code inside Comment Out is ignored: no facts, no calls, unreachable target", () => {
    const assets = db.prepare("SELECT normalized FROM edge WHERE package_id='P' AND kind='reads_asset' AND file='Main.xaml'").all().map((r) => r.normalized);
    assert.deepEqual(assets, ["live_asset"]);
    assert.equal(db.prepare("SELECT count(*) n FROM invocation WHERE package_id='P'").get().n, 0);
    assert.equal(db.prepare("SELECT reachable FROM workflow WHERE package_id='P' AND path='Old.xaml'").get().reachable, 0);
    assert.equal(db.prepare("SELECT count(*) n FROM activity_use WHERE package_id='P' AND activity='InvokeWorkflowFile'").get().n, 0);
    assert.match(db.prepare("SELECT detail FROM coverage WHERE package_id='P' AND stage='extract'").get().detail, /3 activities inside Comment Out/);
  });

  test("shipped documents are catalogued, templates recognised, docx text read and scrubbed", () => {
    const docs = Object.fromEntries(db.prepare("SELECT path, kind, template, text FROM document WHERE package_id='P'").all().map((d) => [d.path, d]));
    assert.equal(docs["README.md"].kind, "readme");
    assert.equal(docs["AGENTS.md"].kind, "agent_context");
    assert.equal(docs["Documentation/REFramework Documentation-EN.pdf"].template, 1);
    assert.equal(docs["Documentation/PDD_Invoices_v1.docx"].kind, "pdd");
    assert.match(docs["Documentation/PDD_Invoices_v1.docx"].text, /Applications Used\nSAP GUI/);
    assert.ok(!docs["Documentation/PDD_Invoices_v1.docx"].text.includes("Zq7Secret"));
    const f = db.prepare("SELECT group_concat(subject) s FROM finding WHERE rule='AI-DOC-001'").get().s;
    assert.equal(f, "Bare");
    assert.equal(run(ix, "cards").status, 0);
    const card = fs.readFileSync(path.join(ix, "cards", fs.readdirSync(path.join(ix, "cards")).find((x) => x.endsWith("__P.md"))), "utf8");
    assert.match(card, /## Documentation/);
    assert.match(card, /template \(not project-specific\)/);
  });

  test("connector configuration gives connector, operation and object, never cached values", () => {
    const e = db.prepare("SELECT value, detail FROM edge WHERE kind='uses_connector_operation'").get();
    assert.equal(e.value, "uipath-salesforce-sfdc Create Account");
    assert.match(e.detail, /method=POST; path=\/Account/);
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE value LIKE '%SampleCustomerRecord%' OR detail LIKE '%SampleCustomerRecord%'").get().n, 0);
    const fp = JSON.parse(run(ix, "export", "--package", "P").stdout);
    assert.deepEqual(fp.connector_operations, ["uipath-salesforce-sfdc create account"]);
    assert.ok(fp.documents.some((d) => d.kind === "pdd"));
  });
});

describe("pull request review fixes", () => {
  let tmp;
  before(() => { tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-pr-")); });

  test("only Studio's Comment Out disables code, and disabled code is still scanned for secrets", () => {
    const repo = path.join(tmp, "repo");
    write(path.join(repo, "P", "project.json"), JSON.stringify({ name: "P", main: "Main.xaml" }));
    write(path.join(repo, "P", "Main.xaml"), `<Activity ${NS} xmlns:fake="urn:custom-activities"><Sequence sap2010:WorkflowViewState.IdRef="S1">
      <fake:CommentOut sap2010:WorkflowViewState.IdRef="F1"><ui:GetRobotAsset AssetName="Runs_Anyway" sap2010:WorkflowViewState.IdRef="G1"/></fake:CommentOut>
      <ui:CommentOut sap2010:WorkflowViewState.IdRef="C1"><ui:CommentOut.Body><Sequence sap2010:WorkflowViewState.IdRef="S2">
        <ui:HttpClient EndPoint="https://old.example.com" Password="Old7Secret" sap2010:WorkflowViewState.IdRef="H1"/>
      </Sequence></ui:CommentOut.Body></ui:CommentOut>
    </Sequence></Activity>`);
    const ix = path.join(tmp, "ix1");
    assert.equal(run(ix, "extract", "--source", repo).status, 0);
    const db = openDb(ix);
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE normalized='runs_anyway'").get().n, 1);
    assert.equal(db.prepare("SELECT count(*) n FROM edge WHERE normalized='old.example.com'").get().n, 0);
    const s = db.prepare("SELECT value, detail FROM edge WHERE kind='hardcoded_secret'").all();
    assert.equal(s.length, 1);
    assert.match(s[0].detail, /Comment Out block \(disabled code\)/);
    assert.ok(!s[0].value.includes("Old7Secret"));
  });

  test("identity lookups never download job output files, and stale run data gives no run findings", () => {
    const d = path.join(tmp, "crawl");
    fs.mkdirSync(d, { recursive: true });
    const log = path.join(d, "calls.log");
    const fake = path.join(d, "fake-uip.mjs");
    fs.writeFileSync(fake, `import fs from "node:fs";
const args = process.argv.slice(2); const a = args.join(" ");
fs.appendFileSync(${JSON.stringify(log)}, a + "\\n");
const ok = (Data) => console.log(JSON.stringify({ Result: "Success", Data, Pagination: { HasMore: false } }));
if (a.startsWith("login status")) ok({ Status: "Logged in" });
else if (a.startsWith("or processes list")) ok([{ Key: "k1", Name: "Proc", FolderKey: "f1", OrganizationUnitFullyQualifiedName: "Shared", ProcessKey: "P", ProcessVersion: "1" },
  { Key: "k2", Name: "Idle", FolderKey: "f1", OrganizationUnitFullyQualifiedName: "Shared", ProcessKey: "P2", ProcessVersion: "1" }]);
else if (a.startsWith("or jobs list")) ok([{ Key: "j1", State: "Successful", StartTime: new Date().toISOString(), Release: { Key: "k1" }, LocalSystemAccount: "robot1" }]);
else if (a.startsWith("or jobs get")) { if (!args.includes("--no-download")) fs.writeFileSync("job-output.bin", "PAYLOAD"); ok({ OrchestratorUserIdentity: "robot-account" }); }
else ok([]);`);
    const ix = path.join(d, "ix");
    const r = spawnSync(process.execPath, [SCRIPT, "--index-dir", ix, "crawl", "--uip", fake, "--skip-libraries", "--resolve-identities"], { encoding: "utf8", cwd: d });
    assert.equal(r.status, 0, r.stderr);
    assert.ok(fs.readFileSync(log, "utf8").split("\n").some((l) => l.startsWith("or jobs get j1") && l.includes("--no-download")));
    assert.ok(!fs.existsSync(path.join(d, "job-output.bin")));
    assert.equal(openDb(ix).prepare("SELECT orchestrator_identity FROM job_identity").get().orchestrator_identity, "robot-account");
    // the idle process comes from the crawl itself, so it survives the re-crawl below
    assert.equal(run(ix, "review").status, 0);
    assert.ok(openDb(ix).prepare("SELECT count(*) n FROM finding WHERE rule='AI-RUN-001'").get().n >= 1);
    // now a crawl that skips jobs: earlier run counts are stale, so no run findings
    assert.equal(spawnSync(process.execPath, [SCRIPT, "--index-dir", ix, "crawl", "--uip", fake, "--skip-libraries", "--skip-jobs"], { encoding: "utf8", cwd: d }).status, 0);
    assert.equal(openDb(ix).prepare("SELECT count(*) n FROM process WHERE name='Idle'").get().n, 1);
    assert.equal(run(ix, "review").status, 0);
    assert.equal(openDb(ix).prepare("SELECT count(*) n FROM finding WHERE rule LIKE 'AI-RUN-%'").get().n, 0);
  });
});

describe("impact, version diff, SARIF and CycloneDX", () => {
  let tmp, ix, repo;
  const act = (body) => `<Activity ${NS}><Sequence sap2010:WorkflowViewState.IdRef="S1">${body}</Sequence></Activity>`;
  const NEW_MAIN = act(`
<ui:GetRobotAsset AssetName="Api_Key" sap2010:WorkflowViewState.IdRef="A1"/>
<ui:HttpClient DisplayName="Call API" EndPoint="https://new.example.com/v2" Password="N3wS3cretValue"/>
<ui:HttpClient DisplayName="Call API" EndPoint="https://new.example.com/v3" Password="N3wS3cretOther"/>`);
  before(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ai-impact-"));
    repo = path.join(tmp, "repo");
    const proj = (dir, extra, body) => {
      write(path.join(repo, dir, "project.json"), JSON.stringify({ name: dir, main: "Main.xaml", ...extra }));
      write(path.join(repo, dir, "Main.xaml"), act(body));
    };
    proj("Writer", { dependencies: { "Shared.Lib": "[1.0.0]", "UiPath.System.Activities": "[25.10.0]" } },
      `<ui:AddQueueItem QueueName="Orders" sap2010:WorkflowViewState.IdRef="Q1"/><ui:GetRobotAsset AssetName="Api_Key" sap2010:WorkflowViewState.IdRef="A1"/>`);
    proj("Reader", {}, `<ui:GetTransactionItem QueueType="Orders" sap2010:WorkflowViewState.IdRef="G1"/>`);
    proj("LibUser", { dependencies: { "shared.lib": "[1.0.0]" } }, `<ui:LogMessage sap2010:WorkflowViewState.IdRef="L1"/>`);
    // a second checkout of Reader, and a link inside it that points outside the project
    write(path.join(repo, "Copy", "Reader", "project.json"), JSON.stringify({ name: "Reader", main: "Main.xaml" }));
    write(path.join(repo, "Copy", "Reader", "Main.xaml"), act(`<ui:LogMessage sap2010:WorkflowViewState.IdRef="L1"/>`));
    write(path.join(tmp, "outside", "Main.xaml"), act(""));
    fs.symlinkSync(path.join(tmp, "outside"), path.join(repo, "Reader", "link"));
    const pk = path.join(tmp, "pk");
    fs.mkdirSync(pk, { recursive: true });
    nupkg(path.join(pk, "lib.nupkg"), "Shared.Lib", "1.0.0", {
      "project.json": JSON.stringify({ name: "Shared.Lib", main: "Get.xaml", designOptions: { outputType: "Library" } }),
      "Get.xaml": act(`<ui:GetRobotAsset AssetName="Api_Key" sap2010:WorkflowViewState.IdRef="A1"/><ui:HttpClient EndPoint="https://lib.example.com/api" sap2010:WorkflowViewState.IdRef="H1"/>`),
    });
    nupkg(path.join(pk, "n1.nupkg"), "Proc.N", "1.0.0", {
      "project.json": JSON.stringify({ name: "Proc.N", main: "Main.xaml" }),
      "Main.xaml": act(`<ui:GetRobotAsset AssetName="Api_Key" sap2010:WorkflowViewState.IdRef="A1"/>`),
      "applicationsMetadata.json": JSON.stringify({ Metadata: [
        { ApplicationType: "Browser", Url: { Value: "in_PortalRoot & in_PagePath", IsExpression: true } },
        { ApplicationType: "Browser", Url: { Value: "\"https://quoted.example.com/home\"", IsExpression: true } }] }),
    });
    nupkg(path.join(pk, "n2.nupkg"), "Proc.N", "1.1.0", { "project.json": JSON.stringify({ name: "Proc.N", main: "Main.xaml" }), "Main.xaml": NEW_MAIN });
    ix = path.join(tmp, "ix");
    let r = run(ix, "extract", "--source", repo, "--nupkg", pk);
    assert.equal(r.status, 0, r.stderr);
    const w = openDb(ix);
    const ver = (p) => w.prepare("SELECT max(version) v FROM package WHERE package_id=? AND version NOT LIKE '%Copy%'").get(p).v;
    const procs = [["K1", "Writer", "Writer", "Finance"], ["K2", "Reader", "Reader", "Finance"],
      ["K3", "LibUser", "LibUser", "HR"], ["K4", "ProcN", "Proc.N", "HR"]].map(([k, n, p, f]) => [k, n, p, ver(p), f]);
    for (const [k, n, p, v, f] of procs) w.prepare("INSERT INTO process VALUES (?,?,?,?,?,?,'Process',NULL,NULL,1,0,NULL,'[]',NULL,NULL)").run(k, n, "fk-" + f, f, p, v);
    w.prepare("INSERT INTO job_summary VALUES ('K1','Writer','Finance',40,12,0,0,40,NULL,NULL,90)").run();
    for (const [k, v] of [["inventory_process_ok", "1"], ["inventory_jobs_ok", "1"], ["jobs_window_days", "90"], ["inventory_asset_ok", "1"]]) w.prepare("INSERT OR REPLACE INTO meta VALUES (?, ?)").run(k, v);
    w.prepare("INSERT INTO orch_asset VALUES ('a1','fk-Finance','Finance','Api_Key','Text','Folder',1,NULL)").run();
    w.close();
    r = run(ix, "review");
    assert.equal(r.status, 0, r.stderr);
  });

  const impact = (...args) => {
    const r = run(ix, "impact", ...args, "--format", "json");
    assert.equal(r.status, 0, r.stderr);
    return JSON.parse(r.stdout);
  };

  test("impact lists direct users and users through a library, with runs, folders and a risk grade", () => {
    const r = impact("--kind", "asset", "--name", "API_KEY");
    assert.deepEqual(r.processes.map((p) => p.process), ["Writer", "LibUser", "ProcN"]); // most active first
    assert.deepEqual(r.processes.find((p) => p.process === "LibUser").via.length, 1);
    assert.equal(r.processes.find((p) => p.process === "Writer").runs_30d, 12);
    assert.deepEqual(r.orchestrator.found, ["Api_Key (Finance)"]);
    assert.equal(r.risk.level, "medium"); // 3 processes, 1 active, 2 folders
    assert.equal(r.unknowns.run_counts_current, true);
    assert.equal(impact("--kind", "asset", "--name", "api_*").processes.length, 3);
    assert.equal(impact("--kind", "asset", "--name", "nothing_here").risk.level, "none");
  });

  test("impact on a queue gives both sides; on a host it accepts a URL; on a library it uses the dependency graph", () => {
    const q = impact("--kind", "queue", "--name", "orders");
    assert.deepEqual(q.processes.map((p) => `${p.process}:${p.modes.join("")}`).sort(), ["Reader:get", "Writer:add"]);
    assert.equal(impact("--kind", "host", "--name", "https://lib.example.com/api?page=1").processes.length, 2);
    const h = impact("--kind", "host", "--name", "https://LIB.example.com/other/path");
    assert.equal(h.normalized, "lib.example.com");
    assert.deepEqual(h.processes.map((p) => p.process).sort(), ["LibUser", "Writer"]);
    assert.deepEqual(impact("--kind", "library", "--name", "shared.lib").processes.map((p) => p.process).sort(), ["LibUser", "Writer"]);
    const md = run(ix, "impact", "--kind", "queue", "--name", "orders");
    assert.match(md.stdout, /## What the index cannot rule out/);
    assert.notEqual(run(ix, "impact", "--kind", "queue").status, 0);
  });

  test("the most shared resources are ranked, in impact --top and in INDEX.md", () => {
    const top = impact("--top", "10");
    assert.deepEqual(top[0], { kind: "asset", name: "api_key", processes: 3, folders: 2, active_30d: 1 });
    assert.ok(top.some((t) => t.kind === "library" && t.name === "Shared.Lib" && t.processes === 2));
    assert.ok(top.every((t) => t.processes > 1));
    assert.equal(run(ix, "cards").status, 0);
    assert.match(fs.readFileSync(path.join(ix, "INDEX.md"), "utf8"), /## Most shared resources[\s\S]*\| asset \| api_key \| 3 \| 2 \| 1 \|/);
  });

  test("an expression in applicationsMetadata.json is dynamic, not a host; a quoted literal is a host", () => {
    const db = openDb(ix);
    const rows = db.prepare("SELECT normalized, static FROM edge WHERE package_id='Proc.N' AND version='1.0.0' AND extractor='studio_applications_metadata' ORDER BY normalized").all();
    assert.deepEqual(rows.map((x) => [x.normalized, x.static]), [["<dynamic>", 0], ["quoted.example.com", 1]]);
  });

  test("diff between two versions shows new external reach; a baseline export diffs the whole index", () => {
    let r = run(ix, "diff", "--package", "Proc.N", "--from", "1.0.0", "--to", "1.1.0", "--format", "json");
    assert.equal(r.status, 0, r.stderr);
    const d = JSON.parse(r.stdout);
    const hosts = d.changed[0].fields.find((f) => f.field === "hosts");
    assert.deepEqual(hosts.added, ["new.example.com"]);
    assert.deepEqual(hosts.removed, ["quoted.example.com"]);
    assert.ok(d.changed[0].fields.some((f) => f.field === "findings" && f.added.some((x) => x.startsWith("AI-SEC-001"))));
    assert.match(run(ix, "diff", "--package", "Proc.N", "--from", "1.0.0", "--to", "1.1.0").stdout, /## New external reach[\s\S]*new\.example\.com/);
    const base = path.join(tmp, "base.jsonl");
    assert.equal(run(ix, "export", "--out", base).status, 0);
    r = JSON.parse(run(ix, "diff", "--baseline", base, "--format", "json").stdout);
    assert.equal(r.changed.length + r.added.length + r.removed.length, 0);
    // drop one package and age another in the baseline
    const lines = fs.readFileSync(base, "utf8").trim().split("\n").map((l) => JSON.parse(l)).filter((f) => f.package_id !== "Reader");
    const w = lines.find((f) => f.package_id === "Writer");
    w.queues.writes = [];
    fs.writeFileSync(base, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
    r = JSON.parse(run(ix, "diff", "--baseline", base, "--format", "json").stdout);
    assert.deepEqual(r.added, ["Reader@source:repo/Reader"]);
    assert.deepEqual(r.changed.map((c) => [c.package_id, c.fields.map((f) => f.field + "+" + f.added.join())]), [["Writer", ["queue_writes+orders"]]]);
    fs.writeFileSync(base, "{\"not\": \"a fingerprint\"}\n");
    assert.match(run(ix, "diff", "--baseline", base).stderr, /not an automation-index fingerprint/);
    assert.match(run(ix, "diff", "--package", "Proc.N", "--from", "1.0.0", "--to", "9.9").stderr, /not in the index/);
  });

  test("SARIF: valid shape, rule metadata, real file locations and stable fingerprints", () => {
    const out = path.join(tmp, "f.sarif");
    assert.equal(run(ix, "export", "--format", "sarif", "--out", out).status, 0);
    const s = JSON.parse(fs.readFileSync(out, "utf8"));
    assert.equal(s.version, "2.1.0");
    const { rules } = s.runs[0].tool.driver;
    for (const r of rules) {
      assert.ok(r.id && r.shortDescription.text && r.fullDescription.text && r.help.text, r.id);
      assert.ok(["error", "warning", "note"].includes(r.defaultConfiguration.level));
    }
    assert.equal(rules.find((r) => r.id === "AI-SEC-001").properties["security-severity"], "9.0");
    const sec = s.runs[0].results.find((r) => r.ruleId === "AI-SEC-001");
    assert.equal(sec.level, "error");
    assert.equal(rules[sec.ruleIndex].id, "AI-SEC-001");
    const loc = sec.locations[0].physicalLocation;
    const file = fileURLToPath(loc.artifactLocation.uri);
    assert.ok(fs.existsSync(file) && file.endsWith("Main.xaml"), file);
    const lines = fs.readFileSync(file, "utf8").split("\n");
    assert.match(lines[loc.region.startLine - 1], /HttpClient/);
    assert.ok(!JSON.stringify(s).includes("N3wS3cretValue"));
    // a finding on a source project points into the project folder, not the index copy
    const src = s.runs[0].results.find((r) => r.properties.package?.startsWith("Reader@") && r.locations?.[0].physicalLocation);
    assert.ok(fileURLToPath(src.locations[0].physicalLocation.artifactLocation.uri).startsWith(fs.realpathSync(repo)) ||
      fileURLToPath(src.locations[0].physicalLocation.artifactLocation.uri).startsWith(repo));
    // two secrets in one file are two alerts
    const secFp = s.runs[0].results.filter((r) => r.ruleId === "AI-SEC-001").map((r) => r.partialFingerprints["automationIndexFinding/v1"]);
    assert.equal(secFp.length, 2);
    assert.equal(new Set(secFp).size, 2);
    // a finding inherited from a library points into the library's files
    const inh = s.runs[0].results.find((r) => r.ruleId === "AI-RES-002" && r.properties.package.startsWith("LibUser@"));
    assert.ok(inh, "inherited wrong-folder finding");
    assert.match(fileURLToPath(inh.locations[0].physicalLocation.artifactLocation.uri), /Shared\.Lib@1\.0\.0_[0-9a-f]+[\/\\]Get\.xaml$/);
    // fingerprints survive a second review
    assert.equal(run(ix, "review").status, 0);
    const out2 = path.join(tmp, "f2.sarif");
    assert.equal(run(ix, "export", "--format", "sarif", "--out", out2).status, 0);
    const fp = (doc) => doc.runs[0].results.map((r) => r.partialFingerprints["automationIndexFinding/v1"]).sort();
    assert.deepEqual(fp(JSON.parse(fs.readFileSync(out2, "utf8"))), fp(s));
  });

  test("SARIF never points through a link that leaves the package, and keeps identical findings apart", () => {
    const db = openDb(ix);
    const v = db.prepare("SELECT version FROM package WHERE package_id='Reader' AND version NOT LIKE '%Copy%'").get().version;
    db.prepare("INSERT INTO finding VALUES ('AI-SRC-003','Warning','Reader',?,NULL,'link/Main.xaml','test','link/Main.xaml:1',NULL)").run(v);
    // two findings identical in every column (two secrets in one property) are still two alerts
    for (let i = 0; i < 2; i++) db.prepare("INSERT INTO finding VALUES ('AI-SEC-001','Critical','Reader',?,NULL,'conn.password','x','Main.xaml:1 [Q1]',NULL)").run(v);
    db.close();
    const s = JSON.parse(run(ix, "export", "--format", "sarif").stdout);
    const twins = s.runs[0].results.filter((x) => x.properties.evidence === "Main.xaml:1 [Q1]").map((x) => x.partialFingerprints["automationIndexFinding/v1"]);
    assert.equal(new Set(twins).size, 2);
    const r = s.runs[0].results.find((x) => x.properties.evidence === "link/Main.xaml:1");
    const uri = r.locations?.[0].physicalLocation?.artifactLocation.uri;
    if (uri) assert.ok(fs.realpathSync(fileURLToPath(uri)).startsWith(fs.realpathSync(path.join(repo, "Reader")) + path.sep), uri);
    assert.equal(run(ix, "review").status, 0);
  });

  test("baseline versions follow NuGet order: a release outranks its prerelease", async () => {
    const { cmpVersion } = await import(pathToFileURL(SCRIPT).href);
    assert.ok(cmpVersion("1.0.0-beta", "1.0.0") < 0);
    assert.ok(cmpVersion("1.10.0", "1.9.0") > 0);
    assert.ok(cmpVersion("1.0.0-beta.2", "1.0.0-beta.10") < 0);
    assert.ok(cmpVersion("1.0.0-alpha", "1.0.0-beta") < 0);
    assert.equal(cmpVersion("1.0.0+build5", "1.0.0"), 0);
  });

  test("CycloneDX: packages, libraries and hosts as one consistent dependency graph", () => {
    const r = run(ix, "export", "--format", "cyclonedx");
    assert.equal(r.status, 0, r.stderr);
    const b = JSON.parse(r.stdout);
    assert.equal(b.bomFormat, "CycloneDX");
    assert.equal(b.specVersion, "1.6");
    assert.match(b.serialNumber, /^urn:uuid:[0-9a-f-]{36}$/);
    const refs = new Set([...b.components, ...b.services].map((c) => c["bom-ref"]));
    assert.equal(refs.size, b.components.length + b.services.length); // unique
    for (const d of b.dependencies) {
      assert.ok(refs.has(d.ref), d.ref);
      for (const x of d.dependsOn) assert.ok(refs.has(x), x);
    }
    const lib = b.components.find((c) => c.name === "Shared.Lib");
    assert.equal(lib.purl, "pkg:nuget/Shared.Lib@1.0.0");
    assert.equal(lib.type, "library");
    assert.match(lib.hashes[0].content, /^[0-9a-f]{64}$/);
    const writer = b.components.find((c) => c.name === "Writer");
    assert.equal(writer.purl, undefined); // a source folder has no package URL
    const deps = b.dependencies.find((d) => d.ref === writer["bom-ref"]).dependsOn;
    assert.ok(deps.includes("pkg:nuget/Shared.Lib@1.0.0"));
    assert.ok(deps.includes("pkg:nuget/UiPath.System.Activities@25.10.0"));
    assert.ok(b.dependencies.find((d) => d.ref === lib["bom-ref"]).dependsOn.includes("host:lib.example.com"));
    assert.ok(!b.services.some((s) => s.name.includes("&")));
    // two checkouts with one name stay two components
    assert.equal(b.components.filter((c) => c.name === "Reader").length, 2);
    // a differently cased reference reaches the indexed library
    const lu = b.components.find((c) => c.name === "LibUser");
    assert.deepEqual(b.dependencies.find((d) => d.ref === lu["bom-ref"]).dependsOn, ["pkg:nuget/Shared.Lib@1.0.0"]);
    // a dependency that was never opened has an unknown graph, not an empty one
    assert.ok(!b.dependencies.some((d) => d.ref === "pkg:nuget/UiPath.System.Activities@25.10.0"));
    assert.ok(b.compositions.some((c) => c.aggregate === "unknown" && c.dependencies.includes("pkg:nuget/UiPath.System.Activities@25.10.0")));
  });
});
