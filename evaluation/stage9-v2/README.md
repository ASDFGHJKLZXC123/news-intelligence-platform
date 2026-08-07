# Stage 9 validation v2

`stage9-validation.v2` is an isolated readiness boundary. It supersedes no v1 bytes and currently
authorizes no evaluation run.

The canonical `readiness.json` records the exact v1 hashes inherited by v2, the unchanged
entity-linking production policy, the spent v1 entity final holdout, and the evidence still needed
for clustering and analogy. It is a readiness artifact, not a calibrated report.

The registered embedding snapshot requirement is now satisfied by the reviewed, active
`nip-es2-20260731-5e17cfbc0b7562ca228f` snapshot. Development runs for clustering or analogy
remain blocked until canonical human-adjudicated pair evidence is present:

1. a canonical, human-adjudicated pair artifact under `gold/`; and
2. its matching, completed human-review manifest.

Clustering additionally requires at least 100 labeled pairs, both label classes in every split, and
disjoint leakage groups and article ids across train, development, and final holdout. Analogy stays
`evaluation_only` until a new split corpus contains explicit negative/no-reliable-analogy labels
with the same isolation rules.

Each labeled-pair artifact must have a canonical sibling review manifest named
`clustering-review-manifest.json` or `analogy-review-manifest.json`. The pair artifact records that
file's SHA-256. The manifest uses schema `stage9-v2.human-review-manifest.v1`, binds the exact
labeled records by count and canonical SHA-256, and records distinct reviewer A, reviewer B, and
adjudicator identities with completion dates. Status strings or an unattached digest alone are not
accepted as evidence of human review.

Only fresh JSON paths under `evaluation/stage9-v2/development/` can be authorized by the scaffold.
There is intentionally no final-output writer here. Entity linking inherits the v1 policy but must
receive a new unseen v2 holdout before a future final-run implementation can be considered.
