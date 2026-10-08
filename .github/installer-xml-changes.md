# Installer XML change reminder

When an individual installer changes, the shared installer may need an update.

After `reusable_installer_xml_check.yml` is merged, add
`.github/workflows/installer_xml_check.yml` to each repository that needs the
check:

```yaml
name: Installer XML changes

on:
  pull_request:

permissions:
  contents: read

jobs:
  check:
    uses: aws-deadline/.github/.github/workflows/reusable_installer_xml_check.yml@mainline
```

The check fails when XML files under `installer/` or `install_builder/` change.
Review the files listed in the log, check whether the shared installer needs an
update, and confirm any follow-up in the PR discussion.

Keep private details and links out of public PR discussions.
