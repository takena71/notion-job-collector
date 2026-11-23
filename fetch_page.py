"""설정한 희망 직무의 신입·경력·인턴 공고를 찾아 저장한다."""

import argparse
import json
import re
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import urlopen

from notion_sync import NotionClient, NotionError, deadline_from_text, make_posting, save_postings


SITES = {}  # 수집 사이트는 사용자 제공 후 등록합니다.


def check_sites():
    for name, url in SITES.items():
        try:
            with urlopen(url, timeout=15) as response:
                size = len(response.read())
                print(f"{name} | HTTP {response.status} | {size:,}바이트")
        except (URLError, OSError, HTTPException) as error:
            print(f"{name} | 접속 실패 | {error}")


def read_rndjob_page(url):
    from bs4 import BeautifulSoup

    with urlopen(url, timeout=15) as response:
        content = response.read()
        charset = response.headers.get_content_charset()
    return BeautifulSoup(content, "html.parser", from_encoding=charset)


DEGREE_REQUIREMENT = re.compile(
    r"석사|박사|석[·ㆍ/･・-]?박(?:통합|사)?|대학원졸|대학원석|대학원박"
    r"|(?<![a-z])(?:ph\.?\s*d\.?|master['’]s|masters?\s+degree|doctorate|doctoral)(?![a-z])", re.I)
CAREER_REQUIREMENT = re.compile(
    r"경력(?:직|사원|자|채용|모집|필수|요건|조건|보유|필요|우대|요구|이있|을보유|을갖)|경력[^.。;\n]{0,12}\d+\s*(?:년|개월)"
    r"|\d+\s*년[^.。;\n]{0,8}경력|(?:^|[\s/|:：,·(\[])경력(?:$|[\s/|:：,·)\]])|경력$")


def career_text(text):
    compact = re.sub(r"(?<=[가-힣·ㆍ/･・-])\s+(?=[가-힣·ㆍ/･・-])", "", text)
    # 항목명·서류·수상·운전·경력 개발은 근무 경력 요건이 아니다.
    compact = re.sub(r"(?:신입\s*/\s*)?경력(?:구분)?\s*[:：]\s*(?=신입|무관)", "", compact)
    compact = re.sub(r"경력\s*무관(?:\s*\(신입포함\))?|(?<![가-힣])무경력", "신입", compact)
    compact = re.sub(r"(?:수상|수료|운전)경력|경력(?:증명서|기술서|개발|개발기회)", "", compact)
    compact = re.sub(r"신입(?:사원|직)?\s*(?:[/·ㆍ･・,&]|및|또는|과|혹은)+\s*경력(?:사원|직)?", "신입", compact)
    compact = re.sub(r"경력(?:사원|직)?\s*(?:[/·ㆍ･・,&]|및|또는|과|혹은)+\s*신입(?:사원|직)?", "신입", compact)
    return compact


def excluded_qualification(text):
    # 학위 기준은 유지하고, 경력은 실제 근무 경력 조건만 확인한다.
    compact = career_text(text)
    return (DEGREE_REQUIREMENT.search(compact) if PROFILE["exclude_graduate_degree_mentions"] else None) or (None if PROFILE.get("allow_experienced", False) else CAREER_REQUIREMENT.search(compact))


