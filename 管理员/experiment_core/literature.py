from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List


USER_AGENT = 'MultiAgentQuantExperiment/0.1 (research use)'


def _fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _plain_text(value: str) -> str:
    value = html.unescape(value or '')
    value = re.sub(r'<[^>]+>', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def search_arxiv(query: str, as_of: str, limit: int = 4) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({
        'search_query': 'all:' + query,
        'start': 0,
        'max_results': max(limit * 2, limit),
        'sortBy': 'relevance',
        'sortOrder': 'descending',
    })
    raw = _fetch('https://export.arxiv.org/api/query?' + params)
    root = ET.fromstring(raw)
    namespace = {'atom': 'http://www.w3.org/2005/Atom'}
    cutoff = dt.date.fromisoformat(as_of)
    results: List[Dict[str, Any]] = []
    for entry in root.findall('atom:entry', namespace):
        published = (entry.findtext('atom:published', default='', namespaces=namespace))[:10]
        try:
            if dt.date.fromisoformat(published) > cutoff:
                continue
        except ValueError:
            continue
        authors = [
            node.findtext('atom:name', default='', namespaces=namespace)
            for node in entry.findall('atom:author', namespace)
        ]
        results.append({
            'database': 'arXiv',
            'title': _plain_text(entry.findtext('atom:title', '', namespace)),
            'authors': [name for name in authors if name],
            'published': published,
            'url': entry.findtext('atom:id', '', namespace),
            'abstract': _plain_text(entry.findtext('atom:summary', '', namespace)),
        })
        if len(results) >= limit:
            break
    return results


def _crossref_date(item: Dict[str, Any]) -> str:
    for key in ('published-print', 'published-online', 'issued', 'created'):
        date_parts = ((item.get(key) or {}).get('date-parts') or [])
        if date_parts and date_parts[0]:
            parts = list(date_parts[0]) + [1, 1]
            try:
                return '%04d-%02d-%02d' % tuple(parts[:3])
            except (TypeError, ValueError):
                continue
    return ''


def search_crossref(query: str, as_of: str, limit: int = 4) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({
        'query': query,
        'rows': max(limit * 2, limit),
        'filter': 'until-pub-date:' + as_of,
        'select': 'title,author,published-print,published-online,issued,created,URL,abstract,DOI',
    })
    raw = _fetch('https://api.crossref.org/works?' + params)
    payload = json.loads(raw.decode('utf-8'))
    results: List[Dict[str, Any]] = []
    for item in ((payload.get('message') or {}).get('items') or []):
        title_parts = item.get('title') or []
        title = _plain_text(title_parts[0] if title_parts else '')
        if not title:
            continue
        authors = []
        for author in item.get('author') or []:
            name = ('%s %s' % (
                author.get('given', ''), author.get('family', '')
            )).strip()
            if name:
                authors.append(name)
        results.append({
            'database': 'Crossref',
            'title': title,
            'authors': authors,
            'published': _crossref_date(item),
            'url': item.get('URL') or '',
            'doi': item.get('DOI') or '',
            'abstract': _plain_text(item.get('abstract') or ''),
        })
        if len(results) >= limit:
            break
    return results


def collect_literature(
    queries: List[str], as_of: str, per_database_limit: int = 3
) -> Dict[str, Any]:
    collection: Dict[str, Any] = {
        'as_of': as_of,
        'queries': queries,
        'results': [],
        'errors': [],
    }
    seen = set()
    for query in queries:
        for database, search in (
            ('arXiv', search_arxiv),
            ('Crossref', search_crossref),
        ):
            try:
                items = search(query, as_of, per_database_limit)
            except Exception as exc:
                collection['errors'].append({
                    'query': query,
                    'database': database,
                    'error': str(exc),
                })
                continue
            for item in items:
                key = (item.get('doi') or item.get('url') or item.get('title', '')).lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                item['query'] = query
                collection['results'].append(item)
    return collection


def literature_to_markdown(collection: Dict[str, Any]) -> str:
    lines = [
        '# Literature search results',
        '',
        'Information cutoff: `%s`' % collection.get('as_of', ''),
        '',
    ]
    errors = collection.get('errors') or []
    if errors:
        lines.extend(['## Search errors', ''])
        for item in errors:
            lines.append(
                '- %s / %s: %s' % (
                    item.get('database'), item.get('query'), item.get('error')
                )
            )
        lines.append('')
    lines.extend(['## Results', ''])
    for index, item in enumerate(collection.get('results') or [], 1):
        lines.extend([
            '### %d. %s' % (index, item.get('title', 'Untitled')),
            '',
            '- Database: %s' % item.get('database', ''),
            '- Published: %s' % item.get('published', ''),
            '- Authors: %s' % ', '.join(item.get('authors') or []),
            '- URL: %s' % item.get('url', ''),
            '- Search query: %s' % item.get('query', ''),
            '',
            item.get('abstract') or '(No abstract returned by the database.)',
            '',
        ])
    if not collection.get('results'):
        lines.append('No results were returned. The researcher must disclose this limitation.')
    return '\n'.join(lines)
