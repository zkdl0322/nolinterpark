# nolinterpark

NOL 인터파크([nol.interpark.com](https://nol.interpark.com/)) 취소표 예매 보조 매크로입니다.
[nolticket](https://github.com/zkdl0322/nolticket) 코드를 기반으로 합니다.

PySide6 GUI와 Selenium(Chrome)을 사용해 다음 흐름을 보조합니다.

1. NOL 통합 로그인(야놀자 계정) 페이지로 이동 → 사용자가 직접 로그인
2. 원하는 공연의 [예매하기] 진입 대기
3. 안심예매 보안문자(캡챠) 입력 보조
4. 좌석 등급 선택 (페이지에서 동적으로 읽음)
5. 구역 선택 (선택한 등급 색상으로 필터링)
6. 구역 순회 + 예매 가능 좌석 자동 클릭
7. 좌석선택완료 → 결제 페이지 진입 시 소리 알림

## 실행 환경

- Windows (소리 알림에 `winsound` 모듈 사용)
- Python 3.10 이상 권장
- Google Chrome 설치 필요 (ChromeDriver는 `webdriver-manager`가 자동 관리)

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python nolinterpark_macro.py
```

로그인 창에서 딜레이(구역 순회 대기 시간)를 설정하고 **시작하기**를 누르면
Chrome 창이 뜹니다. 로그인을 직접 완료한 뒤 안내에 따라 진행하세요.

## 주의

- 본 도구는 개인 학습/편의 목적의 자동화 보조 도구입니다.
- 사이트 이용약관 및 관련 법규를 확인하고 본인 책임 하에 사용하세요.
