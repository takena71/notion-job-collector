"""노션에 쓰지 않고 친구 본인의 연결과 테이블 열만 확인한다."""
from notion_sync import NotionClient

if __name__ == "__main__":
    client = NotionClient()
    client.validate_schema()
    print("노션 연결 및 수집함 열 확인 완료. 저장은 실행하지 않았습니다.")
