# Changelog

All notable changes to Raggd are documented here. Entries summarize user-facing
behavior, telemetry, and documentation updates as features become ready to ship.

## [Unreleased]

### Added
- Finalized the `raggd parser` command group with release-ready `parse`,
  `info`, `batches`, and `remove` flows driven by the handler registry and
  manifest lifecycle.
- Instrumented parser runs with structured telemetry covering handler runtime,
  fallback counts, queue depth, and database lock contention surfaced through
  the health hook.

### Documentation
- Published Phase 8 parser release notes and refreshed user guidance for parser
  configuration, telemetry alerts, and operational runbooks.
