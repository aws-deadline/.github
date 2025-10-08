#!/usr/bin/env python3
import os
from pathlib import Path
import re
import requests
from urllib.parse import urlparse

def validate_site(site_dir):
    errors = []
    site_path = Path(site_dir)
    verified_links = set()
    
    for html_file in site_path.rglob("*.html"):
        content = html_file.read_text()
        
        # Check images
        for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', content):
            src = match.group(1)
            if src.startswith(('http', '//')):
                # External image - check with HEAD request if not already verified
                if src in verified_links:
                    continue
                try:
                    print(f"Verifying external link: {src}")
                    response = requests.head(src, timeout=10, allow_redirects=True)
                    if response.status_code != 200:
                        errors.append(f"External image error {response.status_code}: {src} in {html_file}")
                except requests.RequestException as e:
                    errors.append(f"External image failed: {src} in {html_file} ({e})")
                finally:
                    verified_links.add(src)
            else:
                if src.startswith('/'):
                    # Absolute path - check from site root
                    img_path = (site_path / src.lstrip('/')).resolve()
                else:
                    # Relative path - check from current file
                    img_path = (html_file.parent / src).resolve()
                
                if not img_path.exists():
                    errors.append(f"Missing image: {src} in {html_file}")
        
        for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', content):
            href = match.group(1)
            if href.startswith(('http', '//')):
                # External link - check with HEAD request if not already verified
                
                # Skip GitHub release links. There are too many of them and they're generated programmatically.
                if href.startswith("https://github.com/") and ("commit" in href or "releases" in href):
                    continue
                
                # Skip this site. It blocks programmatic access.
                if href.startswith("https://support.maxon.net"):
                    # This site blocks programmatic access. Skip it.
                    continue

                if href in verified_links:
                    continue
                try:
                    print(f"Verifying external link: {href}")
                    response = requests.head(href, timeout=10, allow_redirects=True)
                    if response.status_code != 200:
                        errors.append(f"External link error {response.status_code}: {href} in {html_file}")
                except requests.RequestException as e:
                    errors.append(f"External link failed: {href} in {html_file} ({e})")
                finally:
                    verified_links.add(href)
            elif href.startswith(('#', 'mailto:')):
                continue
            else:
                # Internal link - check file path
                if href.startswith('/'):
                    # Absolute path - check from site root
                    link_path = (site_path / href.lstrip('/')).resolve()
                else:
                    # Relative path - check from current file
                    link_path = (html_file.parent / href).resolve()
                
                # If link doesn't exist, try directory/index.html for directory links
                if not link_path.exists():
                    if href.endswith('/') or '#' in href:
                        # Extract base path without anchor
                        base_href = href.split('#')[0]
                        if base_href.endswith('/'):
                            base_href = base_href.rstrip('/')
                        
                        if href.startswith('/'):
                            index_path = (site_path / base_href.lstrip('/') / 'index.html').resolve()
                        else:
                            index_path = (html_file.parent / base_href / 'index.html').resolve()
                        
                        if index_path.exists():
                            continue  # Valid directory link
                    
                    errors.append(f"Broken link: {href} in {html_file}")
    
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        exit(1)
    print("All links and images validated successfully")

if __name__ == "__main__":
    validate_site("site")
