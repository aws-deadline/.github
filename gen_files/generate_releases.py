#!/usr/bin/env python3
"""Generate release notes from GitHub releases."""

import subprocess
import json
import mkdocs_gen_files
from datetime import datetime
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))
from config import INTEGRATIONS

CACHE_DIR = Path('.cache')

def get_cache_path(repo_name, cache_type):
    """Get cache file path for a repo and cache type."""
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{repo_name}_{cache_type}.json"

def load_cache(cache_path):
    """Load data from cache file if it exists."""
    if cache_path.exists():
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None

def save_cache(cache_path, data):
    with open(cache_path, 'w') as f:
        json.dump(data, f)

def fetch_release_details(repo_name, tag_name):
    """Fetch detailed release info including body using GitHub CLI."""
    cache_path = get_cache_path(repo_name, f"release_{tag_name}")
    
    # Try to load from cache first
    cached_data = load_cache(cache_path)
    if cached_data is not None:
        return cached_data
    
    result = subprocess.run([
        'gh', 'release', 'view', tag_name,
        '--repo', f'aws-deadline/{repo_name}',
        '--json', 'body'
    ], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    save_cache(cache_path, data)
    return data

def fetch_releases(repo_name):
    """Fetch releases using GitHub CLI."""
    cache_path = get_cache_path(repo_name, "releases")
    
    # Try to load from cache first
    cached_data = load_cache(cache_path)
    if cached_data is not None:
        return cached_data
    
    try:
        result = subprocess.run([
            'gh', 'release', 'list', 
            '--repo', f'aws-deadline/{repo_name}',
            '--json', 'tagName,publishedAt,name,isDraft'
        ], check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        save_cache(cache_path, data)
        return data
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to fetch releases for {repo_name}: {e.stderr}")
    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse release data for {repo_name}: {e}")

def format_release_date(date_str):
    date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    return date_obj.strftime('%B %d, %Y')

def generate_release_page(integration_key, integration_config):
    """Generate release notes page for an integration."""
    repo_name = integration_config['repo']
    display_name = integration_config['display_name']
    releases = fetch_releases(repo_name)
    
    # Generate release notes content
    content = f"# {display_name} Releases\n\n"
    
    for release in releases:
        if release.get('isDraft', False):
            continue  # Skip draft releases
            
        version = release['tagName']
        name = release.get('name', '').strip()
        
        # Fetch detailed release info including body
        release_details = fetch_release_details(repo_name, version)
        body = release_details.get('body', '').strip()
                
        # Add release notes body if available
        if body:
            content += f"{body}\n\n"
        
        content += f"[View on GitHub](https://github.com/aws-deadline/{repo_name}/releases/tag/{version})\n\n"
        content += "---\n\n"
    
    with mkdocs_gen_files.open(f"{integration_key}/releases.md", "w") as f:
        f.write(content)

def main():
    """Generate release notes for all integrations."""
    for integration_key, integration_config in INTEGRATIONS.items():
        print(f"Generating releases for {integration_key}...")
        generate_release_page(integration_key, integration_config)

if __name__ == "__main__":
    main()
