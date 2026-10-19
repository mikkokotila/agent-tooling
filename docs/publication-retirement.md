# Publication reconciliation

Updating a clean checkout from the target branch is not new PR work. A recorded
publication request may also outlive a branch switch after its work has shipped.

Full PR snapshots reconcile exact recorded branch refs, rather than requiring the
checkout to remain on that branch. An independently read live default-branch tip
also proves publication when it contains that exact local branch tip. This retires
only the matching clean source record; it does not mark CI, reviews or other work
complete, adopt a referenced PR, or create another commit/PR.

Stop and `status --fresh` reconcile remaining records, including when no active PR
remains. To inspect and reconcile one exact checkout explicitly:

```sh
caller-pr-gates publication-reconcile --session SESSION --cwd /absolute/repository
```

The command reads repository identity and the live default tip twice, then first
checks ancestry against the recorded local branch. If that tip is outside the
default branch's history, it reads at most 100 closed pull requests for the exact
head repository and branch, targeting the default branch. The first matching
candidate receives two individual reads; an unstable candidate stops reconciliation
without trying another candidate. Listings are shared within the repository and
branch for that reconciliation run.

A squash-merged pull request can prove earlier delivery even when another chat
owned it. Both reads must agree on the exact head repository, branch and commit,
target repository and branch, closed and merged state, merge commit and merge
time. GitHub reports merge times to whole seconds. That entire reported second
must end before or at the publication marker (`merged_at + 1 second <= marker`),
so a marker in the same second remains ambiguous and is retained. The merge commit
must be an ancestor of the live default tip. Repository identity, default-branch
selection and its commit are rechecked after these reads. A reused branch name or
a closed but unmerged pull request is not proof.

Before retiring a record, the command compares the clean source, recorded branch
tip, task ancestry and exact marker version again. Only that task's matching
publication marker is removed. Other chats' ownership, watches, snapshots and CI
or review gates remain unchanged; the audit records the publication proof with
`completion: false`. Dirty source, unpublished commits, unrelated origins or
checkouts, changing remote identity, missing evidence and concurrent updates keep
their markers. Errors remain local. No direct database repair is needed.

Deployment retains worker protection. `make install` waits up to 600 seconds for
active workers by default, without retrying deployment or changing a lease. The
explicit option changes that bound; use zero for immediate admission failure:

```sh
python3.13 tools/manage.py install --wait-for-workers 600
```

Timeout leaves the running installation intact. Deployment rechecks worker
admission after the wait and at its existing transaction boundaries.
