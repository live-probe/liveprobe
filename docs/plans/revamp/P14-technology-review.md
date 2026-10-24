# P14 기술 검토 — GPT Decision API 및 Jev (TypeSafe AI)

## 1. 결론과 조사 경계

- **정체 확인:** 사용자가 언급한 `GPT Decision API`와 직접 부합하는 공식 제품은 OpenAI의 **Decisions API**다. OpenAI 공식 개발자 문서에 `POST https://api.openai.com/v1/decisions`와 지원 모델 `gpt-6-luna`가 명시되어 있다. 별도 업체의 제품이라고 볼 근거는 발견하지 않았다. 사용자의 원 표현은 보존하며, 공식 명칭과 원 표현이 문자 그대로 같다는 뜻은 아니다. [O1, O2]
- **정체 확인:** `Jev (TypeSafe AI)`는 TypeSafe AI가 개발한 판단 전용 모델이다. 공식 홈페이지 → 문서 → 공식 GitHub 조직·SDK의 상호 연결로 발행 주체를 확인했다. 이름이 비슷한 다른 타입 안전성 라이브러리로 대체해 해석하지 않았다. [T1, T2, T8, T9]
- **도입 판단:** 두 기술 모두 **지금의 LiveProbe 런타임·평가에는 도입 보류**한다. 기능이 없어서가 아니라, 현재 승인 범위인 **구독 Codex CLI 전용**과 별도 API 인증·과금·외부 데이터 처리 경계가 충돌하기 때문이다. 조사 요청은 API 키 사용·계정 개설·유료 시험·운영 로그 반출 승인이 아니다.
- **향후 검토 가치:** 자유 서술 분석 전체의 교체가 아니라, 제한된 선택지 분류·근거 충분성 판정·검토 우선순위 산정 같은 작은 판단을 위한 후보이다. 별도 승인 후에도 규칙 기반 HIGH_RISK, LangGraph, 근거 인용 검증, 별도 실행 권한과 RBAC/admission을 유지해야 한다.

### 조사 메타데이터

| 항목 | 내용 |
|---|---|
| 조사 질문 | 두 명칭의 정확한 제품·제공자·공식 인터페이스는 무엇이며, 기존 LiveProbe 제약 안에서 사용할 수 있는가? |
| 조회일 | **2026-10-18 KST**. 도구 `date '+%Y-%m-%d %H:%M:%S %Z'` 결과: `2026-10-18 21:30:52 KST` |
| 자료 종류 | **조회일의 라이브 웹 자료**. 당시 화면을 복원한 아카이브 자료가 아님 |
| 출처 경계 | OpenAI 및 TypeSafe AI의 공식 원문 우선. 검색 결과·광고·위키·제3자 비교 글은 발견 단서로만 사용 |
| 검증 방법 | 실제 웹 검색 후 공식 URL을 브라우저로 열어 본문 추출. 로컬 구현은 파일 읽기로 비교 |
| 수행하지 않은 일 | API 키 조회·사용, 인증된 추론 호출, Playground 입력, SDK 설치, 실제 사건·로그·Secret 업로드, 배포·설정·코드 수정 |
| 신뢰도 | 제품 정체·공개 인터페이스: 높음. 공식 문서에 적힌 가격·정책: 조회일 기준 확인. LiveProbe 품질·속도·절감 효과: 미측정 |
| 문서의 효력 | 검토 근거이지 구현 승인·Jira 범위 확정·실행 증거가 아님. `과제계획서.md`는 초기 초안으로 취급 |

발행일·문서 갱신일과 조회일은 구분한다. 아래 출시일은 현재 원문에 적힌 날짜를 확인한 것이며, 그 날짜의 과거 화면까지 확인했다는 뜻이 아니다.

## 2. 검색어와 취득 기록

모든 검색·원문 조회는 2026-10-18 KST에 수행했다.

