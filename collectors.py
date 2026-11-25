"""공개 채용 목록/상세 어댑터. 로그인·추천 문구는 판정에 사용하지 않으며 상세 이미지는 OCR로 판독한다."""

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from http.cookiejar import CookieJar
from http.client import RemoteDisconnected
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, unquote_plus, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor

from bs4 import BeautifulSoup
from notion_sync import make_posting, deadline_from_text
from profile_config import PROFILE, classify_interest
from eligibility import check_eligibility, jd_sections
INTEREST_FIELDS = {g["name"] for g in PROFILE["interests"]}
from ocr import enrich_html, scope_ocr_role, split_ocr_roles, OCRError


NEW_SITES = {source["key"]: (source["name"], source["url"]) for source in PROFILE.get("sources", [])}


class CollectionError(RuntimeError):
    pass


class PublicReader:
    def __init__(self, interval=0.5):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.cache = {}
        self.interval = interval

    def read(self, url, data=None, referer=None):
        key = (url, data)
        if key in self.cache:
            return self.cache[key]
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"}
        if referer:
            headers.update({"Referer": referer, "X-Requested-With": "XMLHttpRequest"})
        for attempt in range(4):
            time.sleep(self.interval)
            try:
                with self.opener.open(Request(url, data=data, headers=headers), timeout=25) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                break
            except HTTPError as error:
                if error.code not in {429, 502, 503, 504} or attempt == 3:
                    raise
                retry = error.headers.get("Retry-After")
                try:
                    delay = float(retry) if retry else 5 * (2 ** attempt)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = 5 * (2 ** attempt)
                if delay > 60:
                    raise CollectionError("서버 요청 대기시간이 60초를 초과해 이번 공고는 다음 실행으로 넘김") from None
                time.sleep(max(1, delay))
            except RemoteDisconnected:
                if attempt == 3:
                    raise
                time.sleep(5 * (2 ** attempt))
        text = raw.decode(charset, errors="replace")
        self.cache[key] = text
        return text

    def soup(self, url, **kwargs):
        return BeautifulSoup(self.read(url, **kwargs), "html.parser")

    def json(self, url):
        return json.loads(self.read(url))


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def iso_deadline(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone(timedelta(hours=9)))
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return None


def job_schema(soup):
    def walk(value):
        if isinstance(value, dict):
            types = value.get("@type", [])
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                yield value
            for item in value.values():
                if isinstance(item, (dict, list)):
                    yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue


def next_props(soup):
    script = soup.select_one("#__NEXT_DATA__")
    if not script:
        raise CollectionError("Next.js 공개 공고 데이터를 찾을 수 없음")
    return json.loads(script.string)["props"]["pageProps"]


def qualification(title, career, education=""):
    from fetch_page import excluded_qualification
    for value in (title, career, education):
        if excluded_qualification(value):
            return "자격 제외", value
    if not PROFILE.get("allow_experienced", False) and not re.search(r"신입|^무관$|경력\s*무관", career.strip()):
        return "자격 제외", "신입 지원 근거 부족 | " + (career or "표시 없음")
    return None


def classify_body(title, html, industry="", base_url="", *, location="", education=""):
    try:
        html = enrich_html(html, base_url)
    except OCRError as error:
        return "검토 필요", str(error)
    if re.search(r"각\s*(?:부문|직무|분야)|부문별|직무별|전\s*직군|인재풀|다수\s*부문|부문별\s*공개채용", title):
        return "검토 필요", "직무별 요강 분리 필요"
    lines, sections = jd_sections(html)
    if not sections["duties"]:
        return "검토 필요", "담당업무 구획 미확인"
    decision = classify_interest(title, sections["duties"], industry=industry)
    if decision[0] not in INTEREST_FIELDS:
        return decision
    return check_eligibility(lines, sections, PROFILE, location=location, education=education) or decision


