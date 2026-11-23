"""원본의 반도체·AX 업무 분류 기준. 개인 정보 없음."""
import re

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

def classify_role(title, duties, industry=""):
    examples = {}
    outside = unclear = None
    if not duties:
        return "직무 제외", "담당업무 본문을 확인할 수 없음"
    for line in [*duties, title]:
        compact = "".join(line.split())
        semi = SEMICONDUCTOR.search(compact) and TECHNICAL_WORK.search(compact)
        ax = AI_TECH.search(compact) and AI_WORK.search(compact)
        if semi and ax:
            return "반도체×AX", line
        if semi:
            examples.setdefault("반도체", line)
        if ax:
            examples.setdefault("AX", line)
        if not outside and OUTSIDE_WORK.search(compact):
            outside = line
        if not unclear and AMBIGUOUS.search(compact):
            unclear = line
    # AX 업무는 기업 업종과 별도로 선택한다. 교집합은 같은 업무에 두 근거가 있을 때만 선택한다.
    if "AX" in examples:
        return "AX", examples["AX"]
    if examples and outside:
        return "직무 제외", "업무 근거 혼재 | " + " / ".join(dict.fromkeys([examples["반도체"], outside]))
    if examples:
        return next(iter(examples.items()))
    if outside:
        return "직무 제외", outside
    research = next((line for line in [*duties, title] if RESEARCH_WORK.search(line)), None)
    if research and industry:
        sector = "".join(industry.split())
        evidence = f"업종: {industry} | {research}"
        if SEMICONDUCTOR.search(sector) and not OTHER_INDUSTRY.search(sector):
            return "반도체", evidence
        if OTHER_INDUSTRY.search(sector) and not SEMICONDUCTOR.search(sector):
            return "직무 제외", evidence
    return "직무 제외", "관심 분야 근거 부족 | " + (unclear or duties[0])

