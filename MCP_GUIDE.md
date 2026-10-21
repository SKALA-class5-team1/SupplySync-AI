# business-mcp 사용 가이드 (Agent 개발자용)

내 SG(서브그래프) 코드에서 business-mcp의 Tool(모델 조회, BOM, 재고 확인, 원가, 견적 저장 등)을 쓰는 방법.
MCP를 몰라도 이 문서만 따라 하면 된다.

- Tool별 입출력·계산 규칙 원본: **SupplySync-Business-MCP 레포의 `CLAUDE.md`**
- 출력 필드명 기준: 제공받은 dataset v2.2의 `05_expected/` 정답 JSON

---

## 1. MCP가 뭔가요? (30초 요약)

**MCP(Model Context Protocol)** 는 AI Agent가 외부 서버의 기능(Tool)을 표준 방식으로 불러 쓰는 규칙이다.
REST API처럼 URL·경로를 하나하나 맞출 필요 없이, 서버가 "내가 가진 Tool 목록과 입력 형식"을 알려주고 Agent는 이름과 인자만 넘기면 된다.

```
내 SG 노드 (LangGraph)
   │  call_business_tool("get_bom", {...})        ← 우리가 쓰는 함수
   ▼
app/core/mcp_client.py  (공통 MCP 클라이언트, 이미 만들어져 있음)
   │  HTTP + 헤더 X-Internal-Api-Key
   ▼
business-mcp 서버 (:8101/mcp)   ← DB에 직접 접근하지 않음
   │  HTTP /internal/** + 헤더 X-Internal-Api-Key
   ▼
Spring 백엔드 → PostgreSQL (Supabase)
   (조회 Tool 6개, 쓰기 Tool 3개, RFQ 원문 모두 백엔드를 거친다)
```

---

## 2. 클라이언트는 SG마다 만드나요? → **아니요, 하나를 같이 씁니다**

MCP 클라이언트는 `app/core/mcp_client.py`에 **하나만** 있고, 모든 SG가 이것을 import 한다.
**각자 `MultiServerMCPClient(...)`를 새로 만들지 않는다.**

| 이유 | 설명 |
|---|---|
| 설정을 한 곳에서 관리 | 서버 주소·인증 키·접두어 규칙이 바뀌면 이 파일 하나만 고치면 된다 |
| 실수 방지 | 각자 만들면 `/mcp`를 두 번 붙이거나, 다른 키를 넣는 실수가 생긴다 |
| 상태가 없음 | 클라이언트는 "어디로 어떻게 접속할지" 설정만 들고 있다. 호출할 때마다 연결을 새로 열고 닫으므로 여러 SG가 같이 써도 서로 영향이 없다 |

rag-mcp도 서버가 준비되면 같은 파일에 `"rag"` 서버로 추가한다 (Tool 이름이 `rag_search_docs`가 됨).

---

## 3. 처음 한 번 준비

### 3-1. 패키지 설치 (SupplySync-AI 폴더)
```bash
.venv/bin/pip install -r requirements.txt
```

### 3-2. `.env` 설정 (SupplySync-AI 폴더)
```
BUSINESS_MCP_URL=http://127.0.0.1:8101/mcp
INTERNAL_API_KEY=<팀 공용 내부 호출 키>
```
- URL은 **`/mcp`까지 포함**한다. 코드에서 `/mcp`를 또 붙이지 않는다.
- `INTERNAL_API_KEY`는 AI 서버·business-mcp·백엔드가 **같은 값 하나**를 공유한다 (내부 호출 공용 키). 키 값은 팀에서 따로 공유받는다. Git·메신저 공개 채널에 올리지 않는다.

### 3-3. 백엔드와 business-mcp 서버 실행
business-mcp는 모든 데이터를 Spring 백엔드에서 가져오므로 **백엔드를 먼저** 띄운다.
```bash
# SupplySync-BE 폴더: v2.2 데모 시드까지 적재 (그 레포 README 참고)
SPRING_PROFILES_ACTIVE=demo ./gradlew bootRun

# SupplySync-Business-MCP 폴더
mvn spring-boot:run
```
`Started BusinessMcpApplication`이 보이면 준비 완료. (JDK 21, Maven 필요. 서버 쪽 `.env`의 `BACKEND_URL`·`INTERNAL_API_KEY`는 그 레포 README 참고)

### 3-4. 연결 확인 (SupplySync-AI 폴더)
```bash
LIVE_MCP=true .venv/bin/python -m pytest tests/core/test_business_mcp_live.py -v
```
4개가 통과하면 연결 성공.

---

## 4. 사용법 A: 코드에서 직접 호출 (대부분 이 방법)

**숫자(단가·원가·수량·날짜)를 코드로 계산하는 노드**는 이 방법을 쓴다.

