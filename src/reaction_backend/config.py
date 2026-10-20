import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 모델별 공개 단가 — (입력 USD/1M, 출력 USD/1M). 2026-07-30 기준.
#
# 왜 표가 필요한가: 요율을 하나만 두면 틀린다. 지금 쓰는 두 모델의 출력 단가가 **3.6배**
# 차이나기 때문이다(lite $2.50 vs flash $9.00). 하나로 뭉개면 어느 모듈이 돈을 쓰는지
# 영영 알 수 없다 — 실제로 그래서 "비용이 이상한데" 를 쿼리로 답하지 못했다.
#
# **사고(thinking) 토큰은 출력 단가로 과금된다.** `tokens_out` 이 이미 사고분을 포함하므로
# (provider `_extract_usage`) 여기서 따로 더하지 않는다.
#
# 새 모델을 도입하면 이 표에 넣어야 한다 — 빠뜨리면 `test_every_configured_model_has_pricing`
# 이 CI 에서 잡는다. 단가는 공개 가격표 기준이라 실제 청구서(할인·배치·캐시)와는 다를 수 있다.
LLM_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-2.5-flash": (0.15, 1.25),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


class Settings(BaseSettings):
    """re:action backend runtime settings.

    환경변수 키는 .env.example 참고.
    민감 값(LLM/OAuth/암호화 키)은 후속 이슈에서 채운다.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 앱 ──
    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    app_name: str = "reaction-backend"
    app_version: str = "0.1.0"

    host: str = "0.0.0.0"
    port: int = 8000

    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # Vercel preview URL 등 패턴 매칭이 필요할 때 사용 (CORSMiddleware allow_origin_regex).
    # 예: ^https://reaction-frontend-.*\.vercel\.app$
    cors_allow_origin_regex: str | None = None

    # ── DB ──
    # 형식: postgresql://user:pass@host:port/db
    # SQLAlchemy 사용 시 코드가 자동으로 postgresql+asyncpg:// 로 변환.
    # 비어있으면 앱은 기동되지만 DB 호출 시점에 명확한 에러로 surface.
    database_url: str = ""
    db_echo: bool = False

    # ── LLM (Issue #5 / ADR-0003) ──
    # Gemini API key. 비어있으면 Tool Executor 가 항상 fallback 으로 분기.
    gemini_api_key: str = ""
    # 호출 모델(base). 인터뷰/인박스/brief 등 지연 민감·경량 태스크가 이 모델을 쓴다.
    #
    # ⚠️ **`-latest` alias 를 쓰지 말 것.** 예전엔 퇴역(404) 회피용으로 alias 를 썼는데,
    # alias 는 Google 이 가리키는 대상을 말없이 바꾼다 — 실제로 `gemini-flash-latest` 가
    # `gemini-3.6-flash` 로 올라가 있었고, 코드 변경 0건으로 모델·단가·기본 thinking 정책이
    # 전부 바뀌었다. 자체 기록에는 요청한 alias 이름만 남아 청구서와 대조도 안 됐다.
    # 모델 교체는 **의도한 변경**이어야 하므로 고정 버전만 쓴다. 퇴역은 배포 전에 알아채는
    # 문제(호출 실패)지만, 조용한 alias 이동은 알아챌 방법이 없다.
    #
    # base 만 `gemini-2.5-flash` 다 (2026-09-04, **비용 결정**). 단가가 정확히 절반이다 —
    # 입력 $0.30 → $0.15, 출력 $2.50 → $1.25/1M. 아래 planning/recovery 는 lite 그대로라
    # 이 값이 닿는 건 `model_for_module` 기준 **interview / inbox / brief** 뿐이다.
    #
    # ⚠️ 이 선택의 알려진 약점: 2.5-flash 는 이 레포의 작업으로 품질을 **측정한 적이 없다.**
    # 2026-08-03 통일 때 lite 를 고른 이유가 정확히 그것이었다(절약액이 인터뷰 1,095회 ≈
    # $0.3 수준이라 미측정 리스크를 살 값이 아니라는 판단). 그 판단을 비용 쪽으로 뒤집은
    # 것이고, 되돌리려면 이 한 줄만 lite 로 바꾸면 된다.
    #
    # 계획 분해는 이 값에 안 걸린다 — 비용의 무게(회당 $0.0073)와 "계약을 어기면 계획 전
    # 구간이 룰 폴백" 리스크는 `llm_model_planning` 쪽이고, 거기는 실측된 lite 를 지킨다.
    #
    # thinking 경로도 갈린다: lite 는 `thinking_budget=0` 을 400 으로 거부해 설정을 아예
    # 안 보내지만, 2.5-flash 는 0 을 받는다(`provider._rejects_zero_thinking`). 둘 다
    # `tests/test_provider_thinking.py` 가 잠가 둔 계열이라 새 경로는 아니다 — 2026-07
    # 이전 이 레포의 기본값이 바로 이 모델이었다.
    llm_model: str = "gemini-2.5-flash"
    # task 별 모델 오버라이드. 빈 문자열이면 llm_model 로 폴백 (매핑은 `model_for_module`).
    #
    # planning 이 lite 인 이유(실측 2026-08-02, 동일 goal_decompose 프롬프트 3회씩):
    #
    #     모델                      세션수  세션길이  중복  출력   지연    회당
    #     gemini-3.5-flash          16 ✅   120 ✅    0   2,896  9.9s  $0.0290
    #     gemini-3.5-flash-lite     16 ✅   120 ✅    0   2,698  7.4s  $0.0073
    #
    # lite 가 4배 싸면서 **더 빠르고**, 분해 계약(총 세션 수·사용자가 말한 세션 길이)을 똑같이
    # 지킨다. 내용도 비교했고 `plan_quality` v2 검토가 양쪽 다 3/3 승인했다. 지연이 줄어
    # 타임아웃(45초) 여유도 커진다.
    #
    # recovery 도 lite 로 내려 **전 모듈을 한 모델로 통일**했다 (2026-08-03).
    # ⚠️ 그 통일은 2026-09-04 에 끝났다 — base 가 비용 결정으로 `gemini-2.5-flash` 가 됐다.
    # 아래 두 줄은 이제 base 와 **다른 값**이라, 맨 아래 "빈 문자열로 두지 않는다" 규칙이
    # 설명이 아니라 실제 방어선이 됐다.
    #
    # ⚠️ 이 값은 원래 상위 flash 였고, 그 근거는 "if-then 코핑 문장 자체가 산출물이라 품질
    # 민감도가 다르다 — 평가 없이 내리지 않는다" 였다. **그 평가는 아직 하지 않았다.**
    # 통일은 운영 결정으로 내려온 것이지 실측으로 뒷받침된 게 아니다. 회복 카드 문구 품질이
    # 눈에 띄게 나빠지면 이 줄만 `gemini-3.5-flash` 로 되돌리면 된다(다른 변경 불필요).
    #
    # 비용 측면에서는 이 줄이 2026-08-03 통일의 실질 이득이었다 — 출력 단가가 $9.00 →
    # $2.50/1M 로 3.6배 내려간다.
    #
    # 이 두 줄이 `gemini-2.5-flash`(단가가 정확히 절반)로 안 내려가는 이유: 2.5-flash 는
    # 이 레포의 작업으로 품질을 **한 번도 측정한 적이 없다**. 반면 lite 는 계획 분해 계약을
    # 지키는 것이 실측으로 확인됐다(위 표). 분해가 계약을 어기면 계획 전 구간이 룰 폴백
    # 자리표시자로 떨어지는데, 그건 회당 0.3센트로 살 수 있는 리스크가 아니다. base 가
    # 비용 결정으로 2.5-flash 가 된 뒤에도(2026-09-04) 여기는 그대로 두는 근거가 이것이다.
    #
    # 빈 문자열로 두지 않고 **명시한다.** 빈 문자열이면 base 를 따라간다 — base 가 이제 다른
    # 모델이므로, 이 두 줄을 비우는 순간 계획·회복이 미측정 모델로 조용히 내려간다.
    llm_model_planning: str = "gemini-3.5-flash-lite"
    llm_model_recovery: str = "gemini-3.5-flash-lite"
    # 검색 그라운딩 전용 모델 (#259 §4.2 ⑧). 다른 모듈과 **따로** 고정한다.
    #
    # 여기서 모델을 가르는 기준은 품질이 아니라 **지연**이다. 실측(#259 §2): lite 는 중앙
    # 8.5s(6.8~9.2) 에 출처 6~11 개, flash 는 23~80s. 그라운딩은 계획 생성 앞단의 별도
    # 단계라(⑦) 사용자가 화면 앞에서 기다린다 — flash 의 80s 는 그냥 못 쓴다.
    #
    # `model_for_module("planning")` 을 재사용하지 않는 이유: 지금은 우연히 둘 다 lite 지만,
    # 분해 품질 때문에 planning 을 상위 모델로 올리는 판단은 언제든 나올 수 있고 그때
    # 그라운딩까지 따라 올라가면 **이 단계가 조용히 못 쓰게 된다.** 두 결정은 근거가 다르므로
    # 값도 분리해 둔다.
    llm_model_grounding: str = "gemini-3.5-flash-lite"
    # 단일 호출 timeout (초). ADR-0003 §1 동결값.
    llm_timeout_seconds: float = 8.0
    # 그라운딩 호출 timeout (초). 동결된 8.0 은 **중앙값(8.5s)보다 짧아** 절반이 타임아웃난다.
    # 상한(9.2s)에 여유를 얹어 20s. 계획 분해(45s, #179) 안에 인라인하지 않는 전제(⑦)라
    # 이 지연은 이 단계 안에서만 산다.
    llm_grounding_timeout_seconds: float = 20.0
    # 재시도 횟수 (지수 backoff). Tool Executor §1.
    llm_max_retries: int = 3
    # 계획(First Plan) 분해·검토 전용 — 인터뷰 턴은 지연 민감(#76)이라 thinking 0 을 유지하되,
    # 분해(goal_decompose)·검토(plan_quality)는 진짜 추론이 필요해 modest thinking 을 허용한다.
    # gemini-2.5-flash 기준 thinking_budget(토큰). 0 이면 비활성(인터뷰와 동일). 로컬 튜닝용 env.
    llm_planning_thinking_budget: int = 2048
    # thinking 이 붙어 단일 호출이 길어지므로 계획 호출 timeout 은 별도로 상향(초).
    #
    # 45초인 이유(실측): 분해 프롬프트가 마감까지의 세션을 **한 번에** 만들도록 바뀌면서
    # (#coverage) 호출당 지연이 평균 8.7초에서 20~30초대로 올라갔다. 20초로 두면 성공 사례가
    # 17~19.7초에 밀집해 벽에 붙고, Gemini 가 조금만 느리면 재시도 3회가 전부 타임아웃해
    # **룰 폴백**으로 떨어진다 — 그러면 계획 전 구간이 "N회차" 자리표시자가 된다.
    # 45초에서 같은 입력 3회 모두 LLM 분해 성공(25.4 / 30.6 / 35.1초).
    #
    # 트레이드오프: 진짜 실패 시 llm_max_retries(3) × 45초 = 최대 135초를 기다린 뒤 폴백한다.
    # 계획 생성은 온보딩의 일회성 동작이고, 그 대기의 대안이 '자리표시자로 채워진 계획'이라
    # 기다리는 쪽을 택했다.
    #
    # 만다라 Stage A/B 도 이 timeout 을 공유한다(궁극목표 만다라트, #6-B) — 배포 전 실측
    # 필수 항목이었다(전략 문서 §11 항목 3). `scripts/measure_mandala_stage_b_latency.py`
    # 로 같은 입력 3회 실측(2026-08-21):
    #
    #     단계                         출력토큰(3회)          지연(3회)
    #     Stage A(mandala_subgoals)    245 / 1,754 / 1,518   3.0 / 5.7 / 5.6초
    #     Stage B(mandala_cells)       2,549 / 2,740 / 1,391 7.6 / 8.3 / 5.1초
    #
    # Stage B(64칸, 가장 큰 출력)도 최악 8.3초로 45초 상한에 크게 못 미친다 — goal_decompose
    # 가 겪은 "20초 벽" 패턴이 재현되지 않았다. A′(축 4개씩 2콜로 쪼개기) + 202 폴링 전환은
    # 불필요 — §11 항목 3 은 이 실측으로 닫혔다.
    llm_planning_timeout_seconds: float = 45.0
    # 일일 토큰 예산 (in + out 합산, 사용자당). 0 이면 무제한.
    llm_daily_token_budget: int = 200_000
    # 일일 토큰 예산 — **전 사용자 합산**(#325, FE #237 §8). 0 이면 무제한. 사용자별
    # 예산(위)과 별개 축이다 — 사용자별은 "이 사람이 폭주"를, 이건 "전체가 합쳐서 폭주"를
    # 막는다(다수가 나눠서 조금씩 쓰면 사용자별 상한엔 아무도 안 걸리면서 청구서만 큰
    # 시나리오가 사용자별 예산만으로는 안 막힌다). 초과 시 `BudgetExceeded` 를 그대로
    # 재사용해 Tool Executor 가 기존과 똑같이 룰 폴백으로 내린다(#325 팀 결정 — 회복은
    # 이미 검증된 룰 엔진, 계획·만다라도 폴백이 있으므로 전체 상한도 조용히 처리).
    #
    # 2,000,000 인 이유: 사용자별 상한(200k)의 10배 — 초대코드 30명 상한(#324) 아래에서
    # "10명이 동시에 개인 상한까지 꽉 채워 쓰는 날"에 걸리는 값이다. gemini-3.5-flash-lite
    # 단가(출력 $2.50/1M)로 최악(전부 출력) 잡아도 하루 $5 수준 — 베타 규모 회로차단기로
    # 적당하다. 이 값은 팀이 실사용을 보고 조정할 정책 숫자이지 실측으로 고정된 게 아니다.
    llm_global_daily_token_budget: int = 2_000_000
    # LLM 호출 1회에 보낼 수 있는 **프롬프트 최대 글자 수** (llm-5). 0 이면 무제한. 넘으면
    # provider 를 부르지 않고 `reason="budget"` 룰 폴백으로 내린다.
    #
    # 예산 가드만으로는 부족한 이유: 가드는 호출 **전** 잔량만 보므로, 사용 0 인 사용자가
    # 수십만 자를 보내면 한 번에 사용자별 한도를 통째로 넘긴다(실측: 인박스 3만 자 1건 =
    # tokens_in 21,179, 보통 ~185). 재시도(최대 3회)는 그걸 세 번 쓴다. 60,000 인 이유: 가장 큰
    # 정상 프롬프트(분해 템플릿 ~15k자 + 자료 2k자 + 변수)와 붙여넣은 자료 답(최대 2만 자)을
    # 싣는 인터뷰 채점(템플릿 ~9k자)의 두 배 넘는 여유를 둔 폭주 차단선이다.
    llm_max_prompt_chars: int = 60_000
    # 비싼 엔드포인트(interview turn/plans generate/mandala generate/recovery proposals)의
    # **사용자별 일일 호출 횟수** 상한 (#325). 0 이면 무제한. 위 토큰 예산과 다른 축이다 —
    # 이건 "AI 비용"이 아니라 "이 엔드포인트 자체(오케스트레이션·DB 왕복)를 오늘 몇 번
    # 실행했는가"를 센다. 계수 단위는 **요청 1건**이다 (#370 —
    # `safety/endpoint_rate_limit` 의 `DISTINCT trace_id`. 예전엔 `llm_runs` 행 수였고,
    # 엔드포인트 1회가 LLM 을 2~7번 부르므로 상한이 실질 2.5~7배로 조여져 있었다).
    #
    # 20 인 이유: 계획 생성·만다라·회복 제안은 사용자가 버튼을 눌러야 도는 1회성 행위라
    # 하루 20번이면 정상 사용을 한참 넘는다. 폭주 차단선이지 정상 상한이 아니다
    # — `llm_daily_grounding_budget` 와 같은 관례.
    llm_endpoint_daily_call_limit: int = 20
    # 인터뷰만 상한이 다른 이유 (#370): 인터뷰는 **한 번 하는 데 요청이 20건 가까이 든다.**
    # 계획 인터뷰 필수 슬롯이 18개고 슬롯당 요청 1건 + 세션 시작 1건이라 **완주 최소 19건**,
    # 재질문(답이 슬롯 형식에 안 맞으면 같은 슬롯을 다시 묻는다)까지 세면 20건을 넘는다.
    # 다른 엔드포인트와 같은 20 을 쓰면 **신규 사용자가 온보딩을 끝낼 수 없다** — 실제로
    # 배포 환경에서 그렇게 막혔다.
    #
    # 60 인 이유: 완주 1회(~21) + 재질문 여유 + 같은 날 재인터뷰("목표가 바뀌었어요", 지속형
    # 슬롯을 이어받아 ~14) 를 합쳐도 40 안쪽이다. 60 은 그 위의 폭주 차단선이다.
    # 이 값은 필수 슬롯 수에 딸려 움직인다 — 슬롯을 늘리면
    # `tests/test_endpoint_invocation_counting.py` 가 먼저 깨진다.
    llm_endpoint_daily_call_limit_interview: int = 60
    # 사용자별 **일일 검색 그라운딩 요청** 상한 (#259 §3). 0 이면 무제한(토큰 예산과 동일 관례).
    #
    # 5 인 이유: 그라운딩은 사용자가 버튼을 눌러야 도는 명시적 행위이고(#259 §4.1 ③ 결정),
    # 목표 하나에 한 번이면 충분하다. 재시도·다른 목표까지 감안해 5 로 둔다. 이 값은
    # **폭주 차단선**이지 정상 사용의 상한이 아니다 — 정상 사용자가 여기 닿으면 값이 아니라
    # 트리거 설계를 다시 봐야 한다.
    llm_daily_grounding_budget: int = 5
    # 1K 입력/출력 토큰당 USD ¢. Flash 무료 티어 기본 0, 유료 환산용.
    # 모델을 모르는 호출의 폴백 요율(1K 토큰당 센트). 보통은 `LLM_PRICING_USD_PER_1M` 이
    # 이긴다 — 이 둘은 표에 없는 모델을 만났을 때만 쓰인다.
    llm_cost_per_1k_input_cents: float = 0.0
    llm_cost_per_1k_output_cents: float = 0.0

    # ── 자료 검색 (ADR-0010) ──
    # (알라딘 Open API 는 2026-10-30 서비스 종료로 걷어냈다 — 도서 검색·쪽수는 아래
    # `nl_seoji_key` 한 키가 맡는다. 남은 `ALADIN_TTB_KEY` 환경변수는 무시된다.)
    # YouTube Data API v3. 비어있으면 영상 검색이 항상 `no_key` 로 실패 처리된다
    # (`integrations/youtube`). 발급: GCP 콘솔 → API 및 서비스 → YouTube Data API v3
    # 사용 설정 (무료, 10,000유닛/일).
    #
    # ⚠️ `search.list` 1회가 100유닛이다 — **앱 전체에서 하루 ~100회만 검색할 수 있다는
    # 뜻**이다(L0 실측, `docs/experiments/l0-materials-source-results.md`). 사용자별 예산
    # 가드는 아직 없다 — 초과하면 매 검색이 `quota_exceeded` 로 실패 응답을 낼 뿐이다
    # (`orchestrator/materials_catalog.py`). 실사용 규모가 커지면 캐싱이나 쿼터 증액이
    # 필요하다(ADR-0010 §4 밖의 알려진 제약).
    youtube_api_key: str = ""
    # 국립중앙도서관 ISBN 서지정보 API — 도서 후보 검색 · 쪽수 · 목차(`integrations/nl_seoji`).
    # 비어있으면 도서 검색·도서 상세가 `no_key` 로 실패 처리된다. 발급(무료, 자동승인):
    # 국립중앙도서관 로그인 → 도서관 서비스 → Open API → 인증키 신청/관리.
    #
    # ⚠️ 목차는 **best-effort 다.** L0 실측(도서 10권 전수)에서 목차가 채워진 건 1권(10%)
    # 뿐이었다 — 출판사가 납본 때 선택적으로 채우는 필드라 판별 규칙이 없다. 쪽수는 대부분
    # 온다(표기가 제각각이라 `parse_page_count` 가 숫자를 뽑는다).
    nl_seoji_key: str = ""

    # ── 보안 (Issue #5 §3) ──
    # 32-byte AES-GCM 키 (urlsafe base64 인코딩). 비어있으면 암호화 함수가 명시 에러.
    column_encryption_key: str = ""

    # ── Auth (Issue #16) ──
    # Google OAuth client (Google Cloud Console 발급). FE(웹)와 BE가 같은 client_id를 공유.
    # 비어있으면 /auth/google 호출 시 명확한 503 에러로 surface (auth_stub_mode=False 일 때).
    google_oauth_client_id: str = ""
    # Android 네이티브 로그인(Credential Manager)이 보내는 id_token 의 aud — 웹과 다른
    # OAuth Client(#322, FE #237 §3). 비어있으면 웹 client_id 하나만 허용(기존 동작 유지).
    # 선행 작업: Google Cloud 콘솔에서 패키지명(com.hanium.reaction) + SHA-1 3종(개발/업로드/
    # Play App Signing)으로 Android Client 등록 — 사람이 직접 해야 한다(FE #237 체크리스트).
    google_oauth_android_client_id: str = ""
    # 로그인(id_token)에는 안 쓰이지만 **캘린더 연결에는 필수**다 — authorization code 를
    # 토큰으로 바꾸려면 client_secret 이 있어야 한다 (#17 해제, integrations/google_calendar).
    google_oauth_client_secret: str = ""
    # 토큰 교환 때 보내는 redirect_uri. **비어 있으면 `postmessage`** — FE 가 GIS popup 코드
    # 흐름(initCodeClient)으로 code 를 받을 때의 값이라 콘솔에 등록할 리디렉션 URI 가 없다.
    # 리디렉션 흐름으로 바꿀 때만 채운다. 그때는 **Cloud 콘솔의 "승인된 리디렉션 URI" 와
    # 문자 단위로 같아야** 한다 — 다르면 토큰 교환이 redirect_uri_mismatch 로 떨어진다.
    google_oauth_redirect_uri: str = ""
    # 캘린더 연결 기능 스위치. False 면 connect/disconnect 가 501 로 남는다 —
    # client_id/secret/redirect_uri 가 준비되기 전에 배포돼도 사용자가 깨진 동의 화면을
    # 만나지 않게 하는 안전핀이다 (외부자원 셋업은 사람 손이 필요하다).
    google_calendar_enabled: bool = False

    # JWT — HS256. JWT_SECRET 은 32+ bytes 권장 (python -c "import secrets; print(secrets.token_hex(32))").
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # 웹 새로고침 뒤에도 하루 동안 로그인 상태가 유지되도록 access token 자체를 24시간 유지한다.
    # refresh token은 기존 14일. 계정 삭제는 access·refresh 모두 즉시 막힌다(사용자 조회 필터).
    # ⚠️ 로그아웃은 refresh 의 jti 만 **프로세스 메모리**(`auth/revoke.py`)에 등록한다 — 이미
    # 발급된 access token 은 만료(24시간)까지 살아 있고, 재배포·재기동하면 로그아웃한 refresh
    # 도 다시 통한다. 영구 차단은 DB 저장소(마이그레이션 필요)가 들어와야 성립한다.
    jwt_access_token_ttl_minutes: int = 24 * 60
    jwt_refresh_token_ttl_days: int = 14
    # refresh 쿠키(`reaction_refresh`, #323)를 심을 경로들. 웹은 Vercel rewrite
    # (`/api/:path*` → 백엔드 `/:path*`)를 거쳐 **브라우저 주소가 `/api/auth/...`** 인데
    # 백엔드는 그 프리픽스를 모른다. `Path=/auth` 하나만 두면 브라우저가 `/api/auth/refresh`
    # 에 쿠키를 싣지 않아 쿠키 폴백이 한 번도 동작하지 못했다(2026-09-17 발견).
    # 직접 접속(`/auth`)과 프록시 경유(`/api/auth`) 두 경로에 같은 쿠키를 심는다 — 프록시
    # 프리픽스가 바뀌면 이 목록만 바꾼다. JSON 배열(예: `["/auth","/api/auth"]`).
    refresh_cookie_paths: list[str] = Field(default_factory=lambda: ["/auth", "/api/auth"])

    # Local 개발에서 Google id_token 검증을 우회하고 고정 demo user 를 발급한다.
    # staging/prod 는 반드시 False. True 일 때 GOOGLE_OAUTH_CLIENT_ID 가 비어도 부팅 가능.
    auth_stub_mode: bool = False

    # ── Scheduler (#24) ──
    # True 면 앱 기동 시 in-process APScheduler 로 cron job 을 등록한다.
    # 기본 False — 테스트/로컬은 안 돈다(데모는 시드로 커버). ⚠️ in-process 라
    # 다중 인스턴스 배포 시 중복 실행(모든 job idempotent → 안전하나 단일 인스턴스 권장).
    scheduler_enabled: bool = False

    # ── 가입 게이트 (#324, FE #237 §8) ──
    # False 면 신규 가입(POST /auth/google, 신규 email)을 전부 403 으로 막는다 — 장애·비용
    # 폭주 시 재배포 없이 끄는 긴급 스위치(toggle-signups.yml, SCHEDULER_ENABLED 와 같은 관례).
    # 기존 사용자 로그인은 이 값과 무관하게 항상 통과한다.
    signups_enabled: bool = True
    # 신규 가입 인원 상한(누적, 초대코드 유효와 별개 조건). 미설정(None)이면 상한 없음 —
    # Play 첫 공개 30명 원칙(#237)은 2026-09-17 해제했다(가입 76명 중 67명이 stub 데모
    # 계정이라 실제 가입자 9명에서 이미 막혀 있었다). 다시 걸려면 `.env` 에 숫자를 넣는다.
    # soft-delete(archived_at) 된 사용자는 세지 않는다 — 나간 자리는 다시 채울 수 있어야 한다.
    signup_capacity: int | None = None
    # True 면 신규 가입에 유효·미사용 초대코드가 필요하다. 기본 False — Google 계정이면
    # 누구나 가입한다(위와 같은 날 해제). False 일 때 `inviteCode` 는 보내도 무시된다
    # (검증도 소비도 안 한다 — 예전 화면이 남은 코드를 보내도 가입이 막히지 않게).
    signup_invite_required: bool = False

    # ── Web Push VAPID (#16/#20) ──
    # 비어있으면 발송이 unconfigured 로 조용히 skip (GEMINI_API_KEY 부재와 같은 degrade).
    # 라이브 키는 provision-vapid.yml 워크플로가 EC2 .env 에 생성 — private key 는 레포·
    # 로그 어디에도 남기지 않는다. public key 는 FE pushManager.subscribe 에 필요(공개값).
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    # VAPID 스펙상 연락처 URI (mailto: 또는 https:). 푸시 서비스가 문제 시 연락하는 주소.
    vapid_subject: str = "mailto:j07801@hanyang.ac.kr"

    def model_for_module(self, module: str) -> str:
        """llm_runs.module → 사용할 Gemini 모델.

        계획(planning)·회복(recovery)만 전용 오버라이드(`llm_model_planning`/
        `llm_model_recovery`)를 가지고, interview/inbox/brief 는 base(`llm_model`)를 쓴다.
        오버라이드가 빈 문자열이면 base 로 폴백한다.

        2026-08-03 현재 세 값이 모두 `gemini-3.5-flash-lite` 라 **결과적으로 전 모듈이 같은
        모델**이다. 그래도 분기를 남겨 두는 이유: 회복은 실측 없이 내린 값이라 되돌릴 수 있어야
        하고(그때 이 분기가 유일한 손잡이다), 계획은 base 가 올라가도 딸려 올라가지 않아야
        한다. 값이 같다고 오버라이드를 지우면 그 두 성질이 사라진다.
        """
        override = {
            "planning": self.llm_model_planning,
            "recovery": self.llm_model_recovery,
        }.get(module, "")
        return override or self.llm_model

    def endpoint_call_limit_for_module(self, module: str) -> int:
        """llm_runs.module → 사용자별 일일 **요청** 상한 (#370). 0 이면 무제한.

        `model_for_module` 과 같은 관례 — 기본값 하나에 필요한 모듈만 오버라이드.
        interview 만 다른 이유는 위 필드 주석 참조: 인터뷰는 한 번 하는 데 요청이
        20건 가까이 드는 유일한 흐름이라, 다른 엔드포인트와 같은 숫자를 쓰면 상한이
        아니라 차단이 된다.

        base 가 0(무제한)이면 오버라이드와 무관하게 전부 무제한이다 — "가드를 끈다"는
        운영 스위치가 모듈별 값 때문에 반만 꺼지면 안 된다.
        """
        if self.llm_endpoint_daily_call_limit <= 0:
            return 0
        override = {"interview": self.llm_endpoint_daily_call_limit_interview}.get(module, 0)
        return override if override > 0 else self.llm_endpoint_daily_call_limit

    @model_validator(mode="after")
    def _validate_refresh_cookie_paths(self) -> Self:
        """쿠키 Path 는 `/` 로 시작하는 단순 경로여야 한다 — 틀리면 기동을 멈춘다.

        `;`·공백이 섞이면 Set-Cookie 헤더에 다른 속성을 끼워 넣을 수 있고, 빈 목록이면
        로그인해도 쿠키가 안 나가 웹 세션 유지가 조용히 꺼진다.
        """
        if not self.refresh_cookie_paths:
            raise ValueError("REFRESH_COOKIE_PATHS must not be empty")
        for path in self.refresh_cookie_paths:
            if not re.fullmatch(r"/[A-Za-z0-9/_\-.]*", path):
                raise ValueError(f"REFRESH_COOKIE_PATHS has an invalid path: {path!r}")
        return self

    @model_validator(mode="after")
    def _forbid_auth_stub_in_deployed_envs(self) -> Self:
        """staging/prod 에서 AUTH_STUB_MODE=true 를 부팅 시점에 차단한다 (Issue #94).

        stub 모드는 Google id_token 서명 검증을 통째로 건너뛰고 고정 demo 클레임
        (`demo@reaction.local`)을 반환한다 — 즉 **인증 우회**. 배포 환경(staging/prod)에
        이 값이 켜지면 서로 다른 Google 계정이 전부 한 유저로 붕괴한다 (#94 증상).
        설정 실수를 조용히 통과시키지 않고 앱 기동을 실패시켜(loud fail) 잘못된 배포가
        헬스체크를 통과하지 못하게 한다. local/dev 는 기존대로 stub 허용.
        """
        if self.auth_stub_mode and self.app_env in ("staging", "prod"):
            raise ValueError(
                f"AUTH_STUB_MODE=true is forbidden when APP_ENV='{self.app_env}'. "
                "Stub mode bypasses Google id_token verification and collapses every "
                "login into the demo user. Set AUTH_STUB_MODE=false and configure "
                "GOOGLE_OAUTH_CLIENT_ID for staging/prod."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