| 실제 검색어 | 관찰 결과와 후속 확인 |
|---|---|
| `"GPT Decision API"` | 첫 직접 검색은 제목·URL·본문이 빈 결과를 반환했다. 제품 부재의 근거로 삼지 않고 표현을 넓혀 재검색 |
| `"GPT" "Decision API" OpenAI` | OpenAI 공식 Decisions 가이드 발견 → O1 원문 추출 |
| `"GPT Decision API" -site:youtube.com` | 공식 Decisions 가이드 및 여러 제3자 글 발견. 공식 원문을 판단 근거로 사용 |
| `"GPT Decision" API github` | 빈 결과. 이 검색만으로 API가 없다고 결론 내리지 않음 |
| `site:developers.openai.com "Decisions API" pricing` | 공식 가이드·가격표·변경 이력 발견 → O1·O2 원문으로 존재·베타·요금 확인 |
| `site:github.com/openai "decisions" "gpt-6-luna"` | 공식 조직 저장소의 관련 이슈 발견. 이슈 작성자 주장은 공식 제품 사양의 근거에서 제외 |
| `"Jev" "TypeSafe AI"` | TypeSafe 공식 홈페이지·소개·모델 문서 발견. 광고·위키·비공식 Jev 사이트도 함께 노출되어 제외 |
| `site:docs.typesafe.ai Jev TypeSafe AI API` | 공식 HTTP API·빠른 시작·모델·System One 설명 확인 → T2~T5·T11 원문 추출 |
| `site:github.com/typesafe-ai Jev systemone` | 공식 조직·SDK·스킬 저장소 발견 → T8·T9 원문 추출. 외부 스킬 설치·실행은 하지 않음 |

### 취득 장애와 한계

- 첫 코드 모드 병렬 검색은 도구 훅 시간 초과로 실패했다. 이 실패는 검색 무결과가 아니다. 이후 직접 `web_search` 호출로 위 검색을 수행했다.
- `web_extract`는 설정된 `ddgs`가 검색 전용이라 URL 본문 추출을 지원하지 않는다는 오류를 반환했다. 설정을 바꾸거나 유료 서비스를 추가하지 않고 **실제 브라우저의 공식 페이지 본문 추출**로 대체했다.
- 일부 검색은 백엔드가 실패한 뒤 도구의 대체 검색 결과를 반환했다. 최종 사실 판정은 검색 요약이 아니라 직접 연 공식 본문에 근거한다.
- T10 하위처리자 목록은 브라우저에서 본문이 비어 내용을 확보하지 못했다. DPA에 목록 링크가 있다는 사실만 확인했으며, 하위처리자 명단을 검증했다고 주장하지 않는다.

## 3. 공식 1차 출처 목록

아래 표의 조회일은 모두 **2026-10-18 KST**, 자료는 모두 라이브다. 발행·갱신일이 본문에 명시되지 않은 문서는 `미표시`로 적었다. 원문 취득 성공은 공개 본문 확인이지 계약 체결이나 해당 계정의 기능 활성화 확인이 아니다.

