# Blue Prism release schema — element paths

> **Version** followed by a number (version 6, version 7.4) is a version of the source platform.

Every path at which a release (`.bprelease`) or a single `.bpprocess` / `.bpobject` export holds a value, with what it means and whether a rendering of the inventory script shows it. The script's `check` walks every file of the export and fails on a path this table does not list, and on a path listed as *ignored while* that holds any other value. A newer version, or a stage feature no release has exercised yet, surfaces there instead of being skipped in silence. Read this file when `check` reports such a path: read the path's raw values with `raw` (`raw <EXPORT> <NAME> --find <text>`), then add the row (and, when the value is behaviour, the rendering).

Paths are typed. A release is `RELEASE`, its `<contents>` `CONTENTS`, and a process or object entry of the contents `WRAPPER` (the element carrying the GUID and `published`). The process or object definition inside it (the root of a single-file export) is `PROC`; a page definition is `SUBSHEET`, a stage `STAGE`, the application model `APPDEF`, one of its elements `ELEMENT` (a surface-automation region and a region container are read as one) and an element group `GROUP`. A collection's initial row is `ROW` and a cell of it `FIELD`. Any other item of the contents is `ITEM:<kind>` (`ITEM:work-queue`). A path restarts at each typed element, so a stage has the same path on every page and an element at every depth of the model. `@name` is an attribute; a path without `@` is the element's text. An element with no text, attributes or children is a path holding `<present>`: an empty element is often a flag. A row's first cell may hold glob patterns (`*` matches any characters); the first row that matches decides.

Status:
- **rendered**: a rendering shows the value; `check` verifies it where it tests that surface.
- **structure**: the key is the shape the renderer reads (a link, a type, an id).
- **ignored**: never behaviour, for the reason given.
- **ignored while** `v` …: ignored while it holds one of the listed values; any other value is a finding.

