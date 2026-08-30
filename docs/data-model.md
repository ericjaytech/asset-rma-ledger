# Data model and file contracts

Asset RMA Ledger stores one schema-versioned SQLite database. Business identifiers are matched case-insensitively while retaining their original display form. Internal integer keys are not exposed through the command-line interface.

## Tables

| Table | Purpose | Important constraints |
| --- | --- | --- |
| `schema_metadata` | Identifies the application and schema version. | Exactly one row; version `1` for `0.1.0a0`. |
| `vendors` | Vendor contacts and default elapsed-time SLA terms. | Unique folded vendor key; inactive records remain queryable. |
| `assets` | Asset identity, model, lifecycle and warranty data. | Unique folded asset tag and serial number. |
| `rma_cases` | Immutable case header and current-state projection. | Unique folded case reference; one non-terminal case per asset. |
| `case_events` | Authoritative append-only case history. | Unique event UUID and case-local sequence; update and delete triggers reject changes. |

Vendor SLA defaults are copied to a case when it opens. Later vendor changes do not alter existing deadlines. Case events and their projection changes commit in one transaction.

## Case lifecycle

```text
open -----> authorised -----> outbound -----> with_vendor
 |              |                                |       \
 +-> cancelled  +-> cancelled                    |        v
                                                  |     returning --> returned
                                                  |                    |
                                                  +--------------------+
                                                                       v
                                                                     closed
```

The standard path requires authorisation, outbound dispatch, vendor receipt, return dispatch and return receipt. Exceptional closure from `open`, `authorised` or `with_vendor` requires an explanatory outcome and an explicit final asset status. Cancellation is available only before outbound dispatch.

Supported outcomes are `repaired`, `replaced`, `refund`, `no_fault_found`, `repair_declined`, `written_off` and `other`. The `other` outcome requires a note.

## Event history

Each case event records its type, occurrence and recording timestamps, operator alias, fixed-schema JSON payload, previous hash and SHA-256 event hash. The first event uses an all-zero previous hash.

The hash input includes schema version, case reference, sequence, UUID, event type, timestamps, operator alias, canonical payload and previous hash. `verify` recomputes the chain, validates its fixed payload schemas, replays the lifecycle and compares the result with `rma_cases`.

Corrections append a compensating event. They do not edit the original event. The chain is useful for detecting accidental modification, but it is not a digital signature or an external proof of authenticity.

## Canonical CSV imports

Files must use UTF-8 or UTF-8 with a byte-order mark. Headers must contain exactly the columns below. Imports are limited to 100 MiB and 250,000 rows, create records only, and commit the whole file or nothing.

### Vendors

```text
vendor_key,name,support_url,support_email,support_phone,account_reference,response_sla_hours,resolution_sla_hours,active
```

### Assets

```text
asset_tag,serial_number,asset_type,manufacturer,model,lifecycle_status,warranty_vendor,warranty_reference,warranty_start,warranty_end
```

The imported lifecycle status cannot be `in_rma`. Only a case dispatch event may set that state.

### Cases

```text
case_reference,asset_tag,vendor_key,opened_at,status,vendor_reference,response_due_at,resolution_due_at,vendor_responded_at,outbound_dispatched_at,vendor_received_at,return_dispatched_at,returned_at,outcome,closed_at
```

A case row is a reconstructed snapshot. The importer validates chronology and creates the minimum standard event sequence needed to reach the supplied state. It cannot import notes, custom events or corrections.

## Export bundle

`export` creates a new directory containing:

| File | Contents |
| --- | --- |
| `vendors.csv` | Vendor register and SLA defaults. |
| `assets.csv` | Asset register and warranty data. |
| `cases.csv` | Current case projections and effective deadlines. |
| `case_history.csv` | Every event, payload, previous hash and event hash. |
| `due_cases.csv` | Overdue and next-48-hour incomplete deadlines at the supplied `as_of`. |
| `export_manifest.json` | Tool/schema versions, generation time, database basename, row counts and SHA-256 checksums. |

Rows and columns have deterministic order. Formula-like text is prefixed with an apostrophe before CSV output. The destination must not exist, and publication occurs only after every staged file and checksum succeeds.

Exports contain operational identifiers. Their checksums detect file changes but do not encrypt or authenticate the data.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Command completed successfully. |
| `2` | Invalid arguments, field values, CSV content or export request. |
| `3` | Domain rule or lifecycle transition failed; no mutation committed. |
| `4` | Database open or initialisation failed. |
| `5` | Verification found an integrity, chain or projection error. |
