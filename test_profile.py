import unittest
from unittest.mock import patch
from profile_config import validate_profile, classify_interest
from notion_sync import make_posting, identity_record
from job_identity import match_kind


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile({"interests": [{"name": "기획", "title_keywords": ["서비스 기획"], "duty_all_keywords": ["요구사항", "기획서"]}]})

    def test_role_changes_without_source_edit(self):
        self.assertEqual(classify_interest("서비스 기획 인턴", ["서비스 개선"], self.profile)[0], "기획")
        self.assertEqual(classify_interest("개발자", ["API 개발"], self.profile)[0], "직무 제외")

    def test_all_duty_keywords_required(self):
        self.assertEqual(classify_interest("인턴", ["요구사항 분석", "기획서 작성"], self.profile)[0], "기획")
        self.assertEqual(classify_interest("인턴", ["기획서 검토"], self.profile)[0], "직무 제외")

    def test_no_missing_duty_or_empty_keyword_match(self):
        self.assertEqual(classify_interest("서비스 기획", [], self.profile)[0], "직무 제외")
        with self.assertRaises(ValueError):
            validate_profile({"interests": [{"name": "모두", "title_keywords": [""]}]})

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):
            validate_profile({"interests": self.profile["interests"] * 2})

    def test_collector_keeps_configured_non_engineering_field(self):
        from collectors import collect_site
        from profile_config import PROFILE
        field = PROFILE["interests"][0]["name"]
        card = {"id": "123", "company": "예시회사", "company_size": "대기업", "title": "신입", "career": "신입", "url": "https://example.org/jobs/123"}
        detail = ("예시회사", "채용", [("콘텐츠 마케팅", (field, "직무명 일치"))], None)
        with patch.dict("collectors.NEW_SITES", {"saramin-home": ("예시 목록", "https://example.org/jobs")}), patch("collectors.saramin_detail", return_value=detail):
            postings, report = collect_site("saramin-home", recheck_cards=[card])
        self.assertEqual(report["status"], "success")
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0]["field"], field)

    def test_duplicate_same_role_but_preserve_other_role(self):
        field = __import__('notion_sync').FIELDS.keys().__iter__().__next__()
        def p(title):
            return identity_record(make_posting("예시회사", title, "https://example.org/jobs/123", field, "담당업무 확인", "공식 채용"))
        self.assertTrue(match_kind(p("서비스 기획"), p("서비스 기획")))
        self.assertFalse(match_kind(p("서비스 기획"), p("콘텐츠 마케팅")))


class EligibilityTests(unittest.TestCase):
    def decision(self, html, title="온라인MD", **metadata):
        from collectors import classify_body
        return classify_body(title, html, **metadata)[0]

    def test_location_is_job_site_not_headquarters(self):
        self.assertEqual(self.decision("<p>본사 주소: 서울</p><p>근무지: 부산</p><p>담당업무: 상품 운영</p><p>학력: 대졸</p>"), "자격 제외")
        self.assertEqual(self.decision("<p>본사 주소: 서울</p><p>담당업무: 상품 운영</p><p>학력: 대졸</p>"), "검토 필요")

    def test_degree_requirements_and_preferences_are_separate(self):
        base = "<p>근무지: 경기 성남시</p><p>담당업무: 상품 소싱</p>"
        self.assertEqual(self.decision(base + "<p>자격요건: 학사 이상</p><p>우대사항: 석사</p>"), "온라인 MD")
        self.assertEqual(self.decision(base + "<p>자격요건: 학사 이상(석사 우대)</p>"), "온라인 MD")
        self.assertEqual(self.decision(base + "<p>자격요건: 석사 이상 필수</p>"), "자격 제외")
        self.assertEqual(self.decision(base + "<p>우대사항: 석사</p>"), "검토 필요")

    def test_preferred_keyword_alone_does_not_classify(self):
        html = "<p>담당업무: 배송</p><p>우대요건: 상품 소싱 및 온라인MD</p>"
        self.assertEqual(self.decision(html, "운송 담당", location="서울", education="무관"), "직무 제외")

    def test_planning_title_beats_general_sales_keyword(self):
        self.assertEqual(classify_interest("영업 기획 인턴", ["영업 실적 분석"])[0], "영업 기획")

    def test_uncertain_locations_are_reported_for_review(self):
        html = "<p>담당업무: 상품 소싱</p>"
        self.assertEqual(self.decision(html, location="서울, 부산", education="학력무관"), "검토 필요")
        self.assertEqual(self.decision(html, location="서울", education="대학원"), "자격 제외")

    def test_complete_saramin_text_does_not_depend_on_decorative_image(self):
        from collectors import egong_roles
        html = "<p>주요업무: SNS 콘텐츠 기획</p><p>자격요건: 영상 편집 가능</p><img src='banner.jpg'>"
        with patch("collectors.enrich_html", side_effect=AssertionError("장식 이미지는 OCR하지 않음")):
            result = egong_roles("콘텐츠 마케팅 인턴", html, "https://example.org", location="서울", education="학력무관")
        self.assertEqual(result[0][1][0], "콘텐츠 마케팅")

    def test_accounting_sales_terms_do_not_mean_sales_role(self):
        title = "제로투엔 재무본부 신입 및 경력 채용(회계·세무·자금·금융)"
        self.assertEqual(classify_interest(title, ["거래처 매출 집계 및 정산"])[0], "직무 제외")
        self.assertEqual(classify_interest("영업관리", ["거래처 매출 관리"])[0], "영업·영업관리")

    def test_company_size_uses_labelled_company_metadata(self):
        from bs4 import BeautifulSoup
        from collectors import company_size
        from notion_sync import properties_for, make_posting
        soup = BeautifulSoup("<p>대기업 고객사와 협업</p><dl><dt>기업형태</dt><dd>중소기업</dd></dl>", "html.parser")
        self.assertEqual(company_size(soup), "중소기업")
        self.assertEqual(company_size(BeautifulSoup("<p>대기업 고객사</p>", "html.parser")), "")
        posting = make_posting("예시", "영업", "https://example.org/1", "영업·영업관리", "업무", "사람인")
        self.assertEqual(properties_for(posting)["회사 규모"]["rich_text"][0]["text"]["content"], "미확인")
        self.assertNotIn("회사 규모", properties_for(posting, {"properties": {}}))

    def test_saramin_regular_results_only(self):
        from bs4 import BeautifulSoup
        from collectors import listing_cards
        html = """<a href="/jobs/relay/view?rec_idx=12">추천</a>
          <section class="list_recruiting"><div class="box_item">
            <div class="company_info"><a class="str_tit">예시</a></div>
            <a class="str_tit" href="/jobs/relay/view?rec_idx=34">온라인MD</a>
          </div></section>"""
        cards = listing_cards("saramin-home", BeautifulSoup(html, "html.parser"))
        self.assertEqual([(c["id"], c["title"], c["company"]) for c in cards], [("34", "온라인MD", "예시")])


if __name__ == "__main__":
    unittest.main()