| ID | 발행자 | 정확한 URL | 본문에 표시된 발행·갱신일 | 확인한 내용·한계 |
|---|---|---|---|---|
| O1 | OpenAI | https://developers.openai.com/api/docs/guides/decisions | 미표시 | 공식 명칭, 전용 엔드포인트, 공개 베타, 모델, 질문 유형, 가격·데이터 제어 |
| O2 | OpenAI | https://developers.openai.com/api/docs/changelog | 10월 6일 항목 | `gpt-6-luna` Decisions API 베타 출시 기록. 라이브 변경 이력의 날짜 표기 |
| O3 | OpenAI | https://developers.openai.com/api/docs/guides/your-data | 미표시 | `/v1/decisions`의 학습 이용·보관·ZDR·지역 처리·예외 |
| O4 | OpenAI | https://developers.openai.com/api/reference/overview | 미표시 | API Bearer 인증, API 키·워크로드 신원 연동 토큰, 조직·프로젝트 사용량 |
| O5 | OpenAI | https://developers.openai.com/codex/pricing | 미표시 | Codex 구독 사용량과 API 토큰 과금이 별개임을 명시 |
| T1 | TypeSafe AI | https://typesafe.ai/ | 미표시 | Jev·System One 소개. 형식 보장과 정답 여부를 구분하는 설명 |
| T2 | TypeSafe AI | https://docs.typesafe.ai/introduction | 미표시 | state와 독립적인 typed questions, Choice·Score·Noul |
| T3 | TypeSafe AI | https://docs.typesafe.ai/models | Jev 1.14 제공일 2026-10-14 명시 | 모델 ID·별칭·가격·컨텍스트·언어·보관 관련 안내 |
| T4 | TypeSafe AI | https://docs.typesafe.ai/introduction/quickstart | 미표시 | 별도 API 키, SDK·직접 HTTP·Playground 사용 경로 |
| T5 | TypeSafe AI | https://docs.typesafe.ai/api | 미표시 | `POST /v1/systemone`, 요청·응답 구조, 선택지·점수 범위, 오류·재시도 |
| T6 | TypeSafe AI | https://docs.typesafe.ai/confidence | 미표시 | Choice·Score의 confidence는 확률분포에서 계산, Noul에는 별도 confidence 없음 |
| T7 | TypeSafe AI / 창업자 Diogo Almeida | https://typesafe.ai/blog/introducing-system-one-models-and-jev | 2026-09-15 | 초기 공개·RLCD·공급자 성능 주장과 측정 조건·한계 |
| T8 | TypeSafe 공식 GitHub 조직 | https://github.com/typesafe-ai | 미표시 | 공식 사이트 링크, Python·JavaScript SDK 및 관련 공개 저장소 |
| T9 | TypeSafe 공식 GitHub 조직 | https://github.com/typesafe-ai/typesafe-sdk-python | 미표시 | 공식 Python SDK, `typesafe-sdk`, API 키 사용, MIT SDK 라이선스 |
| T10 | TypeSafe AI | https://trust.typesafe.ai/subprocessors | 취득 불가 | DPA에서 연결한 공식 목록 URL. 본문 미확보로 명단 미확인 |
| T11 | TypeSafe AI | https://docs.typesafe.ai/concepts/system-one | 미표시 | 텍스트 전용, 자유 설명·코드 생성 안 함, 보정이 개별 정답을 보장하지 않음 |
| L1 | TypeSafe AI | https://docs.typesafe.ai/legal | 미표시 | DPA·고객 계약·개인정보 정책 링크, 기업 고객 ZDR 문의 경로 |
| L2 | TypeSafe AI, Inc. | https://typesafe.ai/legal/data-processing | 2026-04-24 갱신 | 처리자 역할·하위처리자·국제 이전·보관 원칙·사고 통지 |
| L3 | TypeSafe AI, Inc. | https://typesafe.ai/legal/privacy-policy | 2025-11-19 갱신 | 입력 학습·미세조정 미사용, 일반 보관 원칙, 미국 호스팅·저장·처리 |

이 출처들은 공급자의 1차 설명이다. 공급자 성능 수치의 독립 재현 결과는 아니며, 제3자 광고·`jevmodel.org`·위키 등의 내용을 공식 사양으로 채택하지 않았다.

## 4. GPT Decision API 검토

### 4.1 확인한 사실