# 직무 분류에는 모집 제목과 담당업무를 사용한다. 우대사항의 도구명은 근거로 쓰지 않는다.
SEMICONDUCTOR = re.compile(
    r"반도체|웨이퍼|포토리소그래피|(?:soc|sip)[/a-z]*패키징|semiconductor|wafer|(?<![a-z])(?:dram|nand|hbm|finfet)(?![a-z])", re.I
)
TECHNICAL_WORK = re.compile(
    r"연구|설계|개발|공정|제조|소자|장비|패키징|계측|분석|평가|테스트|엔지니어|검사|정비|"
    r"셋업|셋팅|세팅|유지보수|수율|품질|식각|증착|기술지원|불량|예측|이상탐지|engineer|process", re.I
)
AI_TECH = re.compile(
    r"(?<![a-z])(?:rag|llm|mlops|ai|ax|ml)(?![a-z])|생성형ai|인공지능|머신러닝|딥러닝|"
    r"컴퓨터비전|비전모델|이상탐지모델|예측모델|분류모델", re.I
)
AI_WORK = re.compile(
    r"모델(?:개발|학습|운영|구축|배포)|구축|도입|구현|학습|파인튜닝|배포|업무자동화|전략수립|답하도록(?:만들|구현|개선)|"
    r"보안(?:정책운영|점검|위험.{0,15}(?:검토|통제|기준))|"
    r"(?:기반|활용|이용|통한|으로).{0,28}(?:자동화|최적화|예측|추천)|"
    r"(?:검색|예측|분류|탐지|분석|추천|에이전트|챗봇|서비스|솔루션|시스템|모델|파이프라인).{0,15}(?:개발|운영)|"
    r"(?:rag|llm|mlops|에이전트|챗봇)(?:기반)?(?:개발|운영)", re.I
)
OUTSIDE_WORK = re.compile(
    r"(?:기어|감속기|차체|동력전달장치|액추에이터기구).{0,20}(?:설계|개발)|"
    r"(?:공동주택|토목공사|건축현장).{0,12}(?:시공|감리|품질관리)|"
    r"(?:문구용?잉크|필기구용?잉크).{0,24}(?:연구|개발|제조|생산)|"
    r"(?:cnc선반조작|밀링가공|용접[·및]?제관|보험영업|매장판매|급여.{0,3}근태관리|전표입력)", re.I
)
AMBIGUOUS = re.compile(r"자동화|품질검사|소재|연구보조|연구개발|데이터분석|python|로봇", re.I)
RESEARCH_WORK = re.compile(r"연구|개발|r\s*&\s*d", re.I)
# 포괄적인 '제조업', '화학'에는 반도체 소재·장비 업체도 포함될 수 있다.
OTHER_INDUSTRY = re.compile(r"문구|필기구|완구|의류|봉제|음료|식품|화장품|가구제조")
from profile_config import PROFILE, classify_interest
INTERESTS = tuple(group["name"] for group in PROFILE["interests"])


def classify_role(title, duties, industry=""):
    return classify_interest(title, duties, industry=industry)


def classify_detail(detail, title, base_url="https://www.rndjob.or.kr"):
    from ocr import enrich_html, OCRError
    from bs4 import BeautifulSoup
    try:
        for body in list(detail.select(".basic_tab_con .vin_dtl")):
            body.replace_with(BeautifulSoup(enrich_html(str(body), base_url), "html.parser"))
    except OCRError as error:
        return "수집 제외", str(error)
    # '경력'은 신입 공고에도 있는 양식 항목명이다. 지원자격에서는 입력값만 검사한다.
    for block in detail.select(".notice_info_box .con"):
        label = block.select_one(".tit")
        if label and label.get_text(strip=True) == "지원자격":
            for item in block.select("dd"):
                value = item.get_text(" ", strip=True)
                if excluded_qualification(value):
                    return "자격 제외", value
    duties = []
    for section in detail.select(".basic_tab_con .sub_each2"):
        heading = section.select_one(".sub_tit2")
        body = section.select_one(".vin_dtl")
        if not heading or not body:
            continue
        name = heading.get_text(strip=True)
        lines = body.get_text("\n", strip=True).splitlines()
        if excluded_qualification(name + " " + " ".join(lines)):
            return "자격 제외", next((line for line in lines if excluded_qualification(line)), name)
        if name in {"담당업무", "주요업무", "직무내용", "직무소개"}:
            duties.extend(line.strip() for line in lines if line.strip())
    industry = ""
    for label in detail.select("dt, th"):
        if "".join(label.stripped_strings).replace(" ", "") in {"산업(업종)", "산업", "업종"}:
            value = label.find_next_sibling(["dd", "td"])
            if value:
                industry = value.get_text(" ", strip=True)
                break
    return classify_role(title, duties, industry)