```python
from app.core.mcp_client import McpToolError, call_business_tool

async def draft_quote(state):
    # Tool 이름은 접두어 없이 (서버 쪽 이름)
    options = await call_business_tool("get_options", {"model_id": "EWP-12-B"})
    cost = await call_business_tool("get_parts_cost", {"model_id": "EWP-12-B", "options": ["OPT-HT", "OPT-IP9K"]})

    unit_price = options["base_price_krw"] + sum(o["add_price_krw"] for o in options["options"])  # 37,300
    parts_cost = cost["parts_cost_krw"]                                                           # 21,650
    ...
```

- 반환값은 Tool 결과 JSON을 그대로 바꾼 **dict / list** 이다. 필드명은 snake_case (CLAUDE.md·정답 JSON과 같음).
- 잘못된 입력이면 **`McpToolError` 예외**가 난다. 메시지에 원인이 들어 있다.

```python
try:
    bom = await call_business_tool("get_bom", {"model_id": "EWP-12-A", "options": ["OPT-IP9K"], "qty": 10})
except McpToolError as e:
    # "get_bom: 모델 EWP-12-A에 허용되지 않은 옵션입니다: OPT-IP9K (허용: [OPT-HT])"
    ...
```

### SG2 예시: BOM → 재고 확인
```python
bom = await call_business_tool("get_bom", {"model_id": "EWP-12-B", "options": ["OPT-HT", "OPT-IP9K"], "qty": 2000})
parts = [{"part_id": p["part_id"], "required_qty": p["required_qty"]} for p in bom["parts"]]

supply = await call_business_tool("check_parts_supply", {
    "due_date": "2026-12-15",
    "base_date": "2026-10-19",   # 생략하면 오늘(Asia/Seoul)
    "parts": parts,
})
shortages = [p for p in supply["parts"] if p["status"] == "SHORT"]   # P-MCU-G1, short_qty 500
```

---

## 5. 사용법 B: LLM에게 Tool 선택을 맡기기

LLM이 상황을 보고 어떤 Tool을 부를지 정하게 하려면 Tool 목록을 LLM에 넘긴다.
이때 Tool 이름에는 **`business_` 접두어**가 붙는다 (예: `business_get_bom`).

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode

from app.core.mcp_client import get_business_tools

tools = await get_business_tools()                 # {"business_get_bom": Tool, ...}
selected = [tools["business_get_models"], tools["business_get_options"]]   # 이 노드에 필요한 것만

llm = ChatOpenAI(model="...").bind_tools(selected)  # LLM이 Tool 호출을 결정
tool_node = ToolNode(selected)                      # LLM이 고른 Tool을 실제로 실행하는 노드
```

그래프에 연결하는 기본 모양 (LLM → 필요하면 Tool 실행 → 다시 LLM):

```python
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition

async def call_llm(state: MessagesState):
    return {"messages": [await llm.ainvoke(state["messages"])]}