| 주제 | 공식 원문에서 확인한 내용 |
|---|---|
| 제품 정체 | OpenAI의 **Decisions API**. 전용 `POST /v1/decisions`가 존재하므로 일반적인 프롬프트 기법이나 임의 명칭의 래퍼로 볼 필요가 없다. [O1] |
| 제공 상태·모델 | 조회일 현재 공개 베타. 지원 모델은 `gpt-6-luna` 하나라고 명시한다. 향후 GA 예상은 공급자 전망이지 GA 완료 사실이 아니다. [O1, O2] |
| 입력 | 텍스트 또는 텍스트·이미지 메시지. 이미지는 인라인 base64 데이터 URL만 지원하고, 호스팅 HTTP(S) 이미지 URL과 `file_id`는 지원하지 않는다. [O1] |
| 질문·응답 | `model`, `input`, `questions`를 입력하고 `answers` 배열을 받는다. 질문 이름을 응답에서 돌려준다. [O1] |
| 유형 | `predicate`: 조건의 참 확률. `choice`: 지정 선택지 하나와 확률분포·confidence. `score`: 순서 있는 단계 인덱스의 확률 가중 평균과 확률분포·confidence. [O1] |
| 여러 질문 | 독립 질문은 한 요청에서 공유 입력을 평가할 수 있다. 선행 답에 의존하는 판단은 별도 요청을 쓰라고 안내한다. [O1] |
| 생성 작업과의 구분 | 임의 JSON 스키마 객체·추출 필드·서술 설명은 Responses의 Structured Outputs, 도구 호출 인자는 function calling을 사용하라고 구분한다. Decisions가 기존 모든 AgentOutput을 반환하는 API는 아니다. [O1] |
| 성능 표현 | Responses 대비 약 10배 빠르다는 것은 공급자 주장. LiveProbe·한국 리전·현재 Codex CLI 대비 실측이 아니다. [O1] |

### 4.2 비용·인증·데이터 경계

- **공개 가격:** `/v1/decisions`의 `gpt-6-luna` 입력 **100만 토큰당 US$0.10**, 출력·캐시 읽기·캐시 쓰기 별도 비용 없음. 지역 처리 가산 및 긴 컨텍스트 입력 배수가 적용된다고 명시한다. 같은 모델의 다른 API 가격에 이 요금을 그대로 적용하면 안 된다. [O1]
- **인증:** 공식 예시는 OpenAI API Bearer 자격증명을 사용한다. 공통 API 문서는 API 키 또는 워크로드 신원 연동으로 발급한 단기 토큰을 설명한다. **Codex의 ChatGPT OAuth 자격증명으로 이 엔드포인트를 호출할 수 있다는 근거는 확보하지 못했다.** 기존 구독 로그인 정보를 API 자격증명으로 재사용해서는 안 된다. [O1, O4]
- **과금 분리:** 공식 Codex 문서는 API 토큰 가격과 구독 사용량이 별개라고 명시한다. 현재의 API 가격 기반 추정 비용 장부와 실제 구독 지출도 구분해야 한다. 저렴한 단가만으로 현재 구독 방식보다 현금 지출이 줄었다고 결론 낼 수 없다. [O5]
- **학습·보관:** API 데이터는 명시적 공유 동의가 없으면 학습에 사용하지 않는다고 안내한다. `/v1/decisions`의 기본 악용 모니터링 로그 보관은 최대 30일이며, 일반 정책에는 법적 요구·위험 대응 예외가 있다. ZDR은 적격 고객의 사전 승인·설정이 필요하다. **학습 미사용은 저장하지 않는다는 뜻이 아니다.** [O3]
- **캐시·지역:** 암호화된 프롬프트 캐시 상태가 GPU 로컬 저장소에 최대 24시간 남을 수 있다는 별도 설명이 있다. 이미지에는 CSAM 탐지 시 보관 예외가 있다. Decisions의 지역 추론 처리는 미국·유럽(EEA+스위스)을 지원한다. 한국에서 API를 사용할 수 있다는 사실을 한국 내 추론 보장으로 읽으면 안 된다. [O3]
- **이번 작업:** 키 사용·요청 전송 없음. 실제 사건·로그·Secret을 OpenAI API나 Playground에 추가 반출하지 않았다.

### 4.3 미확인 사항

- 사용자가 이 이름을 접한 원 링크는 제공되지 않았다. 공식 API와 기능상 직접 부합하지만, 그 링크까지 동일 제품으로 확정한 것은 아니다.
- 해당 계정의 API 프로젝트·인증·결제·한도·ZDR 활성화 여부, 배타적 국내 처리 충족 여부는 확인하지 않았다.
- 검토한 가이드에서 정확한 Decisions 입력 상한·긴 컨텍스트 적용 기준·계정별 처리량은 확정하지 못했다. 일반 모델 컨텍스트 수치를 임의 대입하지 않는다.
- Kubernetes 사건 라우팅 정확도, 과소·과잉 구성률, 확률 보정 수준, 한국어·혼합 언어 성능, p95 지연, 장애율·재시도 비용은 미측정이다.
- `gpt-6.1-sol`·`gpt-6-astra`의 Decisions 지원은 확인되지 않았다. 기존 Codex 모델 사용 이력과 별개의 API 지원 문제다.

