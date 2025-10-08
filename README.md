# AWS Deadline Cloud Integrations

This repository contains:

- **User guides** - MkDocs-based user guides for AWS Deadline Cloud DCC integrations
- **Reusable workflows** - GitHub Actions workflows for integration repositories
- **Organization profile** - GitHub profile README in `profile/README.md`

User guides for AWS Deadline Cloud DCC integrations.

## User Guides

Build and serve the user guides locally:

```bash
uv run mkdocs serve --watch gen_files/ --watch overrides/ --watch docs/
```

Visit http://127.0.0.1:8000 to view the site.

Validate all links and images in the generated user guides:

```bash
uv run python scripts/validate_docs.py
```

### Adding a New Integration

To add a new integration, update `gen_files/config.py`:

```python
INTEGRATIONS = {
    'new-integration': {
        'repo': 'deadline-cloud-for-new-integration',
        'display_name': 'New Integration Name',
        'has_user_guide': True  # or False if no user guide exists
    },
    # ... existing integrations
}
```

For user guides (`has_user_guide: True`), the repository needs:
- `docs/user_guide/index.md` (main user guide page)
- Optional additional `.md` files (become sub-pages)
- Images in `docs/user_guide/images/`

The integration appears automatically in navigation, alphabetized by display name.
