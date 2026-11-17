# Migration packs

Migration knowledge for third-party automation frameworks: per framework, an inventory script that reads the framework's export, a **source guide** (detection, inventory, signals, component → UiPath skill mapping, composite actions, UI target locators, test data, evidence) and a **selectors guide** (locator formats and their attribute-by-attribute translation into UiPath targets). Consumed by two skills of the [UiPath/skills](https://github.com/UiPath/skills) plugin: `uipath-planner` extracts the genome, `uipath-genome` builds it, from a local folder the user points to.

## Layout

```
README.md                      this index — § Frameworks is what both skills read first
CONTRACT.md                    what every framework pack provides
LICENSE                        Apache License 2.0
automation-anywhere/
  source-guide.md              framework facts; command block at the top runs the script
  selectors-guide.md           locator formats → UiPath tag.attribute, one table per technology
  schema.md                    every key path (or API member) of the export, and what the script does with it
  scripts/a360-export-inventory.py   profile | cards | dump | targets | data | check
  docs/                        records for people — § For people
blue-prism/
  source-guide.md              framework facts; command block at the top runs the script
  selectors-guide.md           locator formats → UiPath tag.attribute, one table per technology
  schema.md                    every key path (or API member) of the export, and what the script does with it
  scripts/bprelease-inventory.py   profile | cards | dump | targets | data | check
  docs/                        records for people — § For people
```

## Frameworks

`<PACK_DIR>` is the root of this folder. Every script is run before any export file is read. Every `<framework>/docs/` is for people and holds no source facts: an agent extracting or building a migration never reads or cites it.

| Framework | Detection | Folder | Script |
|---|---|---|---|
| Automation Anywhere (Automation 360 Control Room export, incl. bots migrated from Enterprise 11) | Folder with `Bots/My Tasks/…` and/or `Bots/My MetaBots/…` holding extension-less JSON bot files (`nodes`, `variables`, `packages`), `<Bot>Metadata/*.png` beside them; a Control Room bot export adds `manifest.json` (`files[]` with content types and scanned dependencies) at the root | [automation-anywhere/](automation-anywhere/) — selectors guide covers Java (Access Bridge), HTML, Active Accessibility, UI Automation, SAP GUI (recorder captures and scripting-wrapper ids) | `<PACK_DIR>/automation-anywhere/scripts/a360-export-inventory.py` (`check`, `profile`, `cards`, `dump <bot id> [--compact]`, `raw <bot id> --lines/--find/--var`, `targets`, `data`) — `check` first; never read a bot file raw (base64 blobs, repeated variable dictionaries), check a finding against its nodes through `raw` |
| Blue Prism (release package or single process / object exports) | A `.bprelease` file (XML, root `<release>` with `<contents count>` holding `process`, `object`, `work-queue`, `environment-variable` and group items), or `.bpprocess` / `.bpobject` files (root `<process>`), or a folder holding several of them; processes and objects are `<stage>` graphs linked by stage ids across `<subsheet>` pages; business objects carry an `<appdef>` application model | [blue-prism/](blue-prism/) — selectors guide covers SAP GUI (scripting mode), web (browser extension), Win32, UI Automation, Active Accessibility | `<PACK_DIR>/blue-prism/scripts/bprelease-inventory.py` (`check`, `profile`, `cards`, `dump <id>[::<page>] [--full]`, `raw <id>[::<page>] --stage/--find/--element`, `targets`, `data`) — `check` first; never read the release raw (stages are unordered in the file; the flow is the links), check a finding against its stages through `raw` |

No row matches the given files → the skill says which framework they appear to come from and that this pack has no guide for it; it never improvises. Adding one: [CONTRACT.md § Adding a framework](CONTRACT.md).

## How the skill uses this pack

1. `uipath-planner` asks for the location of this folder when the source is not a UiPath project (extraction), and `uipath-genome` when the Source Map names a framework other than UiPath (build), unless the request already gave it. Each reads this file's § Frameworks and selects the row whose detection matches (extraction) or whose framework the Source Map names (build).
2. **Extraction** (`uipath-planner` `extract`) runs the row's script into a scratch folder outside the genome's folder, reads `<framework>/source-guide.md` in full, and writes the genome and its Source Map. A suspected source defect is checked against the export itself before the genome calls it one: through `raw` for a structured export, in the cited lines for code ([CONTRACT.md § Reading the source directly](CONTRACT.md)). Nothing from the export or the catalogs is written beside the genome.
3. **Build** (`uipath-genome` `build`) re-runs the script at migration preflight into the build's working folder, reads `source-guide.md` § Composite actions and `selectors-guide.md` by technology section together with the skill's selector translation for each technology the source recorded, and migrates UI targets and test data.

A pack cites the skill's rules by their subject in the skill's own words ([CONTRACT.md](CONTRACT.md) rule 4); `<SKILL_DIR>` is the folder of the skill running the command (`uipath-planner` for `check-genome.mjs`), `<PACK_DIR>` this folder.

## For people

Not for agents (§ Frameworks).

- `<framework>/docs/source-repositories.md` — public repositories each framework's pack drew on, each with its licence: [automation-anywhere/](automation-anywhere/docs/source-repositories.md), [blue-prism/](blue-prism/docs/source-repositories.md).