### 4.4 LiveProbe에 대한 판단

**현재는 보류.** 향후 별도 API 허용 시 `choice`로 `SIMPLE/NORMAL/COMPLEX` 중 분석 깊이 후보를 판단하거나, `predicate`로 특정 근거의 충분성을 점검하는 작은 보조 판단을 검토할 수 있다. 이때도 현재 규칙의 하향 금지와 HIGH_RISK 권한을 그대로 유지한다.

`RouterOutput`은 `level/rationale/confidence`를 요구한다. Decisions의 선택 결과·confidence만 매핑하고 **존재하지 않는 자연어 rationale을 생성된 근거처럼 꾸미면 안 된다**. 질문·선택지·확률과 규칙에 근거한 이유 기록 방식부터 별도 합의해야 한다. `score`를 반올림해 안전 등급으로 쓰는 방식도 별도 검증 없이 도입하지 않는다. 자유형 원인 후보·근거 인용·실행 계획을 포함한 전체 AgentOutput이나 LangGraph를 대체할 수 있다는 결론은 내리지 않는다.

## 5. Jev (TypeSafe AI) 검토

### 5.1 확인한 사실

| 주제 | 공식 원문에서 확인한 내용 |
|---|---|
| 제품 정체 | Jev는 TypeSafe의 첫 System One 모델이며, 자연어 입력에서 코드가 소비할 typed decisions와 확률을 반환한다. 공급자는 새 모델 구조·병렬 샘플러·RLCD를 설명한다. 이는 공급자 설명이며 모델 내부를 독립 검증한 것은 아니다. [T1, T2, T7] |
| 공개 인터페이스 | `POST https://api.typesafe.ai/v1/systemone`. 요청은 `state`, `model`, 질문 ID별 `questions` 맵, 응답은 모델 ID·질문 ID별 `answers` 맵·usage. OpenAI Decisions와 동일한 요청 스키마가 아니다. [T5] |
| 유형 | `Choice`: 사전 지정 옵션·전체 확률분포·confidence. `Score`: 정의한 단계의 확률 가중 점수·분포·confidence. `Noul`: 참일 확률 `noul` 값이며 별도 confidence 없음. [T2, T5, T6] |
| 선택지 한계 | Choice 최대 255개. Score는 최소 2단계 권장, API 최대 10단계. 한 요청의 독립 질문은 같은 state에 대해 개별 평가한다. [T2, T5] |
| 입력·출력 범위 | 현재 Jev는 텍스트, JSON 객체·배열을 평가한다. System One 설명은 이미지·오디오·비디오 미지원이라고 명시한다. 자유형 답변·코드·추론 설명을 생성하는 모델이 아니다. SDK에 이미지 도우미가 있다는 사실만으로 현재 Jev의 이미지 추론 지원을 추정하지 않는다. [T3, T9, T11] |
| 모델 버전 | `jev-1.13.0`, `jev-1.14.0`. 1.14는 2026-10-14 제공이라고 안내한다. 조회일 별칭 표는 `jev-latest` → `jev-1.13.0`, `jev-preview` → `jev-1.14.0`이다. 1.14가 새 버전이라는 이유로 stable 별칭도 1.14라고 고쳐 쓰지 않는다. [T3] |
| 컨텍스트 | 기본 요청 전체 64k, 질문별 32k. 긴 컨텍스트는 기본 꺼짐이며 `TypeSafe-Danger-Zone-Long-Context: true`로 두 한도를 262k로 확장한다고 설명한다. 확장 시 더 느리고 덜 정확하다는 경고가 있다. [T3] |
| 언어·모델 적응 | 영어가 주요 학습 언어이며 CJK 등은 동등한 품질을 보장하지 않는다. 고객별 미세조정·LoRA 대신 state·질문·기준과 코드 조합을 사용한다고 설명한다. [T3] |
| SDK | 공식 `typesafe-sdk` Python SDK 및 JavaScript SDK가 존재한다. Python SDK 저장소는 MIT 라이선스다. 이것이 모델 가중치 공개나 자체 호스팅 권한을 의미하지 않는다. [T8, T9] |

