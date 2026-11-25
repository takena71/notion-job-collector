"""관심 공고를 기존 Notion 수집함에 저장한다. 토큰은 환경변수에서만 읽는다."""

import hashlib
import argparse
import json
import os
import re
import unicodedata
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from job_identity import advert_key, company_name, company_key, is_summary, match_kind

from profile_config import PROFILE
DATA_SOURCE_ID = ""
FIELDS = {group["name"]: group["name"] for group in PROFILE["interests"]}
SCHEMA = {"직무명": "title", "회사": "rich_text", "공고 링크": "url",
          "관심 분야": "select", "마감일": "date", "지원 상태": "status", "출처": "rich_text"}


class NotionError(RuntimeError):
    pass


def canonical_url(url):
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("공고 링크는 http 또는 https 주소여야 합니다.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/",
                       urlencode(sorted(query)), ""))


def deadline_from_text(text):
    """연도가 명시된 마감일만 해석한다. 연도 없는 날짜/상시채용은 추정하지 않는다."""
    match = re.search(r"(?:마감(?:일)?|접수\s*마감)\s*[:：]?\s*(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return None


def make_posting(company, title, url, field, evidence, source, source_id="", deadline=None, company_size=""):
    if field not in FIELDS:
        raise ValueError("관심 공고만 저장할 수 있습니다.")
    if not all(isinstance(v, str) and v.strip() for v in (company, title, evidence, source)):
        raise ValueError("회사·직무명·판단 근거·출처가 필요합니다.")
    url = canonical_url(url)
    identity = str(source_id) or url
    if not identity:
        raise ValueError("공고 ID나 개별 공고 링크가 필요합니다.")
    if deadline:
        date.fromisoformat(deadline)
    # 내부 감사 키다. 실제 중복 판정은 원문 ID·확인된 모집·직무로 별도 수행한다.
    basis = [source, str(source_id)] if source_id else [source, identity, company.strip(), title.strip()]
    key = hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()
    return dict(company=company_name(company), title=title.strip(), url=url, field=field,
                evidence=evidence.strip(), source=source.strip(), key=key, deadline=deadline, source_id=str(source_id), company_size=company_size)


def rich_text(value):
    # Notion의 텍스트 조각당 2,000자 제한을 지키며 조용히 잘라내지 않는다.
    if len(value) > 100000:
        raise ValueError("저장할 텍스트가 너무 깁니다.")
    return [{"type": "text", "text": {"content": value[i:i + 2000]}}
            for i in range(0, len(value), 2000)]


def normalized_title(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def property_text(properties, name):
    prop = properties.get(name, {})
    kind = prop.get("type") or ("title" if "title" in prop else "rich_text")
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in prop.get(kind, []))


def source_references(properties):
    refs = []
    for item in properties.get("출처", {}).get("rich_text", []):
        value = item.get("plain_text", item.get("text", {}).get("content", ""))
        labels = [line.strip() for line in value.splitlines() if line.strip()]
        url = item.get("href") or (item.get("text", {}).get("link") or {}).get("url") or ""
        for label in labels:
            refs.append((label, url if len(labels) == 1 else ""))
    primary = properties.get("공고 링크", {}).get("url") or ""
    if len(refs) == 1 and not refs[0][1] and primary:
        refs[0] = (refs[0][0], primary)
    return refs


def identity_record(posting):
    return dict(company=posting["company"], title=posting["title"],
                urls=[posting["url"]] if posting.get("url") else [],
                deadline=posting.get("deadline"), sources=[posting["source"]],
                origin_ids=[(posting["source"], str(posting["source_id"]))] if posting.get("source_id") else [])


def row_identity(row):
    props = row["properties"]
    refs = source_references(props)
    urls = [props.get("공고 링크", {}).get("url") or "", *[url for _, url in refs]]
    valid = []
    for url in urls:
        if url:
            try:
                valid.append(canonical_url(url))
            except ValueError:
                pass
    origins, source_names = [], []
    for label, _ in refs:
        match = re.fullmatch(r"(.+) · 공고 (.+)", label)
        source_names.append(match[1] if match else label)
        if match:
            origins.append((match[1], match[2]))
    return dict(company=property_text(props, "회사"), title=property_text(props, "직무명"),
                urls=valid, deadline=(props.get("마감일", {}).get("date") or {}).get("start"),
                sources=source_names, origin_ids=origins)


def properties_for(posting, existing=None):
    props = existing.get("properties", {}) if existing else {}
    refs = source_references(props)
    label = posting["source"]
    if not posting["url"] and posting.get("source_id"):
        label += " · 공고 " + str(posting["source_id"])
    incoming = (label, posting["url"])
    if not any(name == incoming[0] and (advert_key(url) == advert_key(incoming[1]) or not url)
               for name, url in refs):
        refs.append(incoming)
    elif incoming[1]:
        refs = [(name, incoming[1] if name == incoming[0] and not url else url) for name, url in refs]
    source_text = []
    for index, (label, url) in enumerate(refs):
        if index:
            source_text.extend(rich_text("\n"))
        pieces = rich_text(label)
        if url and urlsplit(url).scheme in {"http", "https"}:
            for piece in pieces:
                piece["text"]["link"] = {"url": url}
        source_text.extend(pieces)
    result = {
        "직무명": {"title": rich_text(posting["title"])},
        "회사": {"rich_text": rich_text(property_text(props, "회사") or posting["company"])},
        "관심 분야": {"select": {"name": FIELDS[posting["field"]]}},
        "출처": {"rich_text": source_text},
    }
    if posting.get("company_size"):
        result["회사 규모"] = {"rich_text": rich_text(posting["company_size"])}
    elif not existing:
        result["회사 규모"] = {"rich_text": rich_text("미확인")}
    # 첫 공고 링크를 유지하고 추가 출처는 클릭 가능한 출처 항목에 보관한다.
    if posting["url"] and not props.get("공고 링크", {}).get("url"):
        result["공고 링크"] = {"url": posting["url"]}
    if posting.get("deadline"):
        result["마감일"] = {"date": {"start": posting["deadline"]}}
    if not existing:
        result["지원 상태"] = {"status": {"name": "검토 전"}}
    return result


class NotionClient:
    def __init__(self, token=None, data_source_id=None):
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        self.token = token or os.environ.get("NOTION_TOKEN", "")
        if not self.token:
            raise NotionError("NOTION_TOKEN 환경변수가 없습니다. README의 최초 연결 안내를 확인하세요.")
        self.data_source_id = data_source_id or os.environ.get("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)
        if not self.data_source_id and os.environ.get("NOTION_DATABASE_ID"):
            db = self.request("GET", "databases/" + os.environ["NOTION_DATABASE_ID"])
            sources = db.get("data_sources", [])
            if len(sources) != 1:
                raise NotionError("데이터 소스가 여러 개입니다. NOTION_DATA_SOURCE_ID를 직접 설정하세요.")
            self.data_source_id = sources[0]["id"]
        if not self.data_source_id:
            raise NotionError("친구 본인의 NOTION_DATA_SOURCE_ID를 .env 또는 GitHub Secret에 설정하세요.")
        self._writes = {}
        self._uncertain_creates = []
        self._batch_rows = None
        self._batch_mode = False

    def request(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        for attempt in range(4):
            request = Request("https://api.notion.com/v1/" + path, data=data, method=method,
                              headers={"Authorization": "Bearer " + self.token,
                                       "Notion-Version": "2026-03-11",
                                       "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code == 429 and attempt < 3:
                    try:
                        delay = float(error.headers.get("Retry-After", "1"))
                    except ValueError:
                        delay = 1
                    time.sleep(max(1, min(delay, 60)))
                    continue
                hints = {401: "토큰을 확인하세요.", 403: "연결의 읽기·삽입·수정 권한을 확인하세요.",
                         404: "수집함에 연결을 추가하고 데이터 소스 ID를 확인하세요."}
                # 생성 요청은 서버 반영 여부가 불명확할 수 있어 자동 재시도하지 않는다.
                raise NotionError(f"Notion HTTP {error.code}. " + hints.get(error.code,
                                  "저장 중단. 다시 실행하면 공고 링크·회사·직무명으로 기존 행을 먼저 확인합니다.")) from None
            except (URLError, OSError, ValueError):
                raise NotionError("Notion 응답 확인 실패. 저장이 반영됐을 수 있으므로 재실행 시 기존 행을 먼저 조회합니다.") from None

    def validate_schema(self):
        schema = self.request("GET", "data_sources/" + self.data_source_id)["properties"]
        if "회사 규모" not in schema:
            self.request("PATCH", "data_sources/" + self.data_source_id,
                         {"properties": {"회사 규모": {"rich_text": {}}}})
        elif schema["회사 규모"].get("type") not in {"rich_text", "select"}:
            raise NotionError("회사 규모 속성은 텍스트 또는 선택 유형이어야 합니다.")
        self.company_size_type = schema.get("회사 규모", {}).get("type", "rich_text")
        invalid = [name for name, kind in SCHEMA.items() if schema.get(name, {}).get("type") not in ({"status", "select"} if name == "지원 상태" else {kind})]
        options = {o["name"] for o in schema.get("관심 분야", {}).get("select", {}).get("options", [])}
        if "회사 규모" not in schema:
            self.request("PATCH", "data_sources/" + self.data_source_id,
                         {"properties": {"회사 규모": {"rich_text": {}}}})
        elif schema["회사 규모"].get("type") not in {"rich_text", "select"}:
            invalid.append("회사 규모")
        self.status_type = schema.get("지원 상태", {}).get("type", "status")
        statuses = {o["name"] for o in schema.get("지원 상태", {}).get(self.status_type, {}).get("options", [])}
        if invalid or not set(FIELDS.values()) <= options or "검토 전" not in statuses:
            raise NotionError("수집함 열/옵션이 예상과 다릅니다: " + ", ".join(invalid))

    @contextmanager
    def batch(self):
        self._batch_mode, self._batch_rows = True, None
        try:
            yield
        finally:
            self._batch_mode, self._batch_rows = False, None

    def read_rows(self):
        if self._batch_mode and self._batch_rows is not None:
            return self._batch_rows
        rows, cursor = [], None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            page = self.request("POST", f"data_sources/{self.data_source_id}/query", payload)
            rows.extend(page["results"])
            if not page.get("has_more"):
                break
            cursor = page["next_cursor"]
        by_id = {row["id"]: row for row in rows}
        for page_id, row in self._writes.items():
            old = by_id.get(page_id)
            if old is None or row.get("last_edited_time", "") > old.get("last_edited_time", ""):
                by_id[page_id] = row
        rows = list(by_id.values())
        if self._batch_mode:
            self._batch_rows = rows
        return rows

    def remember_write(self, page):
        self._writes[page["id"]] = page
        if self._batch_rows is not None:
            self._batch_rows = [row for row in self._batch_rows if row["id"] != page["id"]] + [page]

    def find_existing(self, posting):
        incoming = identity_record(posting)
        rows = self.read_rows()
        matches = [row for row in rows
                   if match_kind(incoming, row_identity(row)) in {
                       "same-ad-role", "verified-campaign-role", "same-origin-role"}]
        if len(matches) > 1:
            raise NotionError("동일 공고의 기존 행이 여러 개입니다. 새 행은 만들지 않습니다. 정리 대상을 먼저 확인하세요.")
        if not matches:
            from job_identity import company_key
            for row in rows:
                old = row_identity(row)
                if (company_key(incoming["company"]) == company_key(old["company"])
                    and normalized_title(incoming["title"]) == normalized_title(old["title"])
                    and (not incoming["urls"] or not old["urls"])
                    and set(incoming["sources"]).intersection(old["sources"])
                    and not old.get("origin_ids")):
                    raise NotionError("링크·원문 공고 번호가 없는 기존 행은 모집 차수를 확인한 뒤 연결해야 합니다. 새 행은 만들지 않습니다.")
        return matches[0] if matches else None

    def upsert(self, posting):
        if posting.get("field") not in FIELDS or not re.fullmatch(r"[0-9a-f]{64}", posting.get("key", "")):
            raise ValueError("저장할 공고의 관심 분야 또는 수집 키가 유효하지 않습니다.")
        if is_summary(identity_record(posting)):
            return "요약 제외", {"url": posting["url"]}
        existing = self.find_existing(posting)
        payload = {"properties": properties_for(posting, existing)}
        if "회사 규모" in payload["properties"] and getattr(self, "company_size_type", "rich_text") == "select":
            payload["properties"]["회사 규모"] = {"select": {"name": posting.get("company_size") or "미확인"}}
        if not existing:
            payload["properties"]["지원 상태"] = {getattr(self, "status_type", "status"): {"name": "검토 전"}}
        if existing:
            page = self.request("PATCH", "pages/" + existing["id"], payload)
            self.remember_write(page)
            return "갱신", page
        # 지원 완료 기록이 있는 회사의 새 직무는 다시 추가하지 않는다.
        # 같은 직무의 기존 행은 위에서 상태·본문을 보존하며 갱신한다.
        applied = next((row for row in self.read_rows()
                        if company_key(property_text(row["properties"], "회사")) == company_key(posting["company"])
                        and ((row["properties"].get("지원 상태", {}).get("status") or row["properties"].get("지원 상태", {}).get("select") or {}).get("name", "").replace(" ", "") == "지원완료")), None)
        if applied:
            return "지원 회사 제외", {"url": applied.get("url", "")}
        incoming = identity_record(posting)
        if any(match_kind(incoming, prior) in {"same-ad-role", "verified-campaign-role", "same-origin-role"}
               for prior in self._uncertain_creates):
            raise NotionError("앞선 동일 공고 생성의 반영 여부가 불명확합니다. 중복 방지를 위해 새 생성은 보류합니다.")
        payload["parent"] = {"type": "data_source_id", "data_source_id": self.data_source_id}
        try:
            page = self.request("POST", "pages", payload)
        except NotionError:
            self._uncertain_creates.append(incoming)
            raise
        self.remember_write(page)
        return "신규", page


def save_postings(postings, client=None):
    if not postings:
        print("Notion 저장 대상 0건 | 신규 0건 | 갱신 0건")
        return
    client = client or NotionClient()
    client.validate_schema()
    counts = {"신규": 0, "갱신": 0, "요약 제외": 0, "지원 회사 제외": 0}
    with client.batch():
        for posting in postings:
            action, page = client.upsert(posting)
            counts[action] += 1
            print(f"Notion {action} | {posting['company']} | {posting['title']} | {page.get('url', '')}")
    print(f"Notion 저장 완료 | 신규 {counts['신규']}건 | 갱신 {counts['갱신']}건 | 요약 제외 {counts['요약 제외']}건 | 지원 회사 제외 {counts['지원 회사 제외']}건")


def main():
    parser = argparse.ArgumentParser(description="보관한 관심 공고 JSON을 Notion 수집함에 저장/재시도")
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        postings = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(postings, list):
            raise ValueError("JSON 최상위 값은 공고 목록이어야 합니다.")
        for posting in postings:
            if not isinstance(posting, dict):
                raise ValueError("각 공고는 JSON 객체여야 합니다.")
            make_posting(posting["company"], posting["title"], posting["url"], posting["field"],
                         posting["evidence"], posting["source"], posting.get("source_id", posting["key"]), posting.get("deadline"))
            if not re.fullmatch(r"[0-9a-f]{64}", posting["key"]):
                raise ValueError("수집 키 형식이 잘못됐습니다.")
        save_postings(postings)
    except (OSError, ValueError, KeyError, TypeError, NotionError) as error:
        parser.exit(1, f"저장 중단: {error}\n")


if __name__ == "__main__":
    main()