def classify_card(card, listing_url):
    company = card.select_one(".brand-logo")
    link = card.select_one(".card-title a[href]")
    company_name = company.get_text(" ", strip=True) if company else "회사명 미확인"
    title = link.get_text(" ", strip=True) if link else "제목 미확인"
    url = urljoin(listing_url, link["href"]) if link else ""
    labels = [tag.get_text(" ", strip=True) for tag in card.select(".card-tag")]
    career_tag = card.select_one(".tag--dark")
    career = career_tag.get_text(strip=True) if career_tag else ""
    for value in [title, career, *labels]:
        if excluded_qualification(value):
            return company_name, title, url, "자격 제외", value
    if "신입" not in career and career != "무관":
        return company_name, title, url, "자격 제외", f"신입 지원 근거 부족 | {career or '표시 없음'}"
    parsed = urlsplit(url)
    if parsed.hostname != urlsplit(listing_url).hostname or parsed.scheme != "https":
        return company_name, title, "", "수집 제외", "유효한 상세 링크 없음"
    try:
        result, evidence = classify_detail(read_rndjob_page(url), title, url)
    except (URLError, OSError, HTTPException) as error:
        result, evidence = "수집 제외", f"상세 페이지 접속 실패 | {error}"
    return company_name, title, url, result, evidence


def show_rndjob(max_pages=5):
    listing_url = SITES["R&D잡"]
    counts = Counter()
    seen = set()
    pages_read = 0
    selected_postings = []
    for page in range(1, max_pages + 1):
        try:
            page_url = listing_url if page == 1 else (
                f"{listing_url}?agecd=&areacd=&bizcd=&careercd=&catecd=&cur_pack=0"
                f"&empcd=&gcnt=&gtxt=&gtype=&page={page}&schoolcd=&ser_type=&sfield=&srno=&ste="
            )
            listing = read_rndjob_page(page_url)
        except (URLError, OSError, HTTPException) as error:
            print(f"{page}페이지 목록 접속 실패 | {error}")
            continue
        cards = listing.select(".job-card")
        if not cards:
            print(f"{page}페이지에서 공고를 읽지 못했습니다.")
            continue
        pages_read += 1
        for card in cards:
            link = card.select_one(".card-title a[href]")
            if link:
                parts = urlsplit(urljoin(listing_url, link["href"]))
                job_id = parse_qs(parts.query).get("gno", [""])[0]
                if job_id:
                    key = (parts.path, job_id)
                    if key in seen:
                        continue
                    seen.add(key)
            company, title, url, result, evidence = classify_card(card, listing_url)
            counts[result] += 1
            if result not in INTERESTS:
                continue
            print(f"{company} | {result} | {title}")
            print(f"근거: {evidence}")
            print(url)
            print()
            source_id = parse_qs(urlsplit(url).query).get("gno", [""])[0]
            selected_postings.append(make_posting(
                company, title, url, result, evidence, "R&D잡", source_id,
                deadline_from_text(card.get_text(" ", strip=True))))
    total = sum(counts.values())
    if not pages_read:
        raise SystemExit("목록을 수집하지 못해 관심 공고 수를 확인할 수 없습니다.")
    selected = sum(counts[key] for key in INTERESTS)
    print(f"목록 {pages_read}/{max_pages}페이지 | 공고 {total}건 | 관심 공고 {selected}건")
    print(" | ".join(f"{key} {counts[key]}건" for key in (*INTERESTS, "직무 제외", "자격 제외", "수집 제외")))
    return selected_postings


