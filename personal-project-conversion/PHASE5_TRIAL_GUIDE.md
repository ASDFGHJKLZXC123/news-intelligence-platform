# Phase 5 raw-reading trial

Phase 5 began on October 4, 2026 (America/Los_Angeles). Preparation is in progress; actual personal use has not begun. The user chose **raw reading first, with model spending off**, and requested a new ordinary-reading baseline log. The [acceptance contract](05-validation-and-release.md) is unchanged.

## First practical step: ordinary reading

Record three real sessions using your usual news-reading approach in [the baseline log](trials/phase-5-20261004/baseline.csv). For each, enter the date, approximate minutes, useful-story count, sources opened, duplicate coverage noticed, broad interest area, and optional notes. Its three blank rows are placeholders, not completed sessions. Same-day baseline sessions need their start time so their completion before the application trial can be checked. Do not estimate earlier sessions from memory and label them timed observations.

Then choose and prepare the intended personal PostgreSQL database using the [existing startup and recovery guide](PHASE4B_SETUP.md). The trial package does not select, migrate, change, or start a database. Reuse a proven backup when the intended workspace contains existing data. The trial's [raw configuration draft](trials/phase-5-20261004/raw-reading-draft.json) uses the previously selected four feeds, empty phrase filters, the existing 100-article bounds, and AI disabled. It has no runtime loader and no source IDs yet; map feed IDs through supported Settings before the trial. Keep paid activation disabled. No prices, provider credentials, model compatibility, or current feed availability were checked during this preparation.

Record the actual profile revision, migration head, enabled features, and runtime source identity before the first reading session. The [prepared manifest](trials/phase-5-20261004/trial.json) records the dirty checkout by Git HEAD plus source hashes, rather than claiming HEAD alone is a reproducible release. Its runtime/profile binding is pending. A read-only database export identifies the exporter's source; it cannot prove which application binary produced a historical run. Retain the documented launch/source record and use it in each reading observation's `application_revision` and `runtime_revision_basis`.

Copy `runtime-binding-template.json` to a separate record, fill its actual `recorded_at`, `profile_revision_id`, `migration_head`, and `runtime_revision_basis`, and verify the supplied candidate source manifest matches the app you launch. Bind it before the first actual reading session:

```sh
.venv/bin/python scripts/personal-trial.py bind-runtime personal-project-conversion/trials/phase-5-20261004 --record RUNTIME_BINDING_JSON
```

This retains your pretrial observations in an exclusive file; it does not configure or start the application. Session registration refuses a missing/late binding, a changed profile or persisted calendar, or AI-enabled operation in this raw trial.

## Each actual application session

Start the application through the documented foreground launcher, open Today, and explicitly request that date's update. Read the available raw articles and inspect their original sources. Log time, useful stories, source visits, any saves/reopening, and any failure or recovery. An honest quiet result can count; readable old news after an unexplained update failure cannot silently become a completed session. Record raw backend outcomes even when supported recovery later succeeds.

Before closing the session, capture its current run evidence. Use the repository environment and explicitly exported local database URL:

```sh
.venv/bin/python scripts/personal-trial.py capture personal-project-conversion/trials/phase-5-20261004 --run-id RUN_UUID
```

The command reads in a consistent PostgreSQL transaction with database-enforced read-only mode. It never starts an update, contacts feeds/models, grants an allowance, or writes to the database. It creates an exclusive private JSON file in `captures/`. Capture each failed attempt before retrying as well: the current schema does not retain every overwritten attempt outcome. Keep those files and record their references under `attempt_observations`; a final-run capture cannot reconstruct missing attempt history.

Copy `observation-template.json` to a separate observation file and fill it with what you actually did. Set `actual_reading_session` to true only after real reading. Use timezone-aware start/close timestamps, explicit completion/intervention booleans, measured or approximate minutes, retry count, and empty lists where no action occurred. Exercise fields accept `exercised_successfully`, `exercised_failed`, `not_exercised`, or `unavailable`; raw-mode briefs/exports are unavailable. Record the actual runtime revision and how it was established. Then attach the observations:

```sh
.venv/bin/python scripts/personal-trial.py record-session personal-project-conversion/trials/phase-5-20261004 --capture CAPTURE_JSON --observations OBSERVATIONS_JSON
```

The command requires a completed three-session baseline and prevents duplicate processing dates. A retry across midnight stays with its original run/date/session. Keep skipped calendar dates in `missed-dates.json`; never manufacture a session for a missed date. Supported retries and ordinary launcher use are routine operation. Code/database repairs or custom internal process recovery count as developer intervention. Preserve failed sessions and record corrections separately.

## Sampling and assessment

```sh
.venv/bin/python scripts/personal-trial.py sample personal-project-conversion/trials/phase-5-20261004
.venv/bin/python scripts/personal-trial.py reliability personal-project-conversion/trials/phase-5-20261004
```

For each later sample run, supply `--previous LATEST_SAMPLE_JSON` using the latest retained manifest printed by the command. Sample files are never overwritten. Selection is provisional before seven real distinct-date sessions. The initial seven sessions and extensions are reported separately, with previous frozen selections retained. The selection tool uses only stable identities and the specified SHA-256/day-balanced method; it never picks an easier story based on content or scores.

Raw articles cannot count as grouped-event or event-summary samples. Raw-only reading therefore leaves P5-02, P5-03, and P5-04 unproven even after seven sessions. Keep quality review files empty until actual eligible material exists. Personal relevance and continued-use judgments must be supplied by the user; engineer reviews must enumerate the complete group/source or factual-claim evidence. Automated records do not supply those judgments.

For a later assisted trial, retain the original raw trial and create a new identified trial when the profile changes. A recurring allowance, selected routes with current price evidence, and separately approved activation are required. The old $0.25 one-shot proof does not authorize it. Report publication time is currently missing as an immutable field; do not substitute `created_at` or mutable `updated_at` for exact earliest-publication ordering. That limitation must be resolved before the summary sample can satisfy P5-04.

The [release assessment](trials/phase-5-20261004/release-assessment.json) starts as **insufficient evidence** with all seven criteria pending. A release still needs actual cost/data-preservation observations, a permitted captured-data demonstration, reproducibility checks, remaining-issue priorities, and your concrete continued-use judgment. No scheduling or publication is part of this phase preparation.
