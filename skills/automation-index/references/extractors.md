# Extractors: what each one reads, how far to trust it, what defeats it

Every edge names its extractor. A `+config` or `+trace` suffix means the value was resolved from a Config file or traced through variables and arguments, so the source does not contain it literally.

| Extractor | Reads | Confidence |
|---|---|---|
| `xaml` | Activity properties with a known meaning: Get/Set Asset and Credential, queue activities, Invoke Process / Run Job, storage buckets, Open Browser / Go To URL, Use Application/Browser `TargetApp`, HTTP Request, Database Connect, mail activities, Invoke Code, Integration Service activities (`ConnectorKey`, `ConnectionId`), Office activity scopes | high when literal |
| `xaml:selector` | The root node of every selector (`app`, `url`, tag) | medium: shows the app the selector targets |
| `xaml:literal`, `cs:literal` | Literal `http(s)://` strings in expressions and code | medium |
| `xaml:code` | URLs, Config keys and credential patterns inside Invoke Code / PowerShell bodies | medium |
| `object_repository`, `object_repository:selector` | `.objects/` screens and elements, labelled with the app / screen / element names | high / medium |
| `studio_applications_metadata` | `applicationsMetadata.json`, which Studio writes at publish and Orchestrator shows as "used applications" | high |
| `xaml` connector configuration | The gzip+base64 `Configuration` of Integration Service activities: connector, operation, object, HTTP method and path. Cached lookup values and schemas are never read | high |
| `package_bindings` | `bindings_v2.json` resources (assets, queues, processes, connections). Present even in compiled packages. | high |
| `config_assets_sheet` | Rows of the Config **Assets** sheet (REFramework loads every row with Get Asset at start) | medium |
| `config_file` | Credential-like values stored in Config files (always redacted) | medium |
| `cs` | Coded workflows: `GetAsset("x")`, `GetCredential("x")`, queue calls, `RunJob("x")`, storage calls, Object Repository descriptors, `RunWorkflow("x")` | medium |
| `dll_strings` | Strings in compiled assemblies: URLs, selectors, relative API paths, workflow class names | **low**: a string in a DLL is not proof of use |
| `dll_strings_match` | Exact matches between DLL strings and Orchestrator asset, queue, bucket and process names from the crawl | **low** |

## Resolution

- **Config keys.** `in_Config("Key")` and `Config("Key")`, or any dictionary whose name ends in `Config` (`io_DictConfig`, `DictionaryConfig`), are looked up in every `*Config*.xlsx` and `*config*.json` in the package. A Settings or Constants value replaces the expression only when the whole expression is the lookup (optionally `.ToString` or `.Trim`). When the lookup is part of a larger expression, the fact stays dynamic and its detail says `expression combines Config 'K' (= 'value')`: a hint, not a value. An **Assets** sheet value is an asset *name*, so the edge stays dynamic and the detail says `value of Orchestrator asset 'X'`.
- **Tracing.** A dynamic value that is just an identifier (`in_QueueName`, `QueueName`) is followed through `Assign` statements earlier in the same file and through the arguments each caller passes in Invoke Workflow File, up to three hops. An assignment later in the file is kept only as a hint (`possible value ... execution order not proven`), because loops and flowcharts do not run in file order. A variable filled by Get Asset is reported as `value read at runtime from asset 'X'`. When several earlier assignments give different values, each is kept with `low` confidence: branches and overwrites are not analysed.
- **Libraries.** Edges of a library package are inherited by each process that pins that library version. Use `v_edge_all` or `v_process_edge`; the `via` column shows the chain.
- **Reachability.** A breadth-first walk from the entry points (`project.json` main, `entryPoints`, `entry-points.json`, test cases) over static Invoke Workflow File calls. Like Studio, `WorkflowFileName` is resolved from the project root; a path that leaves the project is left unresolved. A file that a dynamic invoke could reach, or any file when a reachable workflow or `project.json` could not be read, is `NULL` (unknown), never `0`. In libraries, every public workflow counts as an entry point.

## What defeats extraction

| Situation | What the index does | What to tell the user |
|---|---|---|
| Package published without sources (common for Studio Web and cross-platform publishing) | `coverage.status = compiled_only`; only bindings, `applicationsMetadata.json`, `project.json` and low-confidence DLL strings | Facts for that automation are partial. Ask the owner for the source repository, then index it with `extract --source` |
| Value built at runtime (string concatenation, API response, data row) | Edge with `normalized = '<dynamic>'`, the expression kept in `value` | The target is not knowable statically. Show the expression and its file:line |
| Value read from an asset at runtime | Dynamic edge, detail names the asset | The real value lives in Orchestrator. The index never reads asset values |
| Agent, Flow, Maestro, API workflow, app or other non-RPA package | `coverage.status = unsupported`; only its `bindings_v2.json` is read (processes it calls, queues, assets, connections) | Its own logic is out of scope; calls declared in a `.flow` file but not in its bindings are not seen |
| Version ranges in dependencies (`[1.0, 2.0)`) | `dep_version` NULL; the library is not downloaded | The library version the robot loads is decided at runtime |
| Custom activity packages (compiled code) | Listed in `dependency`, not opened | Behaviour inside them is invisible |
| Selectors with variables (`{{Title}}`) | Kept as written | The app is still known from the `app` attribute |
| Malformed XAML | `workflow.parse_error`; finding AI-SRC-003 | Studio may still open it; check the file |
| Activities inside a Comment Out block | Ignored: no facts, no calls, not counted; the number is noted in coverage | Disabled code does not run |
| DTD declarations in a file (entities, attribute defaults) | Refused as a parse error; entities are never expanded | Unusual for Studio files; check where the file came from |
| Files over 50 MB, packages over 1 GB inflated, more than 200,000 zip entries, wrong CRC | The file or package is refused and recorded with the reason | Usually a corrupt or unusual package |
| `project.json` that is not valid JSON | `coverage.status = partial`: workflows are read, dependencies and entry points are unknown | Fix the file, or treat the automation's library links as unknown |
| Symbolic links in a source folder that point outside the project | Skipped; links that stay inside the project are read | Files outside a project are never copied into the index |
| Zip entries with `..`, absolute or drive paths | Skipped | Nothing is written outside `INDEX_DIR` |

## Where the facts are

The extracted project tree of every package is under `INDEX_DIR/src/PACKAGE@VERSION_HASH/` (the hash keeps names that look alike apart; each card's Provenance section gives the exact folder). Grep it when the database does not answer a question, and cite `file:line`. It is a copy of the customer's source, including anything sensitive in it. Keep `INDEX_DIR` where the source repository would be kept.
