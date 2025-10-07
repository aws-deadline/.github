#!/usr/bin/env python3
"""Generate integration content from GitHub repositories."""

import sys
from pathlib import Path

# Add current directory to path for imports when run as script
sys.path.insert(0, str(Path(__file__).parent))

import mkdocs_gen_files
import subprocess
from config import INTEGRATIONS
from generate_releases import generate_release_page
import yaml

def clone_repo(repo_name, cache_dir):
    """Clone a repository to cache directory."""
    repo_path = cache_dir / repo_name
    
    # Return existing clone if it exists
    if repo_path.exists():
        return repo_path
    
    repo_url = f"https://github.com/aws-deadline/{repo_name}.git"

    subprocess.run(['git', 'clone', '--depth', '1', repo_url, str(repo_path)], 
                    check=True, capture_output=True)
    return repo_path

def process_user_guide(integration_key, repo_path):
    user_guide_path = repo_path / 'docs' / 'user_guide'
    
    if not user_guide_path.exists():
        return
    
    # Copy user guide files
    for md_file in user_guide_path.glob("*.md"):
        if md_file.name == 'index.md':
            dest_path = f"{integration_key}/user-guide.md"
        else:
            dest_path = f"{integration_key}/user-guide/{md_file.name}"
        
        content = md_file.read_text()
        # Fix image paths
        content = content.replace('../images/', '../../images/')
        content = content.replace('images/', f'../../images/{integration_key}/')
        
        # Fix links to other user guide files only for the main user guide file
        if md_file.name == 'index.md':
            import re
            # Replace links like "installation.md" or "installation.md#anchor" with "installation/" or "installation/#anchor"
            content = re.sub(r'(\[.*?\])\(([^/)]+)\.md(#[^)]*)?\)', r'\1(\2/\3)', content)
        
        with mkdocs_gen_files.open(dest_path, "w") as f:
            f.write(content)
    
    # Copy images if they exist
    images_path = user_guide_path / 'images'
    if images_path.exists():
        for img_file in images_path.rglob("*"):
            if img_file.is_file():
                rel_path = img_file.relative_to(images_path)
                dest_path = f"images/{integration_key}/{rel_path}"
                
                with mkdocs_gen_files.open(dest_path, "wb") as f:
                    f.write(img_file.read_bytes())

print("=== GENERATE_CONTENT.PY RUNNING ===")

nav_structure = [{"Home": "index.md"}]

# Use a local cache for GH release data and repo clones so docs rebuild faster during local development
cache_dir = Path('.cache')
cache_dir.mkdir(exist_ok=True)

sorted_integrations = sorted(INTEGRATIONS.items(), key=lambda x: x[1]['display_name'])

for integration_key, integration_config in sorted_integrations:
    print(f"Creating page for {integration_key}...")
    
    display_name = integration_config['display_name']
    has_user_guide = integration_config['has_user_guide']
    
    if has_user_guide:
        repo_path = clone_repo(integration_config['repo'], cache_dir)
        process_user_guide(integration_key, repo_path)
        
        content = f"""AWS Deadline Cloud integration for {display_name}.

## Resources

- [User Guide](user-guide.md) - Installation and usage instructions
- [Releases](releases.md) - Release notes and downloads
- [GitHub Repository](https://github.com/aws-deadline/{integration_config['repo']})
- [Issues](https://github.com/aws-deadline/{integration_config['repo']}/issues)
"""
        # Add to navigation with user guide
        nav_structure.append({
            display_name: [
                {"Overview": f"{integration_key}/index.md"},
                {"User Guide": f"{integration_key}/user-guide.md"},
                {"Releases": f"{integration_key}/releases.md"}
            ]
        })
    else:
        content = f"""AWS Deadline Cloud integration for {display_name}

## Resources

- [Releases](releases.md) - Release notes and downloads
- [GitHub Repository](https://github.com/aws-deadline/{integration_config['repo']})
- [Issues](https://github.com/aws-deadline/{integration_config['repo']}/issues)
"""
        # Add to navigation without user guide
        nav_structure.append({
            display_name: [
                {"Overview": f"{integration_key}/index.md"},
                {"Releases": f"{integration_key}/releases.md"}
            ]
        })
    
    with mkdocs_gen_files.open(f"{integration_key}/index.md", "w") as f:
        f.write(f"# Overview\n\n{content}")
    
    generate_release_page(integration_key, integration_config)

# Update mkdocs.yml with navigation
mkdocs_path = Path("mkdocs.yml")
with open(mkdocs_path, 'r') as f:
    original_config = yaml.safe_load(f)

# Create updated config
config = original_config.copy()
config['site_name'] = 'AWS Deadline Cloud Integrations'
config['extra'] = {'generator': False}  # Hide mkdocs-material call out
config['nav'] = nav_structure

# Only save if config has changed, otherwise mkdocs --watch will loop endlessly
if config != original_config:
    with open(mkdocs_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print("Updated mkdocs.yml")
else:
    print("mkdocs.yml unchanged, skipping update")

print("=== GENERATE_CONTENT.PY COMPLETE ===")
