"""Compare retained native DOM title order against the retained production reader."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

SCOPE = Path(__file__).resolve().parent
snapshot_path = SCOPE / 'fixture-browser-pages-before-failures.json'
snapshot = json.loads(snapshot_path.read_text())['snapshot']
pattern = re.compile(r'Synthetic item 6 (?:technology|policy): retained raw article \d{2}')
records = []
for page in (1, 2, 3):
    expected = snapshot[f'browser_raw_page_{page}']['items']
    capture = SCOPE / 'browser' / f'raw-page-{page}.dom.txt'
    native = capture.read_text()
    actual_titles = pattern.findall(native)
    expected_titles = [item['title'] for item in expected]
    records.append({
        'page': page, 'offset': (page - 1) * 12, 'limit': 12, 'total': 26,
        'native_capture': str(capture.relative_to(SCOPE)),
        'native_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(),
        'expected_titles': expected_titles, 'actual_titles': actual_titles,
        'title_order_equal': actual_titles == expected_titles,
        'expected_records': [{key: item[key] for key in ('id', 'title', 'source_name', 'url', 'published_at', 'captured_at', 'admitted_at', 'enrichment_state', 'event_id')} for item in expected],
        'field_scope': 'Exact title order is observed in native DOM; expected per-record fields identify retained API/service records and are not claims of unobserved DOM values.',
    })
repeat = SCOPE / 'browser' / 'raw-pagination-repeat.dom.txt'
repeat_titles = pattern.findall(repeat.read_text())
all_expected = [title for record in records for title in record['expected_titles']]
grouped_id = json.loads((SCOPE / 'fixture-initial.json').read_text())['actions']['brief']['article_id']
result = {
    'schema': 'item6-native-browser-raw-pagination-comparison.v1',
    'snapshot': snapshot_path.name, 'snapshot_sha256': hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    'pages': records, 'observed_lengths': [len(record['actual_titles']) for record in records],
    'unique_observed_titles': len(set(title for record in records for title in record['actual_titles'])),
    'repeat_capture': str(repeat.relative_to(SCOPE)),
    'repeat_sha256': hashlib.sha256(repeat.read_bytes()).hexdigest(),
    'repeat_exact_order_equal': repeat_titles == all_expected,
    'first_page_stable_observed': 'firstPageStable: true' in repeat.read_text(),
    'grouped_article_id': grouped_id,
    'grouped_article_excluded_from_expected_raw_pages': grouped_id not in [item['id'] for page in (1, 2, 3) for item in snapshot[f'browser_raw_page_{page}']['items']],
}
result['status'] = 'PASS' if all(record['title_order_equal'] for record in records) and result['repeat_exact_order_equal'] and result['first_page_stable_observed'] and result['unique_observed_titles'] == 26 and result['grouped_article_excluded_from_expected_raw_pages'] else 'FAIL'
(SCOPE / 'browser-raw-pagination-comparison.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({key: result[key] for key in ('status', 'observed_lengths', 'unique_observed_titles', 'repeat_exact_order_equal', 'first_page_stable_observed')}))
if result['status'] != 'PASS':
    raise SystemExit(1)