LOOFIT_DUTY = re.compile(r"담당\s*업무|주요\s*업무|수행\s*업무|직무\s*(?:내용|소개)|업무\s*(?:내용|소개)|주요\s*역할")
LOOFIT_STOP = re.compile(r"필요\s*역량|필수\s*요건|지원\s*자격|응시\s*자격|자격\s*(?:요건|사항)|우대\s*(?:사항|조건)|근무\s*(?:조건|시간)|전형\s*(?:절차|일정)|기타사항|모집분야")


def classify_loofit_jd(title, html, industry):
    from bs4 import BeautifulSoup

    body = BeautifulSoup(html, "html.parser")
    for br in body.select("br"):
        br.replace_with("\n")
    # 표 안의 항목명과 값도 줄 단위로 읽는다.
    for tag in body.select("p, li, div, h1, h2, h3, h4, h5, h6, td, th"):
        tag.append("\n")
    lines = [line.strip() for line in body.get_text().splitlines() if line.strip()]
    duties = []
    recording = False
    for index, line in enumerate(lines):
        if line.replace(" ", "").rstrip(":：") in {"경력", "신입/경력", "경력구분"} and index + 1 < len(lines) and lines[index + 1].replace(" ", "") in {"신입", "무관", "신입/경력"}:
            continue
        # 양식의 '신입/경력: 신입'에서 실제 자격 값은 '신입'이다.
        value = re.sub(r"^\s*(?:신입\s*/\s*)?경력\s*[:：]\s*", "", line)
        if line not in {"신입/경력", "경력구분"} and excluded_qualification(value):
            return "자격 제외", line
        heading = LOOFIT_DUTY.search(line)
        if heading and heading.start() < 8:
            recording = True
            rest = line[heading.end():].lstrip(" ]】:：-")
            if rest and rest not in {"및 역할", "및 책임"}:
                duties.append(rest)
        elif LOOFIT_STOP.search(line[:15]):
            recording = False
        elif recording:
            duties.append(line)
    if not any(line != "상세요강" for line in lines):
        return "수집 제외", "텍스트 상세요강 없음"
    return classify_role(title, duties, industry)


def read_loofit_card(page, card, title, description):
    from playwright.sync_api import Error as BrowserError

    for value in (title, description):
        if excluded_qualification(value):
            return "자격 제외", value, ""
    # '학력무관'이나 근무지역의 '무관'을 채용형태로 오인하지 않는다.
    parts = re.split(r"\s*/\s*", description)
    if len(parts) < 4 or not (any("신입" in part for part in parts[2:-1]) or parts[-2] == "무관"):
        return "자격 제외", "신입 지원 근거 부족", ""
    modal = page.locator("#detail_modal:visible")
    try:
        card.get_by_role("button", name="자세히 보기").click()
        jd = modal.locator("#ds1 .ds_titbox").first
        jd.wait_for(state="visible")
        industry = modal.locator(".item").filter(
            has=page.locator("p.tit", has_text=re.compile(r"^\s*업종\s*$"))
        ).locator(".desc")
        sector = industry.first.text_content().strip() if industry.count() else ""
        from ocr import enrich_html, OCRError
        try:
            html = enrich_html(jd.inner_html(), page.url)
        except OCRError as error:
            return "수집 제외", str(error), ""
        result, evidence = classify_loofit_jd(title, html, sector)
        apply_link = modal.locator("a.apply_btn[href]")
        href = apply_link.first.get_attribute("href") if apply_link.count() else ""
        url = urljoin(page.url, href) if href else ""
        return result, evidence, url if urlsplit(url).scheme in {"http", "https"} else ""
    except BrowserError as error:
        return "수집 제외", f"상세창 읽기 실패 | {str(error).splitlines()[0]}", ""
    finally:
        if modal.count():
            modal.locator(".ed_btn_close:visible").first.click()
            modal.wait_for(state="hidden")


