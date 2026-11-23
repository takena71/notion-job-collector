"""공유 가능한 직무 설정. 이 파일에는 토큰이나 개인 지원 기록을 넣지 않는다."""
import json
import re
from pathlib import Path


def validate_profile(profile):
    mode = profile.get("classification_mode", "keywords")
    if mode not in {"keywords", "semiconductor_ax"}:
        raise ValueError("classification_mode는 keywords 또는 semiconductor_ax입니다.")
    groups = profile.get("interests")
    if not isinstance(groups, list) or not groups:
        raise ValueError("profile.json에 interests를 1개 이상 설정하세요.")
    if mode == "semiconductor_ax" and {g.get("name") for g in groups if isinstance(g, dict)} != {"반도체", "AX", "반도체×AX"}:
        raise ValueError("기본 모드의 관심 분야는 반도체·AX·반도체×AX이어야 합니다.")
    names = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("관심 분야는 객체여야 합니다.")
        name = group.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("관심 분야 name은 비어 있지 않은 고유한 문자열이어야 합니다.")
        names.add(name)
        for key in ("title_keywords", "duty_all_keywords"):
            values = group.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(key + "에는 비어 있지 않은 문자열 배열을 넣으세요.")
        if mode == "keywords" and not group.get("title_keywords") and not group.get("duty_all_keywords"):
            raise ValueError(name + "에 직무 키워드를 설정하세요.")
    if not isinstance(profile.get("exclude_graduate_degree_mentions", False), bool):
        raise ValueError("exclude_graduate_degree_mentions는 true/false여야 합니다.")
    return {**profile, "exclude_graduate_degree_mentions": profile.get("exclude_graduate_degree_mentions", False)}


PROFILE = validate_profile(json.loads(Path(__file__).with_name("profile.json").read_text(encoding="utf-8")))


def normalize(value):
    return "".join(value.casefold().split())


def classify_interest(title, duties, profile=None, industry=""):
    profile = profile or PROFILE
    if profile.get("classification_mode") == "semiconductor_ax":
        from role_rules import classify_role
        return classify_role(title, duties, industry)
    if not duties:
        return "직무 제외", "담당업무 본문을 확인할 수 없음"
    # 지원자격·우대사항의 도구명만으로 직무를 선택하지 않는다.
    title_text = normalize(title)
    duty_text = normalize("\n".join(duties))
    def title_matches(keyword):
        if keyword.isascii():
            return re.search(r"(?<![A-Za-z])" + re.escape(keyword) + r"(?![A-Za-z])", title, re.I)
        return normalize(keyword) in title_text

    if match := max(((len(normalize(keyword)), group["name"])
                     for group in profile["interests"] for keyword in group.get("title_keywords", [])
                     if title_matches(keyword)), default=None):
        return match[1], "직무명 일치: " + title
    # 회계의 매출 집계·거래처 정산은 영업 담당업무와 구분한다.
    unrelated_role = re.search(
        r"회계|세무|재무|자금|금융|경리|인사|총무|법무|개발자|디자이너|건축|시공|간호|상담원",
        title,
    )
    if unrelated_role:
        return "직무 제외", "다른 직무가 명시됨: " + unrelated_role.group()
    for group in profile["interests"]:
        keywords = group.get("duty_all_keywords", [])
        if keywords and all(normalize(k) in duty_text for k in keywords):
            return group["name"], "담당업무 일치: " + " / ".join(duties)[:1500]
    return "직무 제외", "설정한 희망 직무 근거 부족"
