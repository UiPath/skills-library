# Scanner Rules

Rules emitted by `scripts/scan.mjs`. Severity is the scanner default; Step 5 verification may downgrade or reject. Values in synthetic/test paths are downgraded to `info` and carry `syntheticContext: true`; published processor test cards and famous sample SSNs carry `knownTestValue: true`.

## Secrets

| Rule | Severity | Detects | Fix |
|---|---|---|---|
| `SEC_PRIVATE_KEY` | critical | PEM private-key block | Rotate key pair; store in certificate store / Orchestrator Secret asset |
| `SEC_KEY_FILE` | critical | `.pem`, `.pfx`, `.p12`, `.key`, `.jks`, `id_rsa`-style files | Remove from repo; rotate; load from secret store at runtime |
| `SEC_AWS_ACCESS_KEY` | critical | AWS access key id | Rotate in IAM; use role-based auth or secret store |
| `SEC_GITHUB_TOKEN` | critical | GitHub PAT / app token | Revoke; use CI secret |
| `SEC_SLACK_TOKEN` | critical | Slack bot/user token | Revoke; use Integration Service Slack connection |
| `SEC_SLACK_WEBHOOK` | warning | Slack incoming-webhook URL | Regenerate webhook; store as Secret asset |
| `SEC_GOOGLE_API_KEY` | critical | Google API key | Rotate; restrict key; store as Secret asset |
| `SEC_OPENAI_KEY` / `SEC_ANTHROPIC_KEY` | critical | LLM provider key | Rotate; call models through UiPath LLM Gateway or store key as Secret asset |
| `SEC_STRIPE_KEY` | critical | Stripe live key | Roll key in Stripe dashboard |
| `SEC_AZURE_STORAGE_KEY` | critical | `AccountKey=` in connection string | Rotate storage key; use managed identity or Secret asset |
| `SEC_AZURE_SAS` | warning | SAS signature in URL | Revoke SAS policy; issue short-lived SAS at runtime |
| `SEC_JWT` | warning | Encoded JWT literal | Confirm expiry; if long-lived, revoke issuing credential |
| `SEC_URL_CREDENTIALS` | critical | `scheme://user:pass@host` | Rotate; move credential to Credential asset |
| `SEC_CONNECTION_STRING_PASSWORD` | critical | `Password=`/`Pwd=` in connection string | Rotate; build connection string at runtime from Credential asset |
| `SEC_BEARER_LITERAL` | critical | Hardcoded `Bearer <token>` | Revoke; obtain token at runtime or use Integration Service connection |
| `SEC_HARDCODED_SECRET` | critical (strong value) / warning (low-entropy word) | Literal assigned to a secret-named key (`password`, `client_secret`, `apiKey`, `token`, …) in code, JSON, or unquoted in `.env`/YAML/INI/properties. Keys with a metadata word (`tokenId`, `passwordPolicy`, `secretName`) are ignored | Rotate; read from Credential/Secret asset or environment at runtime |
| `SEC_LABELLED_SECRET` | critical | Random-looking value after a secret label in prose — prompts, comments, docs (`API key for X: …`) | Rotate; inject at runtime through a tool or asset, never in prompt text |
| `SEC_ENV_FILE` | critical (git-tracked or in build output) / warning (not ignored) / info (ignored) | `.env` with populated values | Add to `.gitignore`; commit `.env.example` with placeholders; rotate if ever committed or shipped |
| `SEC_SOURCE_MAP_IN_BUILD` | warning | `.map` in `dist/`/`build/`/`out/` | Disable production source maps |
| `SEC_PACKAGE_REGISTRY_CREDENTIAL` | critical | Token/password in `.npmrc`, `.pypirc`, `.netrc`, `nuget.config` | Use user-level config or CI secret; rotate |

## UiPath project files

| Rule | Severity | Detects | Fix |
|---|---|---|---|
| `UIP_AUTH_IN_PROJECT_FILE` | critical | Client secret / access / refresh token in `uipath.json` or `.uipath/` | Rotate external-app secret; keep auth in `uip login` store or environment |
| `UIP_SECRET_IN_PROJECT_JSON` | critical | Secret-looking value in `project.json` | Move to Credential/Secret asset |
| `UIP_SECRET_IN_CONFIG_SHEET` | critical | Value next to a secret-named key in a Config workbook | Replace with asset name; store value in Orchestrator Credential asset |

## XAML

