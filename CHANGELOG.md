# Changelog

All notable changes to Asset RMA Ledger are recorded here.

## [0.1.0a0] - 2026-08-30

Initial alpha release.

### Added

- Local SQLite vendor, asset and RMA case registers.
- Controlled return lifecycle with elapsed response and resolution deadlines.
- Append-only, hash-chained case events and compensating outcome corrections.
- Transactional, dry-runnable CSV imports for vendors, assets and reconstructed case snapshots.
- Deterministic CSV export with a checksum manifest and spreadsheet-formula safeguards.
- Read-only database, schema, event-chain, lifecycle and projection verification.
- Installable Python command with no runtime dependencies.