### 5.2 형식 안전성과 판단 안전성은 다르다

- 홈페이지의 환각 0% 설명은 **출력 스키마 일치 보장**을 가리키며, 관찰된 정답률 100%가 아니라고 같은 페이지에서 한정한다. [T1]
- 공식 System One 설명도 확률 보정은 예측 집단에 대한 성질이지 개별 답의 정답 보장이 아니라고 명시한다. 따라서 잘못된 원인 분류·안전하지 않은 조치 선택·누락 근거까지 방지한다고 해석하면 안 된다. [T11]
- Choice·Score의 confidence는 확률분포에서 계산하는 통계다. 현재 Agent가 출력하는 자기평가 confidence와 의미가 같다고 간주해 같은 임계값을 복사할 수 없다. [T6]
- 공식 블로그의 70~500ms 및 큰 비용·속도 개선 수치는 공급자 워크플로우·지역·짧은 입력 등의 측정 조건을 동반한다. LiveProbe에서 재현했다고 주장하지 않는다. [T7]

### 5.3 비용·인증·데이터 경계

- **공개 가격:** 두 모델 모두 입력 **100만 토큰당 US$0.042**, 출력 무료. 긴 컨텍스트도 토큰당 같은 가격이라고 안내한다. 계정별 한도·기업 계약·최저 결제 조건은 확인하지 않았다. [T3]
- **인증:** TypeSafe 콘솔에서 별도 API 키를 발급하고 Bearer 인증을 사용한다. SDK는 `TYPESAFE_API_KEY`를 읽는 예시를 제공한다. Codex 구독·OAuth와는 별개다. 키를 발급·저장·입력하지 않았다. [T4, T5, T9]
- **학습 미사용:** 모델 문서는 고객 요청·응답으로 학습하지 않는다고 명시하고, 개인정보 정책은 Input으로 학습·미세조정하지 않는다고 설명한다. [T3, L3]
- **보관:** 일반 개인정보 정책은 서비스·사업 목적상 합리적으로 필요한 기간, DPA는 처리 목적·법에 필요한 기간이라는 원칙을 제시한다. 조사한 공개 계약 원문에서 일반 API 입력의 고정된 삭제 일수를 확정하지 못했다. ZDR은 기업 고객 문의 사항이지 기본 계정의 자동 보장이 아니다. [L1~L3]
- **국제 이전:** 개인정보 정책은 미국 호스팅 및 미국 저장·처리를 명시한다. DPA는 국제 이전과 하위처리자 이용을 규정한다. 한국 내 처리·국내 보관을 충족한다고 주장할 수 없다. 하위처리자 목록 원문은 취득 실패했다. [L2, L3, T10]
- **자체 호스팅:** SDK 공개는 확인했으나, 검토 원문에서 Jev 모델 가중치 다운로드·온프레미스 추론 계약·자체 호스팅을 확인하지 못했다. 불가능하다고 단정하는 대신 미확인으로 남긴다.
- **이번 작업:** 실제 state·사건·로그를 TypeSafe API·콘솔·Playground에 전송하지 않았다.

### 5.4 미확인 사항과 도입 판단

**현재는 보류.** API 전용 인증·과금·미국 처리·보관 조건은 현 승인 범위 밖이다. 신뢰도 높은 숫자라도 HIGH_RISK·실행 승인·RBAC를 대체할 수 없다. Jev의 강점은 원자적 분류·점수·참 확률이며, 원인 분석 보고서·반증·근거 인용을 쓰는 현재 모든 Agent의 직접 교체 후보는 아니다.