def show_loofit(max_pages=5):
    from playwright.sync_api import Error as BrowserError, sync_playwright

    counts = Counter()
    seen = set()
    pages_read = 0
    selected_postings = []
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except BrowserError as error:
                if "Executable doesn't exist" not in str(error):
                    raise
                browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page()
            page.set_default_timeout(12000)
            page.goto(SITES["SKALA 루핏"], wait_until="domcontentloaded")
            for number in range(1, max_pages + 1):
                if number > 1:
                    target = page.locator(f'.paging .page[data-page="{number}"]')
                    if not target.count():
                        break
                    target.locator("a").click()
                    page.locator(f'.paging .page.on[data-page="{number}"]').wait_for()
                cards = page.locator(".joblist > .item")
                cards.first.wait_for()
                pages_read += 1
                for card in cards.all():
                    bookmark = card.locator(".btnbox .bookmark_btn[id]")
                    key = bookmark.first.get_attribute("id") if bookmark.count() else None
                    if key in seen:
                        continue
                    if key:
                        seen.add(key)
                    title = card.locator(".txtbox .tit").inner_text().strip()
                    company = card.locator(".logo img").get_attribute("alt") or "회사명 미확인"
                    description = card.locator(".txtbox .desc").inner_text().strip()
                    result, evidence, url = read_loofit_card(page, card, title, description)
                    counts[result] += 1
                    if result in INTERESTS:
                        print(f"{company} | {result} | {title}")
                        print(f"근거: {evidence}")
                        print(url or f"루핏 목록에서 제목으로 검색: {SITES['SKALA 루핏']}")
                        print()
                        selected_postings.append(make_posting(
                            company, title, url, result, evidence, "SKALA 루핏", key or "",
                            deadline_from_text(card.inner_text())))
            browser.close()
    except BrowserError as error:
        raise SystemExit(f"루핏 수집 중단 | {str(error).splitlines()[0]}") from error
    print(f"루핏 목록 {pages_read}페이지 | 공고 {sum(counts.values())}건 | 관심 공고 {sum(counts[k] for k in INTERESTS)}건")
    print(" | ".join(f"{k} {counts[k]}건" for k in (*INTERESTS, "직무 제외", "자격 제외", "수집 제외")))
    return selected_postings


def open_postings(postings, today):
    """Keep postings with today's or a future deadline, plus undated postings."""
    return [posting for posting in postings
            if not posting.get("deadline") or posting["deadline"] >= today]


