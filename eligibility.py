"""공고에 명시된 근무지와 학력의 판정 근거를 남긴다."""
import re

from bs4 import BeautifulSoup

HEADINGS = re.compile(
    r"^\s*[\[【(•·-]*\s*(?:(?P<duties>담당\s*업무|주요\s*업무|수행\s*업무|직무\s*내용|업무\s*내용)"
    r"|(?P<required>지원\s*자격|응시\s*자격|공통\s*자격|필수\s*(?:자격|요건|사항)|자격\s*(?:요건|사항))"
    r"|(?P<preferred>우대\s*(?:사항|조건|요건))"
    r"|(?P<other>근무\s*조건|근무\s*시간|전형\s*절차|접수\s*기간|복리\s*후생|모집\s*분야|기타\s*사항))"
    r"\s*[\]】):：-]*\s*"
)
LOCATION = r"근무\s*(?:지역|장소|위치|지(?!원))|근무처|배치\s*지역"
REGIONS = re.compile(r"서울|경기(?!장)|인천|부산|대구|대전|울산|세종|광주광역시|광주(?=\s+[^ ]+구)|제주|강원|충[남북]|충청[남북]도|전[남북]|전라[남북]도|경[남북]|경상[남북]도")
GRADUATE = re.compile(r"석사|박사|대학원|master.s|ph\.?d", re.I)
BACHELOR = re.compile(r"학사|대졸|대학교\s*졸업|(?:4|４)\s*년제|bachelor", re.I)
LOWER = re.compile(r"학력\s*무관|학력\s*제한\s*없음|^무관$|고졸|고등학교\s*졸업|초대졸|전문대|(?:2|3)\s*년제")


def jd_sections(html):
    body = BeautifulSoup(html, "html.parser")
    for tag in body.select("script,style,nav,footer"):
        tag.decompose()
    for line_break in body.select("br"):
        line_break.replace_with("\n")
    for tag in body.select("p,li,div,h1,h2,h3,h4,h5,h6,td,th,dt,dd"):
        tag.append("\n")
    lines = [line.strip() for line in body.get_text().splitlines() if line.strip()]
    sections = {"duties": [], "required": [], "preferred": [], "other": []}
    current = "other"
    for line in lines:
        heading = HEADINGS.match(line)
        if heading:
            current = heading.lastgroup
            line = line[heading.end():]
            if current == "duties" and re.match(r"^(?:및|/|·)\s*(?:자격|요건|우대|지원)", line):
                current = "other"
        if re.match(r"^[•·\[ -]*(?:" + LOCATION + r"|학력)\s*(?:[:：\]]|$)", line):
            current = "other"
        if line:
            sections[current].append(line)
    return lines, sections


def labelled_values(lines, label):
    values = []
    for index, line in enumerate(lines):
        match = re.match(r"^[\[【•· -]*(?:" + label + r")\s*(?:[\]】:：-]\s*)?(.*)$", line)
        if not match:
            continue
        value = match[1].strip()
        if not value and index + 1 < len(lines):
            value = lines[index + 1]
            if HEADINGS.match(value) or re.match(r"학력|본사|기업\s*주소|회사\s*주소", value):
                continue
        if value:
            values.append(value)
    return values


def region_decision(values, targets):
    if not targets:
        return "통과", ""
    evidence = " / ".join(values)
    regions = {match.group() for match in REGIONS.finditer(evidence)}
    if not regions or re.search(r"전국|미정|협의|추후|별도|해외|또는", evidence):
        return "검토 필요", "근무지 확인 필요: " + (evidence or "표시 없음")
    if regions <= set(targets):
        return "통과", "근무지: " + evidence
    if regions.intersection(targets):
        return "검토 필요", "복수 근무지 배치 확인 필요: " + evidence
    return "자격 제외", "희망 지역 외 근무지: " + evidence


def education_decision(values, required):
    evidence = " / ".join(values + required)
    outcomes = set()
    for line, structured in [(line, True) for line in values] + [(line, False) for line in required]:
        if re.search(r"우대|선호", line):
            # 학사 이상(석사 우대)처럼 필수와 우대가 같은 줄에 있을 수 있다.
            required_part = re.split(r"\s*[(,;/]\s*|\s+(?=석사|박사|대학원)", line, maxsplit=1)[0]
            if required_part == line or re.search(r"우대|선호", required_part):
                continue
            line = required_part
        has_bachelor = BACHELOR.search(line)
        has_graduate = GRADUATE.search(line)
        if re.search(r"학사\s*(?:제외|불가)|(?:고졸|초대졸|전문대졸)\s*(?:이하|만\s*가능)", line):
            outcomes.add("자격 제외")
        elif has_graduate and has_bachelor:
            if re.search(r"부터|학사\s*이상|또는|학사\s*[/~·]", line):
                outcomes.add("통과")
            else:
                outcomes.add("검토 필요")
        elif has_graduate and (structured or re.search(r"필수|이상|졸업|소지|학위", line)):
            outcomes.add("자격 제외")
        elif has_bachelor or LOWER.search(line):
            outcomes.add("통과")
    if outcomes == {"통과"}:
        return "통과", evidence
    if outcomes == {"자격 제외"} or outcomes == {"통과", "자격 제외"}:
        return "자격 제외", "필수 학력 제한: " + evidence
    return "검토 필요", "필수 학력 확인 필요: " + (evidence or "표시 없음")


def check_eligibility(lines, sections, profile, *, location="", education=""):
    locations = labelled_values(lines, LOCATION) or ([location] if location else [])
    degrees = labelled_values(sections["other"], r"(?:최종\s*)?학력") or ([education] if education else [])
    region = region_decision(locations, profile.get("target_regions", []))
    degree = education_decision(degrees, sections["required"]) if profile.get("education") == "학사 지원 가능" else ("통과", "")
    for status in ("자격 제외", "검토 필요"):
        reasons = [evidence for result, evidence in (region, degree) if result == status]
        if reasons:
            return status, " | ".join(reasons)
    return None