향후 API 허용 시 제한된 질문의 병행 평가 후보로 남길 수 있다. 다만 실제 한국어·영문 로그 혼합 상태의 정확도·보정, 입력 길이에 따른 성능, p95 지연, 타임아웃·429·529 비용, 조직별 한도·삭제 보장·기업 ZDR 조건은 검증하지 않았다. `jev-latest` 자동 변경을 허용하지 말고 버전 고정·반환 모델 기록·임계값 재검증을 조건으로 삼아야 한다. 이는 향후 설계 권고이지 이번에 구현한 기능이 아니다.

## 6. 기존 구현과의 비교

아래는 조회일의 **로컬 소스 확인** 결과다. 배포 클러스터나 신규 기술의 실호출 검증 결과가 아니다. 기존 API provider 코드의 존재와 현재 사용 승인·배포 활성화는 구분한다.

| 영역 | 기존 구현·근거 | 신규 기술과 비교·판단 |
|---|---|---|
| 승인된 추론 경로 | `liveprobe/app/llm/codex_cli.py:35-58`: `codex exec`, `--output-schema`, 격리 HOME, CODEX_HOME OAuth, 환경변수 허용목록. `engine.py:489-535`: 한도 도달 시 대기·계정 전환, API·Claude로 전환하지 않는 경로 | 두 후보는 별도 HTTP API다. 같은 `gpt-6-luna`라도 Codex 호출과 Decisions endpoint는 다른 인증·과금 경로 |
| provider 확장점 | `liveprobe/app/llm/__init__.py:83-103`에 API provider 선택 코드가 있고, `openai_api.py:34-48`은 **Responses API** 호출 | API provider 파일이 있다고 API 사용이 승인된 것은 아니다. 기존 `openai_api` 설정만 켜면 Decisions가 된다는 주장도 틀림 |
| 출력 형식 | `llm/base.py:67-84` strict JSON schema, `codex_cli.py:135-143` JSON Schema 검증, `llm/__init__.py:16-80` Pydantic 모델, `engine.py:438-458` Pydantic 검증·잘못된 출력 재시도 | 형식 검증은 이미 있음. typed endpoint는 분포 제공·판단 전용 인터페이스라는 차이가 있으나 그것만으로 분석 품질이 상승하지 않음. Pydantic 전체 strict 모드가 켜졌다고 주장하는 것은 아님 |
| 라우팅 안전 | `router.py:85-105,167-223`: 규칙·근거 실패 이관, 하향 금지, 정책 C에서 LLM HIGH_RISK 제안은 최대 COMPLEX로 제한. HIGH_RISK 위험 등급은 결정적 규칙이 우선 | 신기술은 비고위험 분석 깊이의 후보만 보조 가능. HIGH_RISK 결정권·하향 권한·자동실행 권한을 이전하지 않음. 실패로 인한 안전 이관 경로는 별도로 유지 |
| 전체 Agent 결과 | `llm/__init__.py:31-44`: summary·원인 후보·evidence_refs·반증·조치·설명·confidence, RouterOutput에는 rationale 요구 | Decisions·Jev의 제한된 typed answers만으로 자유 서술 필드를 채울 수 없음. 별도 매핑 계약과 설명·인용 보존 설계가 필요 |
| 워크플로우 | `workflows.py:8,80-131`, `agent_groups.py`: LangGraph와 분석·독립 검증·조기 종료·승격 | 작은 판단 함수 후보이지 오케스트레이터 대체재가 아님. 질문 병렬 평가와 전체 의존 그래프 실행은 서로 다른 기능 |
| 실행 권한 | `safety.py:55-68,71-81`, `deploy/optional/safe-action-executor/safe-action-executor.yaml`: 별도 SA·대상별 Role·admission `failurePolicy: Fail`·대상 namespace·조치 범위 제한 | typed 출력이 Kubernetes 쓰기 권한을 부여하지 않음. 모델 응답은 제안일 뿐, 기존 executor·RBAC·admission을 통과해야 함 |
| 비용 기록 | `llm/__init__.py:106-121`, `engine.py:459-466`: API 가격표 기반 추정액·사용량·시간 기록 | 후보별 실제 HTTP usage와 신규 가격 버전 기록 필요. 구독 비용·API 추정 비용·실제 API 청구를 섞지 않음 |

