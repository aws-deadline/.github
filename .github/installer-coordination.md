# Shared installer coordination check

Installer definition changes can require a corresponding update to the shared
Deadline Cloud submitter installer. This static check posts one public PR comment
asking for confirmation and publishes an **Installer coordination** commit status.
It requires no AI interpretation or non-public implementation details.

The check watches all XML files under `installer/` and `install_builder/`,
including nested files, additions, deletions, and both sides of renames. Any
change to these definitions requires confirmation, including cosmetic edits.
Other files pass automatically without creating a comment.

The PR author or a maintainer replies in a new PR conversation comment with
exactly one command:

| Command | Confirmation |
| --- | --- |
| `/installer-followup done` | The corresponding update is complete. |
| `/installer-followup in-review` | A corresponding change is under review. |
| `/installer-followup not-needed` | No corresponding update is needed. |

A change under review is sufficient; it does not need to have merged. A promise
to coordinate later is not one of the accepted commands. Keep non-public
implementation details, review identifiers, and tracking links out of the PR.
The checker never repeats reply text; the question and its status use fixed
public wording.

Commands must be the entire comment, apart from surrounding whitespace.
Quoted commands, code examples, and arbitrary prose do not count. The checker
accepts the PR author's commands and checks repository permissions before
accepting another user's command: write, maintain, or admin access is required.
Bot replies do not count.

The latest valid, authorised command determines the confirmation. Confirmation
applies to the PR and is rechecked on pushes. Creating, editing, or deleting a
conversation comment reevaluates the current comments. Deleting or invalidating
the only valid confirmation makes the status fail again. The original bot comment
is updated in place; a copied marker in a human comment cannot impersonate it.
If installer changes are removed, the status passes and any existing bot question
is updated to say no confirmation is required.

## Repository setup

Add `.github/workflows/installer_coordination.yml` using this caller:

```yaml
name: Installer Coordination

on:
  # Runs trusted base-branch workflow code. The reusable workflow checks out
  # central tooling only and never executes PR code.
  pull_request_target: # zizmor: ignore[dangerous-triggers] -- trusted central tooling only; no PR checkout or execution
    branches: [mainline]
    types: [opened, synchronize, reopened, edited, ready_for_review]
  issue_comment:
    types: [created, edited, deleted]

permissions: {}

concurrency:
  group: installer-coordination-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true

jobs:
  coordination:
    if: >-
      github.event_name == 'pull_request_target' ||
      (github.event.issue.pull_request && github.event.comment.user.type != 'Bot')
    permissions:
      contents: read
      pull-requests: read
      issues: write
      statuses: write
    uses: aws-deadline/.github/.github/workflows/reusable_installer_coordination.yml@mainline
    with:
      pr_number: ${{ format('{0}', github.event.pull_request.number || github.event.issue.number) }}
```

Do not add event path filters: every PR must receive a passing or failing status,
including unrelated PRs. The PR template does not need a checkbox.

Merge the central reusable workflow before the callers. After a caller runs,
make the **Installer coordination** commit status required in the `mainline`
branch protection rule or ruleset. This is the dedicated status on the PR head,
not the Actions job result: comment events run against the default branch, so
the script explicitly updates the PR's current head SHA.

Both event types use trusted base-branch workflow code, including fork PRs.
Only central tooling is checked out. PR files and comments are read through the
GitHub API and are never executed. The token has read access to contents/PRs and
write access to issue comments and commit statuses. No secrets or OIDC credentials
are required.

Incomplete API data, permission lookup failures, or a PR revision changing while
the check runs cause an error rather than a successful result. Once a revision
is identified, the script attempts to publish an error status on that revision;
the workflow can be retried after the underlying failure is resolved.

For pre-merge testing, the caller's reusable workflow reference and the
`tooling_repository` / `tooling_ref` inputs can point to the same fork revision.