def poster_deadline(html):
    """명시된 모집 날짜 범위의 끝 또는 모집기간의 연도 포함 날짜만 사용한다."""
    value=BeautifulSoup(html,'html.parser').get_text('\n',strip=True)
    direct=deadline_from_text(value)
    if direct:
        return direct
    date=r'(20\d{2})[.년/\-]\s*(\d{1,2})[.월/\-]\s*(\d{1,2})일?'
    dates=[]
    for match in re.finditer(date+r'(?:\s*\d{1,2}:\d{2})?\s*[~～–]\s*'+date,value):
        dates.append('-'.join((match[4],match[5].zfill(2),match[6].zfill(2))))
    for match in re.finditer(r'(?:모집|접수)\s*기간[^\n]*\n?\s*[-·•]?\s*'+date,value):
        # 범위면 위에서 확보한 종료일을 사용한다.
        if not dates:
            dates.append('-'.join((match[1],match[2].zfill(2),match[3].zfill(2))))
    if len(set(dates))==1:
        try:
            return datetime.strptime(dates[0],'%Y-%m-%d').date().isoformat()
        except ValueError:
            pass
    return None


def egong_roles(title, html, base_url, *, location="", education=""):
    lines, sections = jd_sections(html)
    if (location and education and sections["duties"] and sections["required"]
        and not re.search(r"각\s*(?:부문|직무|분야)|(?:부문|직무)별|전\s*직군|다수\s*부문", title)):
        # 요강이 HTML로 완비되면 회사 소개 이미지의 OCR 결과에 의존하지 않는다.
        decision = classify_interest(title, sections["duties"])
        if decision[0] in INTEREST_FIELDS:
            decision = check_eligibility(lines, sections, PROFILE, location=location, education=education) or decision
        return [(title, decision)]
    try:
        html = enrich_html(html, base_url)
    except OCRError as error:
        return [(title, ("검토 필요", str(error)))]
    separate = split_ocr_roles(html, title)
    if not separate:
        lines = BeautifulSoup(html, "html.parser").select("section[data-ocr] p")
        if sum(bool(re.search(r"담당\s*업무", p.get_text())) for p in lines) > 1:
            return [(title, ("수집 제외", "여러 직무의 OCR 자격 구획을 명확히 분리하지 못함"))]
        return [(title, classify_body(title, html, base_url=base_url, location=location, education=education))]
    decisions = []
    for role_title, body in separate:
        intern = "인턴" in title and re.search(r"졸업\s*예정", BeautifulSoup(body, "html.parser").get_text())
        career = "신입" if "신입" in role_title or intern or (len(separate) == 1 and "신입" in title) or re.search(r"신입", BeautifulSoup(body, "html.parser").get_text()) else ""
        if intern and "인턴" not in role_title:
            role_title += " (채용형 인턴)" if re.search(r"채용\s*(?:연계)?형\s*인턴", title) else " (인턴)"
        decision = qualification(role_title, career) or classify_body(role_title, body, location=location, education=education)
        decisions.append((role_title, decision))
    return decisions