현재 모델 표기 `luna / gpt-6-luna`, `mid / gpt-6.1-sol`, `high / gpt-6-astra`는 그대로 유지한다. 이 검토는 기존 실호출 이력을 부정하거나 모델명을 교정하는 작업이 아니다. 새 endpoint의 지원 모델과 기존 Codex의 지원 이력은 분리해서 다룬다.

## 7. 권고와 재검토 조건

| 기술 | 지금의 권고 | 재검토할 수 있는 위치 | 보류 해제에 필요한 조건 |
|---|---|---|---|
| GPT Decision API → 공식 OpenAI Decisions API | **도입·유료 시험 보류** | 작은 분류·참 확률·우선순위 판단의 보조 후보 | 별도 API 사용·예산·인증·외부 데이터 처리 승인, 계정 접근·한도 확인, 베타 변경 위험 수용, 스키마·rationale 계약, 실제 도메인 평가 |
| Jev (TypeSafe AI) | **도입·유료 시험 보류** | 독립적인 원자 질문·Choice/Score/Noul의 병행 평가 후보 | 별도 공급자·API·예산 승인, 미국 처리·보관·하위처리자·ZDR 계약 확인, 버전 고정, 한국어·혼합 로그·확률 보정 평가 |

### 승인된 뒤에만 진행할 검증안

1. **권한과 데이터부터 확정:** 승인 주체·허용 공급자·자격증명 방식·예산 상한·반출 필드·보관/삭제·리전·기업 계약을 명시한다. 원본 운영 로그, Secret, 토큰, ConfigMap 값은 보내지 않는다. 비식별 요약도 자동 승인으로 보지 않는다.
2. **합성 데이터부터 시험:** 승인된 무민감 합성 사건으로 연결·스키마·장애 응답만 확인한다. 실사건 재사용은 별도 반출 검토 이후다. 지금은 이 연결 시험조차 수행하지 않았다.
3. **결정권 없는 병행 비교:** 기존 판단과 나란히 기록하되 라우팅·실행에는 영향 주지 않는다. 입력 요약·질문·선택지·모델 버전·분포·지연·토큰·오류·추정 및 실청구 비용을 분리 기록한다.
4. **정확도와 위험을 함께 평가:** 분리한 검증 사건으로 Workflow 정확도, 과소·과잉 구성률, 근거 누락·OOD·적대적 로그·고위험 경계·언어별 성능을 본다. 확률 보정과 검토/기권 임계값을 새로 정한다. 기존 자기평가 confidence 임계값은 복사하지 않는다.
5. **종단간 효과로 채택 결정:** 공급자 속도·단가가 아니라 실제 사건 전체의 품질·지연·호출 수·추가 API 지출을 비교한다. 계획서의 비용 절감 목표를 달성했다고 선결론 내리지 않는다. 수용 기준과 로컬 기획서의 구현 범위를 먼저 합의한다.
6. **안전 경계 고정:** HIGH_RISK 규칙·하향 금지·근거 인용·독립 검증·LangGraph·executor·RBAC/admission을 보존한다. API 장애·인증 실패·한도·비정상 분포는 안전 이관/기권 경로로 처리하고, 무승인 유료 fallback은 금지한다.

## 8. 이번 작업의 완료 범위

- 공식 제품 정체, 기능과 한계, 가격·인증·데이터 경계를 확인한 사실과 LiveProbe 도입 권고로 분리했다.
- **이 문서만 작성했다.** 구현 stub·의존성·API 키·provider 설정·README 변경은 추가하지 않았다. 기존의 동시 작업 변경에는 손대지 않았다.
- 새 기술의 실제 API 호출·성능 시험·배포 검증은 **미수행**이다. 공개 문서의 기능 확인을 LiveProbe 통합 완료나 도입 승인으로 표현하지 않는다.
