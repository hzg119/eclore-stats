# Éclore 홍보 집계

서강대학교 성가대 피앗 제23회 정기공연 〈Éclore〉 사이트의 방문 집계 페이지 → https://stats.sogangfiat.com

GitHub Actions(`.github/workflows/stats.yml`)가 매시간 GoatCounter API에서 숫자를 받아 페이지를 다시 만든다.
집계 숫자만 담는다 (개인정보 없음). 토큰은 Actions 비밀값 `GOATCOUNTER_TOKEN`(읽기 전용)에만 있다.

이 레포의 파일은 원본 레포에서 `python tools/stats.py --sync`로 올라온다. 여기서 직접 고치지 않는다.
