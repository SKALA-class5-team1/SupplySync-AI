"""SG3가 쓰는 외부 데이터 접근

노드는 QuoteDataSource 인터페이스만 안다. 실제 서버는 McpQuoteDataSource,
MCP 서버·백엔드가 준비되기 전에는 같은 입출력의 MockQuoteDataSource(v2.2 시드 값)를 쓴다.
입출력 필드명은 app/quote_drafting/CLAUDE.md "SG3가 쓰는 도구 입출력"과 같다.
"""
import logging
from typing import Protocol

from app.core.mcp_client import McpToolError, call_business_tool

logger = logging.getLogger(__name__)


class QuoteDataSource(Protocol):
    async def get_options(self, model_id: str) -> dict: ...

    async def get_parts_cost(self, model_id: str, options: list[str]) -> dict: ...

    async def search_docs(self, query: str, doc_type: str | None = None, k: int = 4) -> list[dict]: ...

    async def save_quote(self, payload: dict) -> dict: ...


class McpQuoteDataSource:
    """business-mcp 실제 호출. Tool 오류는 McpToolError로 올라온다."""

    async def get_options(self, model_id: str) -> dict:
        return await call_business_tool("get_options", {"model_id": model_id})

    async def get_parts_cost(self, model_id: str, options: list[str]) -> dict:
        return await call_business_tool("get_parts_cost", {"model_id": model_id, "options": options})

    async def search_docs(self, query: str, doc_type: str | None = None, k: int = 4) -> list[dict]:
        # rag-mcp가 공통 클라이언트에 연결되기 전까지는 근거 문서 없이 문구를 쓴다
        logger.warning("rag-mcp 미연결: search_docs(%r)는 빈 결과를 돌려줍니다.", query)
        return []

    async def save_quote(self, payload: dict) -> dict:
        return await call_business_tool("save_quote", payload)


# --- mock (dataset v2.2 02_seed.sql, 03_vectordb/chunks.jsonl 기준) ---

_MODELS = {"EWP-12-B": 34000}
_MODEL_OPTIONS = {"EWP-12-B": {"OPT-HT": 2400, "OPT-IP9K": 900}}
_PART_COST = {
    "P-MTR-BL12": 9500,
    "P-MCU-STD": 3200,
    "P-MCU-G1": 5300,
    "P-IMP-25": 1200,
    "P-HSG-AL": 4100,
    "P-SEAL-STD": 400,
    "P-SEAL-9K": 950,
    "P-CON-4P": 600,
}
_BASE_BOM = {"EWP-12-B": ["P-MTR-BL12", "P-MCU-STD", "P-IMP-25", "P-HSG-AL", "P-SEAL-STD", "P-CON-4P"]}
_OPTION_REPLACES = {"OPT-HT": ("P-MCU-STD", "P-MCU-G1"), "OPT-IP9K": ("P-SEAL-STD", "P-SEAL-9K")}
_CHUNKS = [
    {
        "chunk_id": "pol-003",
        "doc_type": "policy",
        "section": "제12조 품질 보증 기간",
        "content": "품질 보증 기간은 양산 시작일로부터 3년 또는 주행거리 100,000 km 중 먼저 도래하는 기간을 넘을 수 없다. "
        "이를 초과하는 보증 조건은 품질본부장의 승인이 필요하며, 견적 단가에 보증 비용을 반영해야 한다.",
    },
    {
        "chunk_id": "pol-004",
        "doc_type": "policy",
        "section": "제13조 지연 배상",
        "content": "납기 지연에 대한 배상 책임은 해당 계약 금액의 10%를 상한으로 한다. "
        "배상 상한이 없거나 10%를 초과하는 조항은 수용할 수 없으며, 고객과 상한 설정을 협의해야 한다.",
    },
    {
        "chunk_id": "pol-009",
        "doc_type": "policy",
        "section": "제20조 신규 공급사 부품",
        "content": "처음 거래하는 공급사의 부품은 검증 시험을 통과한 뒤에만 생산에 투입한다. "
        "대체 부품을 사용하는 경우 견적 조건에 그 사실을 명시해 고객에게 알린다.",
    },
    {
        "chunk_id": "pol-010",
        "doc_type": "policy",
        "section": "제21조 대체 부품 원가",
        "content": "대체 부품 사용으로 부품 원가가 오르는 경우, 증가분은 견적 단가에 포함하지 않고 견적 조건에 별도 비용으로 표시한다.",
    },
]


class MockQuoteDataSource:
    """business-mcp·rag-mcp mock. save_quote로 받은 payload는 saved에 쌓인다."""

    def __init__(self, part_cost_override: dict[str, int] | None = None, save_error: str | None = None):
        self.part_cost = {**_PART_COST, **(part_cost_override or {})}
        self.save_error = save_error
        self.saved: list[dict] = []

    async def get_options(self, model_id: str) -> dict:
        options = _MODEL_OPTIONS[model_id]
        return {
            "model_id": model_id,
            "base_price_krw": _MODELS[model_id],
            "options": [{"option_code": code, "add_price_krw": price} for code, price in options.items()],
        }

    async def get_parts_cost(self, model_id: str, options: list[str]) -> dict:
        parts = list(_BASE_BOM[model_id])
        for code in options:
            old, new = _OPTION_REPLACES[code]
            parts[parts.index(old)] = new
        breakdown = [
            {"part_id": p, "qty_per_unit": 1, "unit_cost_krw": self.part_cost[p], "cost_krw": self.part_cost[p]}
            for p in parts
        ]
        return {
            "model_id": model_id,
            "options": options,
            "parts_cost_krw": sum(row["cost_krw"] for row in breakdown),
            "parts_cost_breakdown": breakdown,
        }

    async def search_docs(self, query: str, doc_type: str | None = None, k: int = 4) -> list[dict]:
        # 임베딩 검색 대신 질의 단어가 조항 제목에 들어 있는 청크를 돌려준다
        words = query.split()
        hits = [
            c for c in _CHUNKS
            if (doc_type is None or c["doc_type"] == doc_type) and any(w in c["section"] for w in words)
        ]
        return [{**c, "score": 1.0} for c in hits[:k]]

    async def save_quote(self, payload: dict) -> dict:
        if self.save_error:
            raise McpToolError(f"save_quote: {self.save_error}")
        self.saved.append(payload)
        issue = payload["issue_date"].replace("-", "")
        return {"quote_id": f"Q-{issue[:4]}-{issue[4:]}-001", "status": "DRAFT"}
