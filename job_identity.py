"""공고 정체성: 확인된 회사 별칭·모집 대응표와 사이트 공고 번호만 사용한다."""
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def normalized_text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()


def company_base(value):
    value = unicodedata.normalize('NFKC', value).strip()
    legal = r'(?:\(\s*[주유]\s*\)|주식회사|유한회사|유한책임회사)'
    value = re.sub(r'^\s*' + legal + r'\s*|\s*' + legal + r'\s*$', '', value)
    value = re.sub(r'\s+(?:co\.?\s*,?\s*ltd\.?|inc\.?|corp\.?|corporation|limited|ltd\.?)$', '', value, flags=re.I)
    return re.sub(r'[^\w가-힣]', '', value).casefold()


REGISTRY = json.loads(Path(__file__).with_name('identity_registry.json').read_text(encoding='utf-8'))
COMPANIES = {}
for _company in REGISTRY['companies']:
    for _alias in [_company['name'], *_company['aliases']]:
        _key = company_base(_alias)
        if _key in COMPANIES and COMPANIES[_key] != _company['name']:
            raise ValueError('회사 별칭이 둘 이상의 회사에 연결됨')
        COMPANIES[_key] = _company['name']


def company_name(value):
    return COMPANIES.get(company_base(value), value.strip())


def company_key(value):
    return company_base(company_name(value))


def advert_key(url):
    """공고 ID를 바꾸지 않는 페이지·검색·추적 인자를 정체성에서 제외한다."""
    if not url:
        return ''
    p = urlsplit(url)
    host, path = (p.hostname or '').lower().removeprefix('www.'), p.path.rstrip('/').lower()
    query = parse_qs(p.query)
    if host == 'rndjob.or.kr' and query.get('gno'):
        return (host, path, query['gno'][0])
    if host == 'saramin.co.kr' and query.get('rec_idx'):
        return (host, 'rec_idx', query['rec_idx'][0])
    patterns = {'jobkorea.co.kr': r'/recruit/gi_read/(\d+)',
                'jasoseol.com': r'/recruit/(\d+)',
                'egongmoya.letuin.com': r'/communityboard/view/(\d+/\d+/\d+)'}
    if host in patterns:
        m = re.fullmatch(patterns[host], path)
        if m:
            return (host, m[1])
    if host.endswith('.recruiter.co.kr'):
        match = re.fullmatch(r'/career/jobs/(\d+)', path)
        if match:
            return (host, 'career/jobs', match[1])
    return url


def round_conflict(a, b):
    if a.get("deadline") and b.get("deadline") and a["deadline"][:4] != b["deadline"][:4]:
        return True
    for pattern in (r'20\d{2}', r'\d+\s*(?:기|차)', r'(?:상|하)반기', r'[1-4]\s*분기'):
        left = {normalized_text(v) for v in re.findall(pattern, a['title'])}
        right = {normalized_text(v) for v in re.findall(pattern, b['title'])}
        if left and right and not left.intersection(right):
            return True
    return False


def campaign_for(record):
    urls = {advert_key(u) for u in record.get('urls', []) if u}
    matches = []
    for campaign in REGISTRY['campaigns']:
        if company_key(record['company']) != company_key(campaign['company']):
            continue
        if not urls.intersection(advert_key(u) for u in campaign['urls']):
            continue
        years = set(re.findall(r'20\d{2}', record['title']))
        if years and years != {campaign['year']}:
            continue
        if record.get('deadline') and record['deadline'] != campaign['end']:
            continue
        matches.append(campaign)
    return matches[0] if len(matches) == 1 else None


def role_key(record, campaign=None):
    title = normalized_text(record['title'])
    if campaign:
        for role in campaign['roles']:
            if title in {normalized_text(role + suffix) for suffix in ('', ' (인턴)', ' (채용형 인턴)', ' (채용연계형 인턴)')}:
                return normalized_text(role)
    return title


def is_summary(record, campaign=None):
    campaign = campaign or campaign_for(record)
    return bool(campaign and normalized_text(record['title']) in {normalized_text(t) for t in campaign['summary_titles']})


def match_kind(a, b):
    """summary-overlap은 정리 제안용이며 자동 갱신 대상이 아니다."""
    if round_conflict(a, b):
        return None
    left = {advert_key(u) for u in a.get('urls', []) if u}
    right = {advert_key(u) for u in b.get('urls', []) if u}
    if (normalized_text(a['title']) == normalized_text(b['title'])
        and any(isinstance(key, tuple) for key in left.intersection(right))):
        return 'same-ad-role'
    if company_key(a['company']) != company_key(b['company']):
        return None
    ca, cb = campaign_for(a), campaign_for(b)
    if ca and cb and ca['id'] != cb['id']:
        return None
    if ca and cb and ca['id'] == cb['id']:
        if is_summary(a, ca) or is_summary(b, cb):
            return 'summary-overlap'
        if role_key(a, ca) == role_key(b, cb):
            return 'verified-campaign-role'
        return None
    if normalized_text(a['title']) != normalized_text(b['title']):
        return None
    left = {advert_key(u) for u in a.get('urls', []) if u}
    right = {advert_key(u) for u in b.get('urls', []) if u}
    if left.intersection(right):
        return 'same-ad-role'
    origins_left = {tuple(value) for value in a.get('origin_ids', [])}
    origins_right = {tuple(value) for value in b.get('origin_ids', [])}
    if (not left or not right) and origins_left.intersection(origins_right):
        if a.get('deadline') and b.get('deadline') and a['deadline'] != b['deadline']:
            return None
        return 'same-origin-role'
    return None
