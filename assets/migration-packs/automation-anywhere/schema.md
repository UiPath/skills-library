# Automation 360 export schema — key paths

Every key path at which a bot document or the export's `manifest.json` holds a value, with what it means and whether a rendering of the inventory script shows it. The script's `check` walks every bot and the manifest and fails on a path this table does not list, and on a path listed as *ignored while* one of its values that holds any other value — a new product version or a command family no export has exercised yet surfaces there instead of being skipped in silence. Read this file when `check` reports such a path: read the path's raw values in the bot file, then add the row (and, when the value is behaviour, the rendering).

Paths are typed: a bot document is `DOC`, a node `NODE` (commands, their `children` and their `else` / `catch` / `finally` `branches`), a node's attribute `ATTRIBUTE`, every value object `VALUE` (an attribute's value, a default, a list item, a dictionary entry's value, a table cell, a sub-bot binding), a dictionary entry `DICTENTRY`, a capture criterion `CRITERION`, a variable `VARIABLE`; `[]` is a list item. A path restarts at each typed object, so a value nested anywhere has the same path. A row's first cell may hold glob patterns (`*` matches any characters); the first row that matches decides. Status: **rendered** — a rendering shows the value (`check` verifies it where it tests that surface); **structure** — the key is the shape the renderer reads (a command's name, a value's type); **ignored** — never behaviour, for the reason given; **ignored while** `v` … — ignored while it holds one of the listed values, and any other value is a finding.

