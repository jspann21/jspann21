"""Refresh only marked star, fork, and release-download counts in a README."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
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


def render_stats_svg(repository, counts):
    validate_counts(counts)
    shapes = {
        'stars': '<path d="m8 1.4 2.04 4.14 4.57.66-3.31 3.22.78 4.56L8 11.83l-4.08 2.15.78-4.56L1.39 6.2l4.57-.66L8 1.4Z"/>',
        'forks': '<circle cx="4" cy="3" r="1.7"/><circle cx="12" cy="3" r="1.7"/><circle cx="8" cy="13" r="1.7"/><path d="M4 4.7V6a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2V4.7M8 8v3.3"/>',
        'downloads': '<path d="M8 1.5v8m-3-3 3 3 3-3M2.5 10.5v3h11v-3"/>',
    }
    groups, labels, offset = [], [], 0
    for kind in ('stars', 'forks', 'downloads'):
        value = counts[kind]
        if value == 0:
            continue
        label = 'GitHub release asset downloads' if kind == 'downloads' else kind
        labels.append(f'{value} {label}')
        color = '#e3b341' if kind == 'stars' else '#9198a1'
        groups.append(
            f'<g transform="translate({offset} 0)">'
            f'<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">{shapes[kind]}</g>'
            f'<text x="20" y="12" fill="#9198a1" font-family="monospace" font-size="11">{value}</text></g>'
        )
        offset += 20 + 7 * len(str(value)) + 10
    if not groups:
        return 0, '', ''
    width = offset - 10
    label = ', '.join(labels)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="16" viewBox="0 0 {width} 16" role="img" aria-label="{html.escape(label,quote=True)}">'
        f'<title>{html.escape(label)}</title>' + ''.join(groups) + '</svg>\n'
    )
    return width, svg, label


def stats_filename(repository):
    return repository.replace('/', '--') + '.svg'


def render_counts(repository, counts):
    width, svg, label = render_stats_svg(repository, counts)
    if not svg:
        return ''
    version = hashlib.sha256(svg.encode('utf-8')).hexdigest()[:12]
    source = f'profile-assets/counts/{stats_filename(repository)}?v={version}'
    return (
        f' &nbsp; <picture><img src="{source}" width="{width}" height="16" '
        f'align="absmiddle" alt="{html.escape(label,quote=True)}" title="{html.escape(label,quote=True)}"></picture>'
    )


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
    changed = False
    assets = readme.parent / 'profile-assets' / 'counts'
    assets.mkdir(parents=True, exist_ok=True)
    for name in names:
        _, svg, _ = render_stats_svg(name, fetched[name])
        target = assets / stats_filename(name)
        if not svg:
            if target.exists():
                target.unlink()
                changed = True
        elif not target.exists() or target.read_bytes() != svg.encode('utf-8'):
            temporary = target.with_suffix('.svg.tmp')
            temporary.write_bytes(svg.encode('utf-8'))
            temporary.replace(target)
            changed = True
    if updated != original:
        temporary = readme.with_name(readme.name + '.counts.tmp')
        temporary.write_bytes(updated.encode('utf-8'))
        temporary.replace(readme)
        changed = True
    return changed


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