def main():
    from collectors import NEW_SITES, collect_site

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rndjob", action="store_true", help="R&D잡의 관심 공고와 근거 보기")
    parser.add_argument("--loofit", action="store_true", help="SKALA 루핏 전체 채용정보 분류")
    parser.add_argument("--notion", action="store_true", help="관심 공고를 Notion에 저장하거나 기존 행 갱신")
    parser.add_argument("--output", type=Path, help="관심 공고를 JSON 파일로 보관 (재실행/연결 도구용)")
    parser.add_argument("--dry-run", action="store_true", help="저장할 관심 공고를 JSON으로 미리 보기; Notion에 쓰지 않음")
    parser.add_argument("--all", action="store_true", help="등록된 수집 목록 전체 실행")
    parser.add_argument("--site", action="append", choices=list(NEW_SITES), default=[], help="추가 사이트 선택 (여러 번 지정 가능)")
    parser.add_argument("--limit", type=int, default=50, help="추가 사이트별 최대 공고 수 (기본 50, 다직무는 각각 분리)")
    parser.add_argument("--pages", type=int, default=5, help="페이지 탐색을 지원하는 목록의 최대 페이지 수 (기본 5)")
    parser.add_argument("--report", type=Path, default=Path("collection-report.json"), help="사이트별 실행 결과/제외 근거 JSON")
    args = parser.parse_args()
    if not SITES and not NEW_SITES:
        parser.error("등록된 수집 사이트가 없습니다. 제공받은 URL에 맞는 수집기를 등록한 뒤 실행하세요.")
    if (args.rndjob and "R&D잡" not in SITES) or (args.loofit and "SKALA 루핏" not in SITES):
        parser.error("요청한 사이트는 등록되어 있지 않습니다.")
    if not 1 <= args.limit <= 200:
        parser.error("--limit은 1~200이어야 합니다.")
    if not 1 <= args.pages <= 20:
        parser.error("--pages는 1~20이어야 합니다.")
    requested = args.all or args.rndjob or args.loofit or args.site
    if (args.notion or args.output or args.dry_run) and not requested:
        parser.error("저장 옵션에는 --all, --rndjob, --loofit 또는 --site를 지정하세요.")
    client = None
    if args.notion and not args.dry_run:
        try:
            client = NotionClient()
            client.validate_schema()
        except NotionError as error:
            parser.exit(1, f"{error}\n")
    if not requested:
        check_sites()
        return
    from ocr import engine, snapshot
    print(f"OCR 엔진: {engine()} (이미지 판독 시 로드)", flush=True)
    tasks = []
    if args.rndjob or (args.all and "R&D잡" in SITES):
        tasks.append(("R&D잡", lambda: show_rndjob(args.pages)))
    if args.loofit or (args.all and "SKALA 루핏" in SITES):
        tasks.append(("SKALA 루핏", lambda: show_loofit(args.pages)))
    keys = NEW_SITES if args.all else dict.fromkeys(args.site)
    for key in keys:
        tasks.append((NEW_SITES[key][0], lambda key=key: collect_site(key, args.limit, args.pages)))
    postings, reports = [], []
    output = args.output or (Path("postings.json") if args.notion else None)
    for name, collect in tasks:
        print(f"\n=== {name} 수집 시작 ===", flush=True)
        ocr_before = len(snapshot())
        try:
            value = collect()
            if isinstance(value, tuple):
                selected, report = value
            else:
                selected, report = value, dict(site=name, status="success", selected=len(value), failures=[])
        except (Exception, SystemExit) as error:
            selected = []
            report = dict(site=name, status="failed", failures=[str(error)])
            print(f"{name} | 수집 실패: {error}", flush=True)
        report["ocr"] = snapshot()[ocr_before:]
        print(f"{name} | OCR 이미지 {len(report['ocr'])}건", flush=True)
        today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        active = open_postings(selected, today)
        report["expired"] = len(selected) - len(active)
        report["selected"] = len(active)
        if report["expired"]:
            print(f"{name} | 마감일 경과 {report['expired']}건 제외", flush=True)
        selected = active
        postings.extend(selected)
        # 각 사이트 처리 후 후보와 보고서를 보관하고, 다른 사이트 오류와 독립적으로 저장한다.
        if output:
            output.write_text(json.dumps(postings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.notion and not args.dry_run:
            try:
                save_postings(selected, client)
            except (NotionError, ValueError) as error:
                report["status"] = "failed"
                report.setdefault("failures", []).append(str(error))
                print(f"{name} | Notion 저장 실패: {error}", flush=True)
        reports.append(report)
        args.report.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if output:
        print(f"관심 공고 {len(postings)}건 보관: {output}")
    if args.dry_run:
        print(json.dumps(postings, ensure_ascii=False, indent=2))
        print("미리 보기 완료 | Notion 쓰기 없음")
    failed = [report["site"] for report in reports if report["status"] == "failed"]
    partial = [report["site"] for report in reports if report["status"] == "partial"]
    print(f"전체 결과 | 사이트 {len(reports)}곳 | 오류 {len(failed)}곳 | 일부 수집 {len(partial)}곳 | 관심 직무 {len(postings)}건 | 보고서 {args.report}", flush=True)
    if failed:
        parser.exit(1, "일부 사이트 실패 (정상 사이트 결과는 보관/저장됨): " + ", ".join(failed) + "\n")


if __name__ == "__main__":
    main()
