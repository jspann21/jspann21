"""Refresh only marked star, fork, and release-download counts in a README."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


REPOSITORY = r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'
REGION = re.compile(
    rf'<!-- repo-stats:(?P<repo>{REPOSITORY}):start -->'
    r'(?P<content>.*?)'
    r'<!-- repo-stats:(?P=repo):end -->',
    re.DOTALL,
)


def repository_names(markdown):
    matches = list(REGION.finditer(markdown))
    if not matches or markdown.count('<!-- repo-stats:') != 2 * len(matches):
        raise ValueError('Missing or malformed repository count markers; README left unchanged.')
    names = [match['repo'] for match in matches]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate repository count markers; README left unchanged.')
    return names


def validate_counts(counts):
    for kind in ('stars', 'forks', 'downloads'):
        value = counts[kind]
        if type(value) is not int or value < 0:
            raise ValueError(f'Invalid {kind} count; README left unchanged.')
    return counts


def request_json(path, token=None):
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'github-profile-count-refresh',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(
        f'https://api.github.com{path}', headers=headers
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            return payload
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f'GitHub returned HTTP {error.code} for {path}.') from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError(f'Could not fetch counts for {path}.') from None
        time.sleep(2 ** attempt)


def paginated(path, token=None):
    page = 1
    while True:
        batch = request_json(f'{path}?per_page=100&page={page}', token)
        if not isinstance(batch, list):
            raise ValueError('Invalid GitHub page; README left unchanged.')
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def fetch_counts(repository, token=None):
    payload = request_json(f'/repos/{repository}', token)
    if payload.get('private') is not False:
        raise ValueError(f'{repository} is no longer public; README left unchanged.')
    downloads = 0
    for release in paginated(f'/repos/{repository}/releases', token):
        if release['draft']:
            continue
        release_id = release['id']
        if type(release_id) is not int or release_id <= 0:
            raise ValueError('Invalid release ID; README left unchanged.')
        for asset in paginated(f'/repos/{repository}/releases/{release_id}/assets', token):
            count = asset['download_count']
            if type(count) is not int or count < 0:
                raise ValueError('Invalid release download count; README left unchanged.')
            downloads += count
    return validate_counts({
        'stars': payload['stargazers_count'],
        'forks': payload['forks_count'],
        'downloads': downloads,
    })


def render_counts(repository, counts):
    validate_counts(counts)
    entries = []
    for kind, endpoint in (('stars', 'stargazers'), ('forks', 'forks'), ('downloads', 'releases')):
        value = counts[kind]
        if value == 0:
            continue
        url = html.escape(f'https://github.com/{repository}/{endpoint}', quote=True)
        icon = f'<img src="profile-assets/{kind}.svg" width="13" height="13" alt="{kind}">'
        label = 'GitHub release asset downloads' if kind == 'downloads' else kind
        entries.append(f'<a href="{url}" title="{value} {label}">{icon} {value}</a>')
    return ' &nbsp; <sup>' + ' &nbsp; '.join(entries) + '</sup>' if entries else ''


def refresh_markdown(markdown, counts):
    names = repository_names(markdown)
    for name in names:
        validate_counts(counts[name])

    def replace(match):
        name = match['repo']
        return (
            f'<!-- repo-stats:{name}:start -->'
            + render_counts(name, counts[name])
            + f'<!-- repo-stats:{name}:end -->'
        )

    return REGION.sub(replace, markdown)


def update_readme(readme, fetcher=fetch_counts, token=None):
    # Fetch every repository successfully before changing the file.
    original = readme.read_bytes().decode('utf-8')
    names = repository_names(original)
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = dict(zip(names, pool.map(lambda name: fetcher(name, token), names)))
    updated = refresh_markdown(original, fetched)
    if updated == original:
        return False
    temporary = readme.with_name(readme.name + '.counts.tmp')
    temporary.write_bytes(updated.encode('utf-8'))
    temporary.replace(readme)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--readme', type=Path,
        default=Path(__file__).resolve().parents[1] / 'README.md',
    )
    args = parser.parse_args()
    try:
        changed = update_readme(args.readme, token=os.environ.get('GITHUB_TOKEN'))
    except Exception as error:
        print(f'Count refresh failed: {error}', file=sys.stderr)
        return 1
    print('Updated profile counts.' if changed else 'Profile counts are current.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
