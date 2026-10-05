# Phase 4A local runtime

Phase 4A uses PostgreSQL, Redis, one loopback application process, the existing separate frontend server, and a temporary supervised Python child for each explicit update. Celery worker and Beat are optional legacy services. The application does not start them. Phase 4B's Redis removal and single-address frontend are not implemented.

## Select the runtime explicitly

Keep your existing database and Redis URLs. Apply the additive `0023_personal_processing_control` migration to a validated backup copy first. The [Phase 4A evidence](evidence/phase-4a.md) records disposable populated upgrade/restore checks. The upgraded schema retains articles, reports, Saved, snapshots, run identities, and spending; rollback selects a supported mode and retains that schema.

Set these environment values for the personal application and manual command:

```sh
export PERSONAL_PROCESSING_TRANSPORT=subprocess
export PERSONAL_PROCESSING_MODE=personal
export PERSONAL_BIND_HOST=127.0.0.1
export PERSONAL_APP_WORKERS=1
export PERSONAL_PAID_RUNTIME_ENABLED=false
export CORS_ALLOW_ORIGINS=http://127.0.0.1:3000
```

`DATABASE_URL` and `REDIS_URL` must point to your intended local services. The paid gate defaults off; choosing this runtime does not authorize a monthly allowance, apply saved preferences, enable models, or start a live update. Configure sources/profile through the existing Settings view. Keep `PERSONAL_OFFLINE_FIXTURE_PATH` empty for ordinary use; the recorded engineering proof used explicitly synthetic fixtures.

From the repository with its existing environment activated:

```sh
python -m alembic upgrade head
python -m services.personal.cli mode personal
python -m services.personal.app --port 8000
```

The mode command is explicit and requires the global writer to be idle. The application validates loopback binding, one process, configuration and database mode agreement. A mismatched mode blocks processing while stored reading remains available. Legacy writers also check configuration and durable mode before provider calls or mutations.

In a second terminal, use the existing frontend server:

```sh
make frontend
```

Open `http://127.0.0.1:3000/SIGNAL%20-%20Intelligence%20Platform.html?api=http://127.0.0.1:8000`. Health requires PostgreSQL, Redis, configuration, and a ready launcher; it does not require a Celery worker or scheduler in subprocess mode.

## Updates and recovery

The Today request/retry controls and these manual commands use the same supervisor:

```sh
python -m services.personal.cli start
python -m services.personal.cli retry RUN_UUID
python -m services.personal.cli status
```

A returned queued identity is not completion. Polling/status never launches a replacement. One global owner covers all dates and entry points. Repeating the active date identifies the existing run; another date receives `processing_busy`. A successful date stays complete. Failed logical runs retain the total attempt ceiling of three.

The handoff lease is two minutes. On running claim, the delivery token rotates and fixed graceful/hard/ownership deadlines are 25/30/35 minutes from the actual claim. There is no lease renewal. Every provider and database operation has a finite remaining-budget bound. Both launcher and child enforce the original hard deadline; the child arms its original handoff deadline before its first database connection. Runtime status exposes these actual instants and the server-derived retry reason.

Stop the app normally with Ctrl-C or SIGTERM. It refuses new launches, requests child cancellation, waits at most 30 seconds, then stops the exact child process group it owns. Ctrl-C in the manual command uses the same cleanup. If PostgreSQL or the launcher is lost, leases, frozen inputs, and reservations remain for explicit recovery. A PID alone never authorizes a kill or proves a child has exited.

```sh
python -m services.personal.cli reconcile-children
python -m services.personal.cli recover RUN_UUID
python -m services.personal.cli retry RUN_UUID
```

Reconciliation conservatively checks retained launcher/child birth and command identities. Recovery requires the original lease to have expired and does not launch or grant a new allowance. A new retry is separately explicit. Unconfirmed older children remain in durable history and block idle mode switching until reconciled. Already-dispatched spending can be reconciled by its accounting-only service path; an old token cannot store business results or fail a newer attempt.

## Backup and supported runtime rollback

The existing `make db-backup` and `make db-restore-drill` commands remain the general PostgreSQL tools; the restore drill checks the full current revision. Preserve the backup and checksum and validate restoration to a disposable database before relying on it. The Phase 4A proof additionally checks a populated personal database, including published versions, Saved and known/uncertain spending.

Once genuinely idle:

```sh
python -m services.personal.cli mode legacy
```

Stop the personal application, select `PERSONAL_PROCESSING_MODE=legacy` and `PERSONAL_PROCESSING_TRANSPORT=celery`, then use the existing legacy setup with application binaries that honor the upgraded writer guard. An active/unconfirmed child, an unrecovered owner or a processing transaction blocks the switch. Switching does not drop tables, rewrite historical reports, clear reservations, or grant another successful date. Do not run older unguarded binaries as writers against the upgraded database.
