# ADR-0010: 자료 조사 파이프라인 — 그라운딩 인용에서 API 검색으로

- 상태: 제안 (2026-09-03)
- 갱신(2026-10-21): 도서 소스를 **알라딘 → 국립중앙도서관 ISBN 서지정보**로 교체했다. 알라딘
  Open API 가 2026-10-30 종료되기 때문이다(신규 키 발급은 2026-09-04 종료). 아래 본문의 '알라딘'
  은 당시 결정의 기록으로 남긴다 — 지금 배선은 `integrations/nl_seoji` 한 키가 검색·쪽수·목차를 다 한다
  (api-change-log v2.38).
- 관련: #259 (자료 검색 3단계 HITL), [`docs/experiments/l0-materials-source-results.md`](../experiments/l0-materials-source-results.md)
  (L0 실측, 2026-09-01), ADR-0003 (LLM Tool Executor)
- 구현(이 ADR 범위): `agents/study_method_agent.py` · `prompts/planning/study_method.v1.md` ·
  `schemas/study_method.py`
- 구현(예정, 이 ADR 밖): 자료 검색(카탈로그) 단계 · 목차/분량 확정 단계 — §4 참고

## 배경

기존 자료 검색(#259, [`api/routes/materials.py`](../../src/reaction_backend/api/routes/materials.py))은
Gemini 검색 그라운딩 1회로 자료의 목차를 "확인"하는 방식이다. 그런데 상업 교재는
`finish_reason=RECITATION` 으로 통째로 막힌다([`llm/provider.py:46`](../../src/reaction_backend/llm/provider.py)
실측, 2026-08-23) — 인프런 강의 커리큘럼은 4/4 로 통과하지만 해커스 토익 같은 상업 교재는
3/4 실패한다. 사용자가 가장 자료를 필요로 하는 부류(토익·수험서)가 정확히 막히는 지점이라
기능이 사실상 죽어 있다.

동시에 요구는 그라운딩 인용보다 더 야심차다: "무슨 방식으로 공부하는 게 좋은지 찾아보고,
그 방식에 맞는 책이나 동영상을 찾아보고, 목차와 분량을 찾아서 계획을 세우게 하고 싶다."
지금 자료 흐름(`suggest_query` → 그라운딩 1회 → 사용자 확정)은 이 중 어느 것도 하지 않는다
— 목표 제목에 "목차 커리큘럼"을 붙인 검색어 하나로 **이미 정해진** 자료의 목차만 확인한다.

L0 스파이크가 재설계의 전제를 실측으로 갈랐다 (4주제 × 도서 10건 · 재생목록 4건):

| 소스 | 목차/커리큘럼 | 분량 |
|---|---|---|
| 알라딘 API (`OptResult=Toc`) | **0/10** — 그 파라미터는 실재하지 않는다 | 페이지 수 10/10 |
| 국중 seoji API | 1/10 (판본별로 다름) | — |
| 크롤링(알라딘·교보) | 불가 — AJAX 후행 로드 · 봇 차단 | — |
| YouTube API | **4/4** | **4/4** (재생시간) |

**핵심 발견**: 영상 강의는 도서 목차보다 나은 것을 준다. 영상 제목이 곧 커리큘럼인데,
"Chapter 3 DFS & BFS"(몇 시간짜리인지 모름)와 달리 "3. DFS & BFS [58분]"처럼 **단원마다
정확한 분량이 붙어 온다** — 세션 배치가 어림짐작이 아니라 산술이 된다.

## 1. 3단계 파이프라인 — LLM 은 "찾기·고르기·구조화", API 는 "가져오기"

RECITATION 은 모델이 저작물을 **낭송**하는 것을 막는 필터다. 반대로 알라딘·YouTube 상품
페이지의 메타데이터(제목·저자·페이지 수·재생목록 구성)는 판매자가 스스로 공개한 것이라
이 벽에 걸리지 않는다. 그래서 책임을 가른다: **LLM 은 무엇을 찾을지 판단하고, API 가
실제 내용을 가져온다.**

    ① Method Agent   목표 → 추천 방식 + 검색어 2종(도서/영상). LLM 구조화 호출 1회.
    ② 자료 검색       검색어 → 알라딘/YouTube 후보 3~5개. API 호출만, LLM 0회. HITL 선택.
    ③ 목차/분량 확정   선택된 자료 → 목차(best-effort)/분량. API 호출만, LLM 0회.

이 ADR 은 **①만** 결정한다. ②③ 은 각각 별도로 설계·합의한다(§4).

## 2. Method Agent 는 그라운딩을 쓰지 않는다

#259 의 그라운딩은 **특정 자료의 원문을 인용**하기 위한 것이라 그라운딩이 필수였다 — 안
그러면 모델이 존재하지 않는 목차를 자신 있게 지어낸다
([`provider.py:255`](../../src/reaction_backend/llm/provider.py) 주석, #259 §2 실측).

Method Agent 의 산출물은 성격이 다르다. "이 목표에 어떤 학습 전략이 효과적인가" 는 특정
저작물의 원문이 아니라 **일반적 지식**이라, 실시간 검색 없이도 모델이 이미 아는 것으로
충분하고 저작물 인용이 아니므로 RECITATION 리스크가 없다. 이 레포의 다른 계획 생성 호출
(`goal_decompose`·`mandala_cells`)도 전부 그라운딩 없는 구조화 호출이다 — 같은 결로 간다.
그라운딩이 `response_schema` 를 못 붙인다는 제약([`provider.py:255`](../../src/reaction_backend/llm/provider.py))도
자연히 피한다.

**결정: `aiClient.run(schema=StudyMethodPlan, ...)` 구조화 호출 1회.** `run_grounded` 는
쓰지 않는다.

## 3. 검색어를 2종으로 낸다 (도서 / 영상)

L0 실측이 "도서와 영상은 다른 강점을 가진 다른 자료"임을 보였다 — 도서는 페이지 수가
안정적이고(10/10), 영상은 커리큘럼+분량이 안정적이다(4/4). 검색어 하나로 두 소스를
겸용하면 어느 한쪽이 최적화되지 않는다 — "해커스 토익 RC 기본서" 는 도서 검색엔 맞지만
영상 재생목록 검색엔 "강의" 가 빠져 있어 결과가 나쁘다.

**결정: `book_query`/`video_query` 를 분리해 낸다.** 다음 단계(②, 이 ADR 밖)가 두 소스를
병행 검색해 사용자에게 후보로 보여준다.

## 4. 스코프 — 이 ADR 이 결정하지 않는 것

- **②** 실제 검색 API 배선(`integrations/aladin/`·`integrations/youtube/`), 쿼터 관리,
  HITL 후보 제시 UX.
- **③** 목차 best-effort 전략 — 국중 seoji 는 판본별로 10% 만 채워지므로(L0 §3.1) "안 되면
  사용자 붙여넣기" 폴백이 필요하다. 도서 목차가 없을 때 페이지 수만으로 세션을 배치하는
  산술 규칙도 여기서 정한다.
- **기존 `POST /plans/materials/search-query` 를 이 파이프라인으로 교체할지, 병행할지.**
  그 endpoint 는 "외부 호출 0회 = 무료" 라는 잠긴 프라이버시 결정을 깔고 있다
  ([`materials.py:1`](../../src/reaction_backend/api/routes/materials.py) 주석, #259 §4.1 ①).
  Method Agent 로 바꾸면 LLM 호출 1회가 생겨 그 결정을 건드리게 되므로, 배선은 별도
  합의가 필요하다(AGENTS §8 — 새 endpoint·응답 계약 변경).

## 5. 다음 단계

1. `agents/study_method_agent.py` + `prompts/planning/study_method.v1.md` — 이 ADR 의 구현.
2. 자료 검색 — 알라딘/YouTube API 클라이언트, HITL 후보 제시 endpoint.
3. 목차/분량 확정 — 선택된 자료의 목차(seoji best-effort)/분량, `goals.materials` 슬롯
   영속 방식(§4 마지막 항목의 합의 이후).