| Rule | Severity | Detects | Fix |
|---|---|---|---|
| `XAML_PLAINTEXT_PASSWORD` | critical | Literal password on a variable default, Assign, or password property | Rotate; Get Credential → `SecureString` → Type Secure Text / login activity |
| `XAML_PASSWORD_AS_STRING` | critical | Credential held in a `String` variable (ST-SEC-008) | Declare `SecureString`; keep it in the consuming scope |
| `XAML_PASSWORD_ARGUMENT_AS_STRING` | critical | Credential passed as `String` argument (ST-SEC-007) | Pass the asset name; fetch inside the consuming workflow |
| `XAML_SECURESTRING_TO_STRING` | warning | `NetworkCredential(...).Password` / BSTR conversion (ST-SEC-009) | Pass `SecureString` to the consuming activity; convert only inside it |
| `XAML_SENSITIVE_LOG` | critical | Log Message expression uses a secret- or PII-named identifier | Log an identifier or masked value only |
| `XAML_GET_ASSET_FOR_SECRET` | warning | Get Asset on a credential-named asset | Use Get Credential / Get Secret |
| `XAML_PII_IN_QUEUE_ITEM` | warning | Queue item field named like SSN, card, DOB, diagnosis, password | Store a reference id; keep sensitive data in a protected system or encrypted storage bucket |

## Code

| Rule | Severity | Detects | Fix |
|---|---|---|---|
| `CODE_SENSITIVE_LOG` | warning | log / print / console call passing a secret- or PII-named identifier | Remove or mask |
| `CODE_PII_IN_TRACES` | warning | `@traced()` with no redaction on a function taking PII-named parameters | `@traced(hide_input=True)` or an `input_processor` redactor |

## PII / PHI

| Rule | Severity | Detects | Fix |
|---|---|---|---|
| `PII_SSN` | critical | US SSN shape (context required outside data files) | Replace with synthetic data; purge real data from repo history |
| `PII_PAYMENT_CARD` | critical | Brand-prefixed, Luhn-valid card number | Remove; card data in repos breaks PCI DSS scope |
| `PII_IBAN` | warning | Checksum-valid IBAN | Replace with synthetic IBAN |
| `PII_EMAIL` | warning (≥5) / info | Personal-looking emails (test domains excluded) | Replace with `example.com` addresses |
| `PII_DOB` | warning | Labelled date of birth | Replace with synthetic data |
| `PII_PASSPORT_OR_NATIONAL_ID` | warning | Labelled passport / national id | Replace with synthetic data |
| `PHI_MRN` | critical | Labelled medical record number | Remove; PHI in repos is a reportable exposure in many jurisdictions |
| `PHI_DIAGNOSIS_CODE` | critical | ICD-style code next to a diagnosis label | Remove or use synthetic records |
| `PHI_NPI_OR_MEMBER_ID` | critical | Labelled insurance member / subscriber / policy id | Remove or use synthetic records |

## `--show` masking

Masks every detector shape above, values on secret-named keys, `.env` values, XAML string literals, and any random-looking token of 16+ characters (`‹HIGH-ENTROPY›`). Free-text names, addresses, and clinical notes are not masked.

## Hardening

- Fingerprints are HMACs with a per-run random salt (set `UIPATH_SECURITY_REVIEW_SALT` to compare runs); a plain hash of an SSN or card could be brute-forced.
- Names copied from files into output (keys, variables, assets) are reduced to `[A-Za-z0-9_.-]`, max 48 chars; control, bidi, and line-separator characters in paths are replaced. File content cannot forge findings or inject instructions through scanner output.
- `--show` refuses files outside `--root` after resolving symlinks (exit 3).
- Office parts are capped at 32 MB inflated (zip-bomb guard); corrupt archives are counted in `stats.officeFilesUnreadable`.
- Regexes are bounded; element scans use `indexOf` windows — adversarial long lines stay linear.
- `.synthetic-data` marker locations are listed in `stats.syntheticMarkers`; a marker never downgrades secrets.

## Limits

- Regex + checksum detection. Names, addresses, and free-text clinical notes are not detected — Full-mode judgment covers them only in files the agent may read.
- Files over 2 MB (20 MB for Office files) are skipped and counted in `stats.filesSkippedLarge`.
- PDFs, images, and binary formats are not scanned. List them under **Not Covered** when present in data folders.
- Git history is not scanned — only the working tree. A secret deleted in the working tree may still be in history.