def scoped_body(html, role, roles=()):
    """다직무 공고를 한 행으로 섞지 않도록 직무가 명시된 최소 구획만 사용한다."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=lambda s: s and role.strip() == s.strip()):
        for parent in node.parents:
            if (parent.name in {"tr", "section", "article", "li"}
                and not parent.has_attr("data-ocr")
                and not any(t.strip() in set(roles)-{role} for t in parent.stripped_strings)
                and len(text(parent)) > len(role) + 30):
                return str(parent)
    return ""


def labelled(soup, label):
    for dt in soup.select("dt"):
        if text(dt).replace(" ", "") == label:
            return text(dt.find_next_sibling("dd"))
    return ""


def company_size(soup):
    """기업정보에 명시된 기업 규모·형태만 수집한다."""
    values = []
    for label in soup.select("dt,th"):
        if re.sub(r"\s+", "", text(label)) in {"기업규모", "기업형태"}:
            value = text(label.find_next_sibling(["dd", "td"]))
            if value:
                values.append(value)
    return " / ".join(dict.fromkeys(values))


def listing_cards(key, soup):
    pattern = r"rec_idx=(\d+)" if key.startswith("saramin") else r"/Recruit/GI_Read/(\d+)"
    cards = []
    seen = set()
    selector = ".list_recruiting .box_item a.str_tit[href]" if key == "saramin-home" else "a[href]"
    for anchor in soup.select(selector):
        match = re.search(pattern, anchor["href"], re.I)
        if not match or match[1] in seen:
            continue
        if key.startswith("saramin"):
            card = anchor.find_parent(class_="box_item") or anchor.find_parent("li") or anchor
            company = text(card.select_one(".c_name,.corp,.company_nm,.corp_name,.company_info .str_tit"))
            title = text(anchor.select_one(".title,.tit")) or anchor.get("title", "") or text(anchor)
            career = text(card.select_one(".career"))
            if not career and card.select_one("ul.desc"):
                career = " ".join(text(x) for x in card.select("ul.desc li")[1:2])
            url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=" + match[1]
        else:
            card = anchor.find_parent("li") or anchor.parent
            company = text(card.select_one(".coLink"))
            title = anchor.get("title") or text(anchor)
            career = text(card.select_one(".sDesc strong"))
            url = "https://www.jobkorea.co.kr/Recruit/GI_Read/" + match[1]
        if not title:
            continue
        seen.add(match[1])
        cards.append(dict(id=match[1], company=company, title=title, career=career, url=url))
    return cards


def saramin_detail(reader, card, split_roles=False):
    base = "https://www.saramin.co.kr"
    soup = reader.soup(card["url"])
    scope = soup.select_one(f"section.jview-0-{card['id']}")
    if scope is None:
        soup = reader.soup(base + "/zf_user/jobs/relay/view-ajax",
                           data=urlencode({"rec_idx": card["id"], "rec_seq": 0,
                                           "view_type": "mail_landing", "t_ref": "non-logged_relay_view",
                                           "t_ref_content": "category_new_rec"}).encode(), referer=card["url"])
        scope = soup.select_one(f"section.jview-0-{card['id']}")
    card["company_size"] = company_size(soup)
    soup = scope or soup
    company = text(soup.select_one(".jv_header .company,.jv_header .corp_name")) or card["company"]
    title = text(soup.select_one("h1.tit_job")) or card["title"]
    career = labelled(soup, "경력")
    decision = qualification(title, career, labelled(soup, "학력"))
    if decision:
        return company, title, ([(title, decision)] if split_roles else decision), None
    frame = soup.select_one('iframe#iframe_content_0[src]')
    if frame and parse_qs(urlsplit(frame["src"]).query).get("rec_idx") != [card["id"]]:
        raise CollectionError("사람인 상세 iframe 공고 번호 불일치")
    if not frame:
        raise CollectionError("사람인 상세요강 iframe 없음")
    html = reader.read(urljoin(base, frame["src"]), referer=card["url"])
    deadline = deadline_from_text("마감일 " + labelled(soup, "마감일"))
    location = labelled(soup, "근무지역")
    education = labelled(soup, "학력")
    detail_url = urljoin(base, frame["src"])
    decisions = (egong_roles(title, html, detail_url, location=location, education=education)
                 if split_roles else classify_body(title, html, base_url=detail_url, location=location, education=education))
    return company, title, decisions, deadline


class JobkoreaBrowser:
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        self.page = self.browser.new_page()
        self.page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "font", "media"} else route.continue_())
        return self

    def body(self, url):
        self.page.goto(url, wait_until="commit", timeout=30000)
        self.page.locator("iframe").first.wait_for(state="attached", timeout=15000)
        chunks = []
        # Jobkorea는 상세요강을 about:blank iframe에 주입한다. 추천 공고 본문은 읽지 않는다.
        for frame in self.page.frames[1:]:
            if frame.url not in {"", "about:blank"}:
                continue
            try:
                frame.locator("body").wait_for(timeout=10000)
                html = frame.content()
                if text(BeautifulSoup(html, "html.parser")) or BeautifulSoup(html, "html.parser").select_one("img[src],img[data-src]"):
                    chunks.append(html)
            except Exception:
                continue
        return "\n".join(chunks)

    def __exit__(self, *args):
        self.browser.close()
        self.pw.stop()


def catch_cards(soup):
    cards = []
    for row in soup.select("#recr_result .table_recr tbody tr"):
        anchor = row.select_one('a[href*="/NCS/RecruitInfoDetails/"]')
        match = re.search(r"/RecruitInfoDetails/(\d+)", anchor["href"]) if anchor else None
        title = text(row.select_one(".subj2,.subj"))
        if not match or not title or re.search(r"교육생|훈련생|부트캠프|교육과정", title):
            continue
        cards.append(dict(id=match[1], title=title, company=text(row.select_one(".name2,.name")),
                          career=text(row.select_one(".cond.ico1")),
                          url="https://www.catch.co.kr/NCS/RecruitInfoDetails/" + match[1]))
    return cards


def catch_featured_cards(soup):
    """동적 검색이 차단되어도 공개 HTML에 직접 제공된 대표 공고는 읽을 수 있다."""
    cards, seen = [], set()
    for anchor in soup.select('a.link[href^="/NCS/RecruitInfoDetails/"]'):
        match = re.search(r"/RecruitInfoDetails/(\d+)", anchor["href"])
        title, company = text(anchor.select_one(".subj")), text(anchor.select_one(".name"))
        if not match or match[1] in seen or not title or not company:
            continue
        seen.add(match[1])
        cards.append(dict(id=match[1], title=title, company=company, career="",
                          url="https://www.catch.co.kr/NCS/RecruitInfoDetails/" + match[1]))
    return cards


class CatchBrowser(JobkoreaBrowser):
    """API가 거절되면 일반 공개 검색 화면을 읽는다. 인증/보안 확인을 우회하지 않는다."""
    def cards(self, number):
        if self.page.url == "about:blank":
            self.page.goto(NEW_SITES["catch"][1], wait_until="commit", timeout=30000)
            self.page.locator("#recr_result .table_recr tbody tr").first.wait_for(timeout=20000)
        if number > 1:
            target = self.page.locator("#recr_result .page3 a").filter(has_text=re.compile(f"^{number}$"))
            if not target.count():
                return []
            target.click()
            self.page.wait_for_function("n => document.querySelector('#recr_result .page3 a.selected')?.textContent.trim() === String(n)", arg=number, timeout=15000)
        try:
            self.page.locator("#recr_result .table_recr tbody tr").first.wait_for(timeout=20000)
        except Exception:
            raise CollectionError("캐치 공개 검색 화면 접근 실패 (로그인/보안 확인은 수동 확인 필요)") from None
        return catch_cards(BeautifulSoup(self.page.content(), "html.parser"))


def collect_site(key, limit=50, max_pages=5, *, recheck_cards=None):
    name, url = NEW_SITES[key]
    reader = PublicReader(interval=1.5 if key == "catch" else 0.5)
    counts, failures, warnings = Counter(), [], []
    postings, audit, seen = [], [], set()
    browser = None
    catch_browser = None
    featured_only = False

    def record(card, decision, company=None, title=None, deadline=None, source_id=None):
        result, evidence = decision
        company, title = company or card.get("company", ""), title or card["title"]
        if result in INTEREST_FIELDS and not company:
            result, evidence = "수집 제외", "회사명 미확인"
        if result in INTEREST_FIELDS and PROFILE.get("prefer_large_public_recruitment"):
            size = card.get("company_size", "")
            public = bool(re.search(r"공채|공개\s*채용|신입사원\s*(?:모집|채용)|대졸\s*신입", title))
            large = bool(re.search(r"대기업|중견기업", size))
            if not large and not public:
                result, evidence = "검토 필요", "대기업·중견기업 또는 공채 근거 미확인 | " + (size or "회사 규모 미확인")
        counts[result] += 1
        audit.append(dict(id=source_id or card["id"], company=company, title=title,
                          url=card["url"], result=result, evidence=evidence))
        if result in INTEREST_FIELDS:
            # 사람인 두 진입점은 같은 사이트 ID 공간을 사용한다.
            source = "사람인" if key.startswith("saramin") else name
            postings.append(make_posting(company, title, card["url"], result, evidence, source,
                                         source_id or card["id"], deadline, company_size=card.get("company_size", "")))
            print(f"{name} | {company} | {result} | {title}\n근거: {evidence}\n{card['url']}", flush=True)

    def process(card):
        nonlocal browser
        pre = qualification(card["title"], card.get("career", ""))
        # 모집 구분이 목록에 없는 경우 상세에서 확인한다.
        if pre and (card.get("career") or pre[1] == card["title"]):
            record(card, pre)
            return
        try:
            if key.startswith("saramin"):
                company, title, decisions, deadline = saramin_detail(reader, card, split_roles=True)
                for role_title, decision in decisions:
                    record(card, decision, company, role_title, deadline)
                return
            elif key == "jobkorea":
                soup = reader.soup(card["url"])
                schema = next(job_schema(soup), None)
                if not schema:
                    raise CollectionError("JobPosting 상세 메타데이터 없음")
                title = schema.get("title") or card["title"]
                company = schema.get("hiringOrganization", {}).get("name") or card["company"]
                decision = qualification(title, str(schema.get("experienceRequirements", "")), str(schema.get("educationRequirements", "")))
                deadline = iso_deadline(schema.get("validThrough"))
                if not decision:
                    # 화면의 "상세 모집 요강" iframe이 제공하는 공개 HTML을 직접 읽는다.
                    # iframe 부착 직후 비어 있는 about:blank 프레임을 읽는 경쟁을 피한다.
                    frame_url = "https://www.jobkorea.co.kr/Recruit/GI_Read_Comt_Ifrm?" + urlencode({
                        "Gno": card["id"], "isHiringCenter": "false", "hideMapView": "false"})
                    frame = reader.soup(frame_url, referer=card["url"])
                    body = frame.select_one('[data-sentry-component="HtmlViewer"]')
                    if body is None:
                        raise CollectionError("잡코리아 공개 상세요강 본문 없음")
                    for role_title, role_decision in egong_roles(title, str(body), frame_url):
                        record(card, role_decision, company=company, title=role_title, deadline=deadline)
                    return
            elif key == "catch":
                soup = reader.soup(card["url"])
                schema = next(job_schema(soup), None)
                if not schema:
                    raise CollectionError("캐치 JobPosting 상세 메타데이터 없음")
                company, title = schema["hiringOrganization"]["name"], schema["title"]
                career = schema.get("experienceRequirements") or ""
                career = " / ".join(career) if isinstance(career, list) else str(career)
                decision = qualification(title, career, str(schema.get("educationRequirements", "")))
                deadline = iso_deadline(schema.get("validThrough"))
                if not decision:
                    frame = soup.select_one('iframe[src^="/controls/recruitDetail/"]')
                    if not frame:
                        raise CollectionError("캐치 상세요강 iframe 없음")
                    decision = classify_body(title, reader.read(urljoin(url, frame["src"])))
            elif key == "egong":
                data = reader.json(card["api"])
                if data.get("code") != 1000 or data.get("data", {}).get("is_paid"):
                    raise CollectionError("공개 상세 접근 불가")
                view = data["data"].get("view", {})
                if view.get("is_secret") == "y":
                    raise CollectionError("비공개 공고")
                company, title, deadline = card["company"], card["title"], None
                try:
                    html = enrich_html(unquote_plus(view.get("content", "")), card["url"])
                except OCRError as error:
                    record(card, ("수집 제외", str(error)))
                    return
                decisions = egong_roles(title, html, card["url"])
                separated = dict(split_ocr_roles(html, title))
                for role_title, decision in decisions:
                    body = separated.get(role_title, html if len(decisions) == 1 else "")
                    record(card, decision, title=role_title, deadline=poster_deadline(body) or (poster_deadline(html) if len(decisions) == 1 else None))
                return
            else:
                raise CollectionError("지원하지 않는 수집기")
            record(card, decision, company, title, deadline)
        except Exception as error:
            reason = f"상세 수집 실패 | {type(error).__name__}: {str(error).splitlines()[0][:200]}"
            failures.append(reason)
            record(card, ("수집 제외", reason))

    def process_jasoseol(card):
        try:
            details = next_props(reader.soup(card["url"]))["initialEmploymentCompany"]
            roles = details.get("employments") or []
            if not roles:
                raise CollectionError("모집 직무 데이터 없음")
            html = details.get("content", "")
            eligible = roles if PROFILE.get("allow_experienced", False) else [role for role in roles if role.get("division") in {1, 3}]
            ocr_error = None
            if eligible:
                try:
                    html = enrich_html(html, card["url"])
                except OCRError as error:
                    ocr_error = str(error)
            for role in roles:
                # 상세 JSON의 division 1은 화면의 신입, 2는 경력에 대응한다.
                division = role.get("division")
                career = "신입" if division == 1 else ("경력" if division == 2 else "신입/경력" if division == 3 else "")
                title = role.get("field") or card["title"]
                decision = qualification(title, career)
                body = (scoped_body(html, title, [r.get("field", "") for r in roles]) or scope_ocr_role(html, title, [r.get("field", "") for r in roles])) if len(roles) > 1 else html
                decision = decision or (("수집 제외", ocr_error) if ocr_error else classify_body(title, body, base_url=card["url"]))
                record(card, decision, title=title, deadline=iso_deadline(details.get("end_time")), source_id=f"{card['id']}:{role['id']}")
        except Exception as error:
            failures.append(f"자소설 상세 {card['id']}: {type(error).__name__}")
            record(card, ("수집 제외", failures[-1]))

    pages_read = 0
    try:
        if recheck_cards is not None:
            for index,card in enumerate(recheck_cards,1):
                if index == 1 or index % 10 == 0:
                    print(f"{name} | 재검토 광고 {index}/{len(recheck_cards)}",flush=True)
                if key == "jasoseol":
                    process_jasoseol(card)
                else:
                    process(card)
        elif key in {"saramin-home", "saramin-kangso", "jobkorea"}:
            page_url = url
            if key == "saramin-home" and set(PROFILE.get("target_regions", [])) == {"서울", "경기"}:
                filters = {"loc_mcd": "101000,102000", "panel_type": "", "search_optional_item": "n",
                           "search_done": "y", "panel_count": "y", "preview": "y"}
                page_url += "?" + urlencode(filters)
            for page in range(1, max_pages + 1):
                soup = reader.soup(page_url)
                cards = listing_cards(key, soup)
                if not cards:
                    raise CollectionError("목록에서 일반 검색 결과 공고 링크를 읽지 못함")
                if PROFILE.get("prefer_large_public_recruitment"):
                    cards.sort(key=lambda card: not bool(re.search(r"공채|공개\s*채용|신입사원|대졸\s*신입", card["title"])))
                fresh = [card for card in cards if card["id"] not in seen]
                if not fresh:
                    raise CollectionError("페이지 이동 후 새 공고를 읽지 못함")
                pages_read += 1
                for card in fresh[:limit - len(seen)]:
                    seen.add(card["id"])
                    process(card)
                if len(seen) >= limit or key != "saramin-home":
                    break
                button = soup.select_one(f'button[page="{page + 1}"][onclick]') or soup.select_one("button.BtnNext[onclick]")
                if not button:
                    break
                match = re.search(r"location\.href\s*=\s*'([^']+)'", button["onclick"])
                next_url = urljoin(url, match[1]) if match else ""
                if not next_url or urlsplit(next_url)[:3] != urlsplit(url)[:3]:
                    raise CollectionError("사람인 목록 페이지 이동 주소를 확인하지 못함")
                page_url = next_url
        elif key == "jasoseol":
            landing = reader.soup(url)
            search = next((urljoin(url, a["href"]) for a in landing.select("a[href]")
                           if urlsplit(urljoin(url, a["href"])).path == "/search"
                           and urlsplit(urljoin(url, a["href"])).netloc == urlsplit(url).netloc), None)
            if not search:
                raise CollectionError("채용달력에서 공개 공고 검색 링크를 찾지 못함")
            for page in range(1, max_pages + 1):
                props = next_props(reader.soup(search + "?" + urlencode({"page": page, "excludeClosed": "true"})))
                queries = props.get("dehydratedState", {}).get("queries", [])
                query = next((q for q in queries if q.get("queryKey", [None])[0] == "jobSearch"), None)
                if not query or int(query["queryKey"][1]["page"]) != page:
                    raise CollectionError("자소설 검색 페이지 데이터/페이지 번호 불일치")
                cards = query["state"]["data"]["data"]
                if not cards:
                    if not pages_read:
                        raise CollectionError("목록 공고 0건: 빈 검색 결과 확인 필요")
                    break
                pages_read += 1
                for item in cards:
                    if item["id"] in seen:
                        continue
                    if len(seen) >= limit:
                        break
                    seen.add(item["id"])
                    card = dict(id=str(item["id"]), title=item["title"], company=item["name"], url=f"https://jasoseol.com/recruit/{item['id']}")
                    process_jasoseol(card)
                if len(seen) >= limit:
                    break
        elif key in {"catch", "egong"}:
            for page in range(1, max_pages + 1):
                if key == "catch":
                    params = dict(Keyword="", JobCode="", Sido="", Career="", JCode="", Size="", EduLevel="", WorkPosition="", CompID="", GroupCode="", Sort=0, curpage=page, pageSize=30, onRecruitYN="Y", ExceptIDList="")
                    if catch_browser:
                        cards = catch_browser.cards(page)
                        items = cards
                    else:
                        try:
                            if page == 1:
                                reader.read(url)
                            api_url = "https://www.catch.co.kr/api/v1.0/recruit/information/getRecruitList?" + urlencode(params)
                            data = json.loads(reader.read(api_url, referer=url))
                            items = data.get("recruitData")
                            if not isinstance(items, list):
                                raise CollectionError("캐치 공개 목록 형식 변경")
                            cards = [dict(id=str(i["RecruitID"]), title=i["RecruitTitle"], company=i["CompName"], career=i.get("CareerGubunCode", ""), url=f"https://www.catch.co.kr/NCS/RecruitInfoDetails/{i['RecruitID']}") for i in items if not i.get("IsProduct")]
                        except HTTPError as error:
                            if error.code not in {403, 429}:
                                raise
                            print("캐치 | 공개 API 접근 제한: 일반 브라우저 검색 화면으로 확인", flush=True)
                            catch_browser = CatchBrowser().__enter__()
                            try:
                                cards = catch_browser.cards(page)
                            except Exception:
                                cards = catch_featured_cards(reader.soup(url))
                                if not cards:
                                    raise CollectionError("캐치 API·브라우저·공개 HTML 목록 모두 확인 불가") from None
                                featured_only = True
                                warnings.append("전체 검색 목록 접근 제한: 공개 HTML 대표 공고만 수집 (전체 검색 결과 아님)")
                            items = cards
                else:
                    data = reader.json(f"https://egongmoya.letuin.com/api/board/CommunityBoard/list/11?page={page}")
                    if data.get("code") != 1000:
                        raise CollectionError("이공계모야 공개 목록 접근 실패")
                    items = data.get("data", {}).get("list")
                    if not isinstance(items, list):
                        raise CollectionError("이공계모야 목록 형식 변경")
                    cards = []
                    for i in items:
                        if i.get("is_notice") == "y" or i.get("need_login") == "y":
                            continue
                        match = re.search(r"【(.+?)】", i["title"])
                        path = f"/CommunityBoard/view/{i['year']}/{i['board_no']}/{i['no']}"
                        cards.append(dict(id=i["no"], title=i["title"], company=match[1] if match else "", career="신입" if "신입" in i["title"] else "", url=urljoin(url, path), api="https://egongmoya.letuin.com/api/board" + path))
                if not items:
                    if not pages_read:
                        raise CollectionError("목록 공고 0건: 사이트 확인 필요")
                    break
                pages_read += 1
                for card in cards:
                    if card["id"] in seen:
                        continue
                    if len(seen) >= limit:
                        break
                    seen.add(card["id"])
                    process(card)
                if len(seen) >= limit or featured_only:
                    break
    except Exception as error:
        failures.append(f"목록 수집 실패 | {type(error).__name__}: {str(error).splitlines()[0][:200]}")
    finally:
        if browser:
            browser.__exit__()
        if catch_browser:
            catch_browser.__exit__()
    report = dict(site=name, pages=pages_read, postings_seen=sum(counts.values()),
                  counts=dict(counts), failures=failures, warnings=warnings, audit=audit,
                  coverage="explicit-recheck" if recheck_cards is not None else "featured-only" if featured_only else "configured-list",
                  status="failed" if failures else "partial" if warnings else "success")
    print(f"{name} | 목록 {pages_read}페이지 | 직무 {sum(counts.values())}건 | 관심 {len(postings)}건 | " + " | ".join(f"{k} {v}건" for k, v in counts.items()), flush=True)
    for failure in failures:
        print(f"{name} | 오류: {failure}", flush=True)
    for warning in warnings:
        print(f"{name} | 일부 수집: {warning}", flush=True)
    return postings, report
