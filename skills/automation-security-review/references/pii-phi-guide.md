# PII / PHI Guide

## Where sensitive data hides in UiPath projects

| Location | Typical content |
|---|---|
| `Data/`, `Input/`, `TestData/`, fixtures | Copies of production exports used while building |
| Config workbook (`Config.xlsx`) | Real emails, account numbers, sometimes passwords |
| Eval sets (`evals/`, `eval-sets/`) | Real tickets, emails, documents pasted as datapoints |
| Agent prompts and few-shot examples | Real customer conversations |
| IXP / Document Understanding samples | Scanned invoices, IDs, medical forms (PDF/images — not scanned) |
| Queue item exports, job logs, `.har` files | Transaction payloads, tokens |
| Screenshots in repo | Application screens with customer records |
| READMEs, PDDs, notebooks | Example records copied from production |

## Categories

| Category | Examples | Default severity |
|---|---|---|
| Government identifiers | SSN, national id, passport, tax id | Critical |
| Financial | Payment card (PCI DSS), IBAN, bank account | Critical (cards) / Warning |
| Health (PHI) | MRN, diagnosis, medication, insurance member id, any health detail tied to a person | Critical |
| Contact | Name + email/phone/address together | Warning |
| Single contact detail | Email alone, phone alone | Info–Warning by volume |
| Credentials of real people | Passwords, security answers | Critical |

PHI = health data linked to an identifiable person. Under HIPAA, the identifier list includes names, geographic subdivisions smaller than a state, dates (except year) related to the person, contact details, record and account numbers, device ids, biometrics, and full-face photos. One diagnosis code next to a name is PHI.

## Synthetic vs real

Downgrade to Info only when all hold:

1. Path or marker says synthetic (`synthetic`, `fixtures`, `mock`, `testdata`, or a `.synthetic-data` file in the directory tree).
2. Values have test shape: test-card numbers, sample SSNs, `example.com` emails, obviously generated names.
3. Nothing indicates a production export (column headers from a real system, export timestamps, real company domains).

Recommend teams drop a `.synthetic-data` file (empty, committed) into folders holding generated test data so future scans downgrade them automatically.

## Remediation order

1. Stop distribution: do not push, pack, or share until removed.
2. Delete from working tree; replace with synthetic data.
3. Purge from git history if the repo was ever pushed (`git filter-repo` or equivalent) and ask collaborators to re-clone.
4. Notify the data owner / privacy contact when real PHI or card data was exposed in a shared location — organization policy decides reporting.