graph = StateGraph(MessagesState)
graph.add_node("llm", call_llm)
graph.add_node("tools", tool_node)
graph.add_edge(START, "llm")
graph.add_conditional_edges("llm", tools_condition)  # Tool 호출이 있으면 "tools", 없으면 종료
graph.add_edge("tools", "llm")                        # Tool 결과를 들고 다시 LLM으로
app = graph.compile()
```

- 모든 Tool을 넘기지 말고 **그 노드에 필요한 것만** 넘긴다. 특히 쓰기 Tool(`business_save_quote`, `business_register_quote`)은 LLM 판단에 맡기지 말고 코드에서 조건을 확인한 뒤 `call_business_tool`로 부르는 것을 권장한다.
- 이 방식에서는 Tool 오류가 예외가 아니라 **오류 메시지 텍스트**로 LLM에게 전달된다 (MCP 표준). LLM이 메시지를 읽고 입력을 고쳐 다시 부르거나 사람에게 묻는다.
- 그래프 안에서 실행된 Tool 결과(`ToolMessage`)는 `status`가 `"success"` 또는 `"error"`라서 성공/실패를 구분할 수 있다. 다만 결과 내용이 텍스트 블록이라 JSON을 직접 꺼내야 하므로, **결과 값을 코드에서 계산에 쓸 거라면 사용법 A**가 간단하고 안전하다.
- `ToolNode`는 그래프 안에 넣어서 실행한다. 그래프 밖에서 `tool_node.ainvoke(...)`로 단독 실행하면 설정 오류가 난다.

---

## 6. Tool 목록

| Tool (A: 직접 호출 이름) | B: LLM용 이름 | 하는 일 | 쓰는 SG | 종류 |
|---|---|---|---|---|
| `get_models` | `business_get_models` | 표준 모델 사양·기본 단가 | SG1 | 읽기 |
| `get_options` | `business_get_options` | 모델의 옵션·옵션 단가·교체 부품 | SG1, SG3 | 읽기 |
| `get_bom` | `business_get_bom` | 모델+옵션 → 부품별 필요 수량 | SG2, SG4 | 읽기 |
| `check_parts_supply` | `business_check_parts_supply` | 재고·부족·입고·위험 코드·대체 부품 | SG2, SG4 | 읽기 |
| `get_parts_cost` | `business_get_parts_cost` | 부품 원가 합계·내역 | SG3 | 읽기 |
| `get_substitutes` | `business_get_substitutes` | 부품 하나의 대체 부품 후보 (`check_parts_supply`의 `substitutes`와 같은 형식) | SG2 | 읽기 |
| `get_rfq_content` | `business_get_rfq_content` | RFQ 원문 | SG1 | 읽기 |
| `save_review` | `business_save_review` | SG1·SG2 결과 저장 (`status`, SG1 `demand`, 항목별 `evidence`) | SG1, SG2 | 쓰기 |
| `save_quote` | `business_save_quote` | 견적 초안 생성 (DRAFT, `revision`별. 같은 revision이면 기존 견적 반환) | SG3 | 쓰기 |
| `register_quote` | `business_register_quote` | 승인된 견적 등록 (**되돌리기 어려움, 사람 승인 후에만**) | SG4 | 쓰기 |

입력·출력 필드는 SupplySync-Business-MCP의 `CLAUDE.md` "Tool 명세"를 본다.

> 모든 Tool이 Spring 백엔드(`/internal/**`)를 호출하므로 **백엔드가 떠 있어야 동작한다** (live 테스트는 백엔드를 `SPRING_PROFILES_ACTIVE=demo`로 실행).
> 백엔드가 꺼져 있으면 "백엔드 오류: 연결 실패 …" `McpToolError`가 난다.

---

## 7. 오류 처리 규칙

business-mcp는 [MCP 공식 명세](https://modelcontextprotocol.io/specification)대로 오류를 돌려준다.

- `error_code` 같은 코드는 없다. **원인 메시지(한국어 텍스트)** 만 온다.
- 백엔드 오류는 메시지에 HTTP 상태 코드가 들어 있다. 예: `"백엔드 오류 (503): ..."`
- business-mcp는 재시도하지 않는다. 재시도 여부는 Agent 쪽이 판단한다.

| 메시지 예 | 의미 | 대응 |
|---|---|---|
| `존재하지 않는 모델입니다: …` / `허용되지 않은 옵션입니다: …` | 입력이 잘못됨 | 입력을 고친다 (재시도 무의미) |
| `현재 상태에서 불가: 승인되지 않은 견적입니다.` | 상태 충돌 (409) | 사람 승인 흐름 확인 |
| `백엔드 오류 (503)` / `연결 실패 또는 응답 시간 초과` | 일시 장애 | 잠시 후 재시도 가능 |

---

## 8. 자주 겪는 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| `Connection refused` / 연결 실패 | business-mcp 서버가 꺼져 있음 | 3-3대로 서버 실행 |
| `RuntimeError: .env의 INTERNAL_API_KEY가 설정되지 않았습니다` | `.env`에 키가 없거나 예시 값(`changeme-internal-key`) 그대로 | 3-2대로 키 설정 |
| `401 Unauthorized` | `INTERNAL_API_KEY`가 business-mcp의 `INTERNAL_API_KEY`와 다름, 또는 키를 바꾼 뒤 서버를 재시작하지 않음 | 값 확인 후 business-mcp 재시작 |
| `404` 또는 Tool 목록이 안 옴 | URL에 `/mcp`가 빠졌거나 `/mcp/mcp`로 두 번 붙음 | `BUSINESS_MCP_URL=http://127.0.0.1:8101/mcp` 그대로 쓰고 코드에서 덧붙이지 않기 |
| `McpToolError: … 백엔드 오류` | 백엔드 Tool인데 Spring 백엔드가 없음 | 정상. 백엔드 API 준비 후 동작 |
| `KeyError: 'business_get_bom'` | 사용법 A/B 이름을 섞어 씀 | A(`call_business_tool`)는 접두어 없이, B(`get_business_tools`)는 `business_` 붙여서 |

---

## 9. 체크리스트

- [ ] `.env`에 `BUSINESS_MCP_URL`, `INTERNAL_API_KEY` 설정
- [ ] 백엔드(demo)·business-mcp 서버 실행 후 `LIVE_MCP=true … test_business_mcp_live.py` 통과
- [ ] 클라이언트를 새로 만들지 않고 `app.core.mcp_client`에서 import
- [ ] 계산에 쓰는 값은 `call_business_tool` (사용법 A)
- [ ] 쓰기 Tool은 코드에서 조건 확인 후 호출. `register_quote`는 사람 승인 후에만