| Path | Meaning | Status |
|---|---|---|
| `RELEASE.name`, `RELEASE.created`, `RELEASE.package-id`, `RELEASE.package-name`, `RELEASE.user-created-by`, `RELEASE.release-notes` | the release's identity: name, export time, package, exporting user, notes | rendered — profile § EXPORT |
| `CONTENTS@count` | the number of exported items | rendered — profile § EXPORT |
| `WRAPPER@id` | the process's or object's GUID | rendered — `guid=` in the dump header; the six-digit id is derived from it |
| `WRAPPER@name` | the process's or object's name | rendered — every rendering names it |
| `WRAPPER@published` | the process is published (runnable from the Control Room) | rendered — dump header, profile, cards |
| `PROC@name`, `PROC@version`, `PROC@bpversion`, `PROC@narrative`, `PROC@runmode`, `PROC@shared` | the definition: name, version, the source platform version that saved it, description, an object's run mode (`Foreground`, `Background`, `Exclusive`), a shareable object (one instance for every caller of a process run) | rendered — dump header; run mode and shared also on the profile objects row and the card header |
| `PROC@type` | `object` marks a business object | structure — the process / object kind |
| `PROC@byrefcollection` | collections are passed by reference | ignored while `true` |
| `PROC@processrunningmessage` | the message the source platform's Desktop product shows its user while the process runs, replacing the default one *[Inferred from the name: the Process Information stage's text to show whilst a Desktop process runs (vendor documentation)]* | ignored — the Desktop user's screen, never the run's behaviour |
| `PROC@disableversioning` | not documented; read from the name, the definition keeps no version history | ignored while `false` |
| `PROC.preconditions`, `PROC.preconditions.condition@narrative` | an object's stated preconditions | structure / rendered — `PRECONDITIONS` in the dump header |
| `PROC.endpoint@narrative` | an object's stated end point | rendered — `ENDPOINT` in the dump header |
| `PROC.view.*`, `SUBSHEET.view.*` | the diagram's camera position and zoom | ignored — presentation |
| `SUBSHEET@subsheetid` | the page's id; stages name it in `subsheetid`, page calls in `processid` | structure — the page a stage is on |
| `SUBSHEET.name` | the page's name | rendered — `=== PAGE «name»`, cards pages |
| `SUBSHEET@type` | `Normal`, `CleanUp` (an object's Clean Up page) | rendered — `[CleanUp]` on the page head, cards pages |
| `SUBSHEET@published` | the page is an action other processes and objects may call | rendered — `published=True` on the page head |
| `STAGE@stageid` | the stage's id; links name it | structure — the flow; `raw --stage` takes it |
| `STAGE@type` | the stage type (`Action`, `Decision`, `Data`, …) | structure — the line's label |
| `STAGE@name` | the stage's name (repeats inside a page) | rendered — `‹name›` on the stage's line |
| `STAGE.subsheetid` | the page the stage is on; none = the main page (a process) or Initialise (an object) | structure — the page |
| `STAGE.onsuccess`, `STAGE.ontrue`, `STAGE.onfalse`, `STAGE.choices.choice.ontrue` | links to the next stage | structure — the dump's execution order |
| `STAGE.groupid` | pairs a Loop, Choice or Wait start with its end | structure — the loop body, the otherwise / timeout branch |
| `STAGE.display@x`, `STAGE.display@y`, `STAGE.display@w`, `STAGE.display@h`, `STAGE.displayx`, `STAGE.displayy`, `STAGE.displaywidth`, `STAGE.displayheight` | the stage's position and size on the diagram, as attributes (version 7) or as elements (version 6): a stage's centre, a Block's top-left corner and extent | structure — a Block's area decides which recovery covers a stage (`RECOVER (scope: block «…»)`) |
| `STAGE.font@*` | the stage's font | ignored — presentation |
| `STAGE.narrative` | the author's description of the stage, the page's description on its `SubSheetInfo` | rendered — `# …` after the stage's line (not on an internal action, whose description is the vendor's), `description:` on the page head |
| `STAGE.loginhibit`, `STAGE.loginhibit@onsuccess` | stage logging: no element = every run logged, `onsuccess="true"` = errors only, a bare element = never logged *[Inferred from the three stage logging options]* | rendered — cards and profile `logging: all / errors-only / none` |
| `STAGE.loginhibitparameters` | the stage's parameters are left out of the log | ignored — logging detail |
| `STAGE.breakpoint@type`, `STAGE.breakpoint.condition` | a debugging breakpoint | ignored — the designer's, never the run's |
| `STAGE.warningthreshold` | a stage's run-time warning threshold | ignored while `0` |
| `STAGE.inputs.input@name`, `STAGE.outputs.output@name` | a parameter's name | rendered — on the stage's line |
| `STAGE.inputs.input@expr` | a call's input value (an expression); blank, the input is left blank | rendered — `name=value`, a blank one by name after `blank:` |
| `STAGE.outputs.output@stage` | the data item a call's output goes to, or a page's End output comes from | rendered — `name→[item]`, `name:type←[item]` |
| `STAGE.inputs.input@stage` | the data item a page's Start input goes to | rendered — `name:type→[item]` on START |
| `STAGE.inputs.input@type`, `STAGE.outputs.output@type` | the parameter's type | rendered — Start, End and Code stages (the definitions); on a call it repeats the callee's |
| `STAGE.inputs.input@narrative`, `STAGE.outputs.output@narrative` | the parameter's description | rendered — Start and End stages (the definition); on a call it is the callee's description, copied |
| `STAGE.inputs.input@friendlyname`, `STAGE.outputs.output@friendlyname` | the parameter's display label | ignored — repeats the name |
| `STAGE.inputs.input.validator@type`, `STAGE.inputs.input.validator@parameter` | a range the callee's input accepts (`RangeParam`, `0:25000`) | ignored — the callee's parameter constraint, copied into the call |
| `STAGE.preconditions.condition@narrative`, `STAGE.postconditions.condition@narrative` | an action page's stated pre- and postconditions, on its Start | rendered — `pre[…]` / `post[…]` on START |
| `STAGE.resource@object`, `STAGE.resource@action` | the business object and action an Action stage calls; an internal object is named by its family (`Work Queues`, `Credentials`) | rendered — `ACTION <object>.<action>`, cards calls, data calls |
| `STAGE.processid` | the page a Page stage runs, or the process a Process stage runs | rendered — `PAGE «name»`, `PROCESS `name` (id)` |
| `STAGE.calculation@expression`, `STAGE.calculation@stage`, `STAGE.steps.calculation@expression`, `STAGE.steps.calculation@stage` | a calculation's expression and target item | rendered — `CALC [item] = expression`, `MULTICALC` |
| `STAGE.decision@expression` | a decision's expression | rendered — `DECISION`, cards decisions |
| `STAGE.choices` | a Choice or Wait stage's branch list (empty = no branch) | structure |
| `STAGE.steps` | a Multiple Calculation's list of calculations (empty = it calculates nothing) | structure |
| `STAGE.choices.choice.name`, `STAGE.choices.choice@expression` | a Choice branch's name and expression | rendered — `case «name» when …`, cards choices |
| `STAGE.choices.choice.element@id`, `STAGE.choices.choice.condition.id`, `STAGE.choices.choice.condition.arguments.argument.id`, `STAGE.choices.choice.condition.arguments.argument.value`, `STAGE.choices.choice@reply`, `STAGE.choices.choice.comparetype` | a Wait branch: the element watched, the condition, its arguments, the expected reply and how it is compared | rendered — `when «name»: «element».condition(…) <comparison> <reply>`, targets actions |
| `STAGE.choices.choice.element.elementparameter.name`, `STAGE.choices.choice.element.elementparameter.expression`, `STAGE.choices.choice.element.elementparameter.comparisontype`, `STAGE.step.element.elementparameter.name`, `STAGE.step.element.elementparameter.expression`, `STAGE.step.element.elementparameter.comparisontype` | a dynamic element attribute set at run time, and how it is compared | rendered — `«element» {name~comparison=expression}`, targets `elementParameters` |
| `STAGE.choices.choice.element.elementparameter.datatype`, `STAGE.step.element.elementparameter.datatype` | the element parameter's type | ignored — the expression is rendered |
| `STAGE.choices.choice.distance` | a branch's position on the diagram | ignored — presentation |
| `STAGE.timeout` | a Wait stage's timeout in seconds (a number or an expression) | rendered — `WAIT up to … s`, targets `timeout` |
| `STAGE.looptype`, `STAGE.loopdata`, `STAGE.loopdata.collection@name` | a loop's kind and the collection it walks, named by the text or by a `collection` child *[Inferred: the child form is in no release surveyed]* | rendered — `LOOP ForEach [collection]`, cards loops |
| `STAGE@interval` | a Navigate stage's pause after each of its steps, in seconds or a timespan | rendered — `interval=`, targets `interval` |
| `STAGE.step.element@id` | the element a UI step acts on | rendered — `«element»/type` on the stage's line, the targets entry it is listed under |
| `STAGE.step.action.id`, `STAGE.step.action.arguments.argument.id`, `STAGE.step.action.arguments.argument.value`, `STAGE.step.action.outputs.output.id`, `STAGE.step.action.outputs.output.value` | a UI step's action, arguments and outputs | rendered — `«element».action(args) → out→[item]`, a Write's `via action(args)`, targets actions |
| `STAGE.step@expr`, `STAGE.step@stage` | the value a Write step writes; the item a Read step reads into | rendered — `«element» := value`, `→ [item]`, targets `value` / `into` |
| `STAGE.exception@type`, `STAGE.exception@detail`, `STAGE.exception@usecurrent`, `STAGE.exception@savescreencapture` | an Exception stage: type, detail, rethrow of the current exception, a screen capture taken | rendered — `THROW type=… detail=…`, `RETHROW`, `[screen capture]`, cards throws |
| `STAGE.exception@localized` | the detail is a localized message | ignored — presentation of the message |
| `STAGE.attempts` | a Recover stage's limit on how often it recovers (at most 100), the session terminating at the limit (vendor documentation) | rendered — `RECOVER … attempts=`, cards error handling |
| `STAGE.bubbleException` | with an attempt limit: `True` raises an exception on the parent page at the limit instead of terminating the session (vendor documentation) | ignored while `False` |
| `STAGE.code` | a Code stage's body | rendered — `dump --full` |
| `STAGE.language`, `STAGE.references.reference`, `STAGE.imports.import`, `STAGE.globalcode` | an object's code options on its ProcessInfo stage: language, references, imports, global code | rendered — `CODE:` in the dump header, `--full` prints the global and object code in a dump of the whole object or of its main page |
| `STAGE.pythondllpath`, `STAGE.pythonenvpath` | on the ProcessInfo stage of a definition saved by version 7.4 or later, the Python runtime a Python object's code uses *[Inferred from the names: version 7.4 adds Python as a code language (vendor documentation)]* | ignored while `<present>` (empty) |
| `STAGE.datatype` | a data item's type (`text`, `number`, `flag`, `password`, `collection`, …) | rendered — `name:type` in the page's data items |
| `STAGE.initialvalue` | a data item's initial value (a password or a password-named text item: masked) | rendered — `name:type="value"` in the page's data items |
| `STAGE.initialvalue@xml:space` | the value's whitespace is preserved | ignored — serialisation |
| `STAGE.initialvalueenc` | a password item's encrypted initial value | rendered — `***`, never the value |
| `STAGE.exposure` | the item is an `Environment` or `Session` variable | rendered — `<Environment variable>`, cards and data |
| `STAGE.private` | the item is hidden from the process's other pages | rendered — its absence as `[shared]` |
| `STAGE.alwaysinit`, `STAGE.noalwaysinit` | the item is reset to its initial value whenever its page runs; `noalwaysinit` states the opposite | rendered — the absence of `alwaysinit` as `[no reset]` |
| `STAGE.collectioninfo.field@name`, `STAGE.collectioninfo.field@type` | a collection's fields | rendered — `collection[field:type, …]` |
| `STAGE.collectioninfo.field.field@name`, `STAGE.collectioninfo.field.field@type` | the fields of a field of type collection | rendered — `field:collection[field:type, …]` inside its collection's entry |
| `STAGE.collectioninfo.singlerow`, `STAGE.singlerow` | the collection holds one row (under the stage itself *[Inferred: in no release surveyed]*) | rendered — `[single row]` |
| `STAGE.collectioninfo.field@description`, `STAGE.collectioninfo.field@displayname` | a field's documentation and display label | ignored — the field's name and type are the schema |
| `ROW` | an empty initial row | rendered — `{}` in `initial rows of [collection] (n)`, which lists every row |
| `FIELD@name`, `FIELD@value` | a collection's initial cell (a secret-named cell masked) | rendered — `initial rows of [collection]` |
| `FIELD@encvalue` | a password cell's encrypted value, in place of `value` | rendered — `***` in `initial rows of [collection]`, never the value |
| `FIELD@type` | the cell's type | ignored — the collection's field states it |
| `APPDEF.apptypeinfo.id`, `APPDEF.apptypeinfo.parameters.parameter.name`, `APPDEF.apptypeinfo.parameters.parameter.value` | how the object reaches its application (`BrowserLaunch`, `Win32Attach`, …) and the launch or attach parameters | rendered — `APPLICATION MODEL:` in the dump header, profile, targets `application` |
| `APPDEF.apptypeinfo.parameters` | an application type with no parameter | structure |
| `ELEMENT@name`, `ELEMENT.id`, `ELEMENT.type`, `ELEMENT.basetype`, `ELEMENT.datatype`, `ELEMENT.description`, `ELEMENT.narrative` | a spied element, a region or a region container: name, id, element type (`WindowRect` or `Win32ListRegion` for a region, `Window` for a region container), base type, value type, description, the markup the spy recorded; a region container without a type is a name and an id only | rendered — targets; an untyped region container as its regions' `group` |
| `ELEMENT.container` | a region's container, by id | structure — the region sits in it, the container in the region's path (or its `group`, when untyped) |
| `ELEMENT.attributes` | an element with no attributes | structure |
| `ELEMENT.attributes.attribute@name`, `ELEMENT.attributes.attribute.ProcessValue@value`, `ELEMENT.attributes.attribute@comparisontype` | a recorded attribute, its value and how it is compared (`Wildcard`, `dynamic`, `GreaterThan`) | rendered — targets `matched` (used for matching) or `info` (recorded, not used) |
| `ELEMENT.attributes.attribute@inuse` | the attribute is used for matching | rendered — `matched` against `info` |
| `ELEMENT.attributes.attribute@system` | an attribute the product fills itself, such as a region's image *[Inferred from the name]*; its value and its matching are rendered | ignored while `True` |
| `ELEMENT.attributes.attribute.ProcessValue@datatype` | the attribute value's type | ignored — the value is rendered |
| `ELEMENT.attributes.attribute@isUserVisible` | not documented; `True` on every attribute surveyed | ignored while `True` |
| `ELEMENT.diagnose` | the element is marked for diagnostics | ignored while `False` |
| `GROUP@name` | an element group, in an element, in another group or in the model itself | rendered — the element's `group` |
| `GROUP.id` | the group's id | ignored — elements are addressed by their own |
| `ITEM:work-queue@id`, `ITEM:work-queue@name`, `ITEM:work-queue@key-field`, `ITEM:work-queue@max-attempts` | a work queue: numeric id, name, key field, attempts before an item is an exception | rendered — profile § WORK QUEUES, data `work-queue` |
| `ITEM:environment-variable@name`, `ITEM:environment-variable@type`, `ITEM:environment-variable@value`, `ITEM:environment-variable.description` | an environment variable (a secret-named or password one masked) | rendered — profile § ENVIRONMENT VARIABLES, data `environment-variable` |
| `ITEM:environment-variable@id` | the variable's id (its name) | ignored — the name identifies it |
| `ITEM:*-group@name`, `ITEM:*-group@isDefaultGroup`, `ITEM:*-group.members.*@id` | a process, object or work-queue group: its full path, the default flag and its members by id | rendered — profile, members by name; each member's `groups` in data and on its dump and cards headers |
| `ITEM:*-group@id` | the group's id | ignored — groups are named |
| `ITEM:calendar@id`, `ITEM:calendar@name`, `ITEM:calendar.schedule-calendar@id`, `ITEM:calendar.schedule-calendar@name` | a calendar's numeric id and name, repeated on its schedule calendar; a Calendars action names it by name | rendered — profile § CALENDARS, data `calendar` |
| `ITEM:calendar.schedule-calendar@working-week`, `ITEM:calendar.schedule-calendar@public-holiday-group` | the calendar's working days as one number, and the country whose public holiday list applies | rendered — profile § CALENDARS, data `calendar` |
| `ITEM:calendar.schedule-calendar.public-holiday-overrides.public-holiday@id`, `ITEM:calendar.schedule-calendar.other-holidays.other-date@value` | a public holiday of the group that the calendar overrides, by id; another non-working date | rendered — `overrides=`, `other-holidays=` |
| `ITEM:calendar.schedule-calendar.public-holiday-overrides`, `ITEM:calendar.schedule-calendar.other-holidays` | an empty list | structure |
| `ITEM:credential@name`, `ITEM:credential.credentialType`, `ITEM:credential.members.process@id`, `ITEM:credential.members.object@id` | a credential definition: name, type, and the processes and objects it lists by GUID; a release never carries the secret | rendered — profile § CREDENTIALS, data `credential`, members by name |
| `ITEM:credential@id` | the credential's GUID | ignored — the name identifies it |
| `ITEM:credential.description` | the credential's description | rendered — data `credential` |
| `ITEM:credential.members` | a credential that lists no process or object | structure |
| `ITEM:webapiservice*` | a Web API service: base URL, timeouts, authentication, common headers, parameters and code, every action's request (method, path, headers, body template or code) and output parameters (a secret-named parameter or header literal masked) | rendered — profile § WEB API SERVICES, data `web-api` (every value) |