| Path | Meaning | Status |
|---|---|---|
| `DOC.nodes`, `DOC.variables` | the bot's command tree and variables | structure — typed below |
| `DOC.packages[].name`, `DOC.packages[].version` | the command packages the bot uses and their build versions | rendered — dump header, profile § PACKAGES |
| `DOC.migrationJournalReviewIds[]` | Bot Migration review ids: the bot was converted from Enterprise 11 | rendered — dump header, profile |
| `DOC.properties.automationPriority` | run priority | rendered — dump header |
| `DOC.properties.botCodeVersion` | the bot's own edit version | rendered — dump header |
| `DOC.properties.timeout` | the bot's run-time limit (`7200s`; `0s` = none) | rendered — dump header |
| `DOC.properties.runInChildWindow`, `DOC.properties.runInChildWindowMode` | the bot runs in a child session (`DESKTOP`, `WINDOW`) | rendered — dump header |
| `DOC.properties.improvedNumberSupport` | the number model the bot's arithmetic uses | rendered — dump header |
| `DOC.swimlaneStacking` | editor layout of the flow view | ignored — presentation |
| `DOC.breakpoints[]` | the node uids the author set a breakpoint on; a breakpoint pauses only a session of the Debug tool (vendor documentation) | ignored — debugging only, never run behaviour |
| `NODE.uid` | the node's id inside one bot; copied bots share uids | ignored — a node is cited by bot id + editor line (the target catalog carries it for captures) |
| `NODE.commandName`, `NODE.packageName` | the command | structure — every line's label |
| `NODE.disabled` | the node is greyed out; its whole subtree is dead | rendered — `[off]` |
| `NODE.children`, `NODE.branches`, `NODE.attributes` | body, `else` / `catch` / `finally` arms, settings | structure — typed |
| `ATTRIBUTE.name` | the setting's name | structure — `name=value` |
| `ATTRIBUTE.operator` | the join operator of a condition chain (`AND`, `OR`); `NONE` = no join | rendered — the condition text |
| `VALUE.type` | the value's type, which names the channel holding it | structure — the channel read |
| `VALUE.string`, `VALUE.number`, `VALUE.boolean`, `VALUE.expression` | the value's channels: the one its type names holds it, an expression (a variable reference) wins | rendered — dump, cards, catalogs |
| `VALUE.variableName` | the variable a `returnTo` or a VARIABLE value names | rendered — `$name$` |
| `VALUE.exceptionName` | the exception type a `catch` handles | rendered — `exceptionType=` |
| `VALUE.label` | the editor's display label of an option picked from a list (the system-information item `USERNAME`); the option itself is the value's `string` channel | ignored — presentation: the channel holds the value |
| `VALUE.conditionalName` | the condition's kind (`stringVariable`, `fileExists`, a capture …) | structure — the condition's rendering |
| `VALUE.iteratorName` | the loop's kind (`loop.iterators.times`, `iterators.resultset` …) | structure — `for <kind>(…)` |
| `VALUE.packageName` | the package defining a condition or iterator kind, or computing a VARIABLE value (the logging package's log file) | rendered — `$<package>:<name>$` on a VARIABLE value; on a condition or iterator the kind is rendered instead |
| `VALUE.sessionTarget` | a session's scope: `LOCAL` to the bot, `GLOBAL` shared with the bots it calls; `NONE` on every other value | rendered — `session:<name> (<scope>)` |
| `VALUE.securelyRecorded` | the value was recorded as a secure (masked) input | ignored while `false` — true would mark a secret the harvest must learn |
| `VALUE.taskbotSelf` | a sub-bot reference to the bot itself | ignored while `false` |
| `VALUE.repository` | a file value addressed in the Control Room repository | ignored while `false` |
| `VALUE.credential.lockerName`, `VALUE.credential.name`, `VALUE.credential.attributeName` | a Credential Vault reference | rendered — `vault:<locker>/<name>.<attribute>` |
| `VALUE.window.name`, `VALUE.window.class`, `VALUE.window.path` | a recorded window: title (with `*` wildcards), class, executable | rendered — `win(…)`, target catalog |
| `VALUE.window.presetType` | a special target: `CURRENTLY_ACTIVE` (whatever has focus), `TASKBAR`; `NONE` = the recorded window | rendered — `win(<CURRENTLY_ACTIVE>)` |
| `VALUE.window.nameCaseInsensitive` | the title is matched without regard to case | rendered — `nameCaseInsensitive` |
| `VALUE.window.handle`, `VALUE.window.location.*`, `VALUE.window.size.*` | the capture session's window handle, position and size | ignored — run-time values of the capture machine |
| `VALUE.window.windowType` | the window kind: `NATIVE`, or a browser tab picked through the window's Browser option (`BROWSER_IE` = an Internet Explorer tab, vendor documentation) | rendered — `windowType=<kind>` when not `NATIVE`, target catalog `windowType` |
| `VALUE.window.browserTabUrl` | the URL of the browser tab at capture | rendered — `tabUrl=`, target catalog `tabUrl` |
| `VALUE.window.nameRegex.*` | the title matched by a regular expression instead of a wildcard title: `list[]` of `STRING` or `PATTERN` conditions with their text, and `regexFlags` (vendor documentation) | rendered — `pattern"…"` / `string"…"` and `regexFlags=` in `win(…)`, target catalog `titleRegex` |
| `VALUE.window.type` | the window object's own type marker | ignored while `WINDOW` |
| `VALUE.windowResize.*` | the command resizes its window to this width and height before it acts — the Resize window option (vendor documentation) | rendered — `resize(width=…, height=…)` after the window, target catalog `resize` |
| `VALUE.window.virtualType` | virtual-channel window (a remote or virtual desktop) | ignored while `VIRTUAL_TYPE_NONE` |
| `VALUE.window.isChild` | a child window | ignored while `false` |
| `VALUE.window.browserTabOpenNewWindow` | the browser tab opens as a new window | ignored while `false` |
| `VALUE.table.schema[].name` | a table literal's column names | rendered — `table(<columns>)[…]` |
| `VALUE.table.schema[].type`, `VALUE.table.schema[].subtype` | a table literal's column types | ignored — every cell carries its own type |
| `VALUE.record.schema[].name` | a record literal's field names, paired in order with `record.values[]` | rendered — `record{<name>: <value>, …}` |
| `VALUE.record.schema[].type`, `VALUE.record.schema[].subtype` | a record literal's field types | ignored — every value carries its own type |
| `VALUE.region.capture.screenshotMetadataPath`, `VALUE.region.capture.thumbnailMetadataPath` | the recorder's pictures of a captured screen area under `<Bot>Metadata/` | ignored — design-time evidence, never read for logic |
| `VALUE.region.capture.screenshotRectangle.*`, `VALUE.region.capture.thumbnailRectangle.*` | where those pictures were cropped | ignored — presentation |
| `VALUE.region.capture.securelyRecorded` | the area's picture was taken of a secure field | ignored while `false` |
| `VALUE.coordinate` | a screen point inside the command's window (a mouse click at a point, no captured control): `x` and `y` are values | structure — `point(<x>,<y>)`, target catalog window operation |
| `VALUE.coordinate.capture.screenshotMetadataPath`, `VALUE.coordinate.capture.thumbnailMetadataPath` | the recorder's pictures of the point under `<Bot>Metadata/` | ignored — design-time evidence, never read for logic |
| `VALUE.coordinate.capture.screenshotPoint.*`, `VALUE.coordinate.capture.screenshotX`, `VALUE.coordinate.capture.screenshotY`, `VALUE.coordinate.capture.screenshotWidth`, `VALUE.coordinate.capture.screenshotHeight` | where the point sits in the recorder's pictures | ignored — presentation |
| `VALUE.coordinate.capture.securelyRecorded` | the point's picture was taken of a secure field | ignored while `false` |
| `VALUE.variableMapFilter[]` | the variable types a `String.evaluate` (Evaluate variable) looks the named variable up among — string variables only (vendor documentation) | rendered — the value's fields in the dump line |
| `DICTENTRY.key` | a dictionary key: a sub-bot binding's variable, a map's key | rendered |
| `VALUE.uiObject.technologyType`, `VALUE.uiObject.controlType` | the capture's technology and control type | rendered — target catalog, dump |
| `VALUE.uiObject.browserType` | the browser a capture was recorded in (`EDGE`, `UNKNOWN_BROWSER`) | rendered — target catalog `browserType` |
| `VALUE.uiObject.isElevated` | the captured application ran elevated (as administrator) | rendered — target catalog `elevated` when true |
| `VALUE.uiObject.blob` | the recorder's captured object tree (base64 JSON) | rendered — decoded into the target catalog `object` (name, role, class, id, path, bounds, every property the recorder read) |
| `VALUE.uiObject.capture.screenshotMetadataPath`, `VALUE.uiObject.capture.thumbnailMetadataPath` | the capture's screenshots under `<Bot>Metadata/` | rendered — target catalog `screenshots` |
| `VALUE.uiObject.capture.screenshotRectangle.*`, `VALUE.uiObject.capture.thumbnailRectangle.*` | where the screenshots were cropped | ignored — presentation |
| `VALUE.uiObject.capture.securelyRecorded` | the capture's screenshot was taken of a secure field | ignored while `false` |
| `CRITERION.enabled` | the property was used for matching | rendered — the catalog lists enabled criteria only |
| `CRITERION.securelyRecordedRemoveDisabled` | a recorder flag beside a criterion's secure-recording state; its meaning is not documented | ignored while `false` — true is undocumented |
| `VARIABLE.name`, `VARIABLE.type`, `VARIABLE.subtype` | the variable | rendered — VARIABLES legend |
| `VARIABLE.input`, `VARIABLE.output`, `VARIABLE.readOnly` | interface flags and constants | rendered — VARIABLES legend `[in out readOnly]` |
| `VARIABLE.description` | the author's description | rendered — VARIABLES legend |
| `VARIABLE.inputRequired` | the input must be bound | ignored while `false` |
| `VARIABLE.workItem` | the variable holds a work item (a Control Room queue item) | ignored while `false` — true means the bot is a queue consumer |
| `VARIABLE.key` | the variable's key in the Control Room | ignored |
| `VARIABLE.constant` | a flag beside `readOnly`, which is the documented constant | ignored while `false` — true is undocumented |
| `VARIABLE.sessionType` | the package a SESSION variable's session belongs to (`Excel_MS`) | rendered — VARIABLES legend `SESSION(<package>)` |
| `MANIFEST.files[].path`, `MANIFEST.files[].contentType` | the exported files and their kinds | rendered — profile § MANIFEST |
| `MANIFEST.files[].scannedDependencies[]`, `MANIFEST.files[].manualDependencies[]` | what the Control Room scanned and what the author added | rendered — profile § MANIFEST cross-check |
| `MANIFEST.files[].excluded` | the file was left out of the export | rendered — profile § MANIFEST |
| `MANIFEST.files[].description`, `MANIFEST.files[].author` | per-file description and author | rendered — profile § MANIFEST counts; the bot's own description is read from the bot |
| `MANIFEST.files[].metadataForFile` | the bot a screenshot belongs to | ignored — the screenshots are listed per capture |
| `MANIFEST.files[].tags[].namespace`, `MANIFEST.files[].tags[].value` | platform tags (`INTENDED_TARGET`, `COMPATIBLE_TARGET` = `WINDOWS`) | rendered — profile § MANIFEST file tags |
| `MANIFEST.files[].newPath`, `MANIFEST.files[].*NewPaths[]` | import re-mapping of paths | ignored — import-time, not the bots' |
| `MANIFEST.packages[]*` | export-level packages | rendered — profile § MANIFEST count |
