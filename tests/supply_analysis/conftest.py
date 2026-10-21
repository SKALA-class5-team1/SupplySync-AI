"""명세 예시와 실제 RAG 조회 원문. 실행 코드는 이 테스트 데이터를 읽지 않는다."""

from copy import deepcopy

import pytest

from app.supply_analysis import deps

ALT_DOCS = [
    {"chunk_id": "alt-001", "section": "1. 대체 부품 검토 조건", "content": "핵심 부품의 가용 재고가 필요 수량보다 적고, 입고 예정일이 고객 납기보다 늦으면 대체 부품을 검토한다. 대체 부품은 원래 부품과 같은 성능 등급 이상이어야 한다."},
    {"chunk_id": "alt-002", "section": "2. 제어칩 대체", "content": "고온용 제어칩(P-MCU-G1)의 대체품은 누리칩스의 P-MCU-G1-ALT다. 같은 고온 등급이며 핀 배치가 같아 회로 변경이 필요 없다. 다만 처음 쓰는 공급사이므로 적용 전 2주간의 검증 시험이 필요하고, 개당 원가가 300원 높다."},
    {"chunk_id": "alt-003", "section": "3. 대안 비교 기준", "content": "대안은 납기 충족 여부, 추가 원가, 검증 기간, 품질 위험 순으로 비교한다. 납기를 맞출 수 없는 대안은 다른 조건이 좋아도 우선순위에서 밀린다."},
]
POLICY_DOCS = [
    {"chunk_id": "pol-004", "section": "제13조 지연 배상", "content": "납기 지연에 대한 배상 책임은 해당 계약 금액의 10%를 상한으로 한다. 배상 상한이 없거나 10%를 초과하는 조항은 수용할 수 없으며, 고객과 상한 설정을 협의해야 한다."},
    {"chunk_id": "pol-009", "section": "제20조 신규 공급사 부품", "content": "처음 거래하는 공급사의 부품은 검증 시험을 통과한 뒤에만 생산에 투입한다. 대체 부품을 사용하는 경우 견적 조건에 그 사실을 명시해 고객에게 알린다."},
    {"chunk_id": "pol-010", "section": "제21조 대체 부품 원가", "content": "대체 부품 사용으로 부품 원가가 오르는 경우, 증가분은 견적 단가에 포함하지 않고 견적 조건에 별도 비용으로 표시한다."},
]


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.failures = {}
        self.alt_docs = deepcopy(ALT_DOCS)
        self.policy_docs = deepcopy(POLICY_DOCS)
        self.bom = {
            "model_id": "EWP-12-B", "options": ["OPT-HT", "OPT-IP9K"], "qty": 2000,
            "parts": [
                {"part_id": pid, "part_name": name, "qty_per_unit": 1, "required_qty": 2000, "source": source}
                for pid, name, source in [
                    ("P-MTR-BL12", "모터", "BASE"), ("P-MCU-G1", "제어칩 (고온용)", "OPTION:OPT-HT"),
                    ("P-IMP-25", "임펠러", "BASE"), ("P-HSG-AL", "알루미늄 케이스", "BASE"),
                    ("P-SEAL-9K", "방수 실링 (강화)", "OPTION:OPT-IP9K"), ("P-CON-4P", "4핀 커넥터", "BASE"),
                ]
            ],
        }
        self.supply = {
            "base_date": "2026-10-19", "due_date": "2026-12-15", "has_risk": True,
            "parts": [
                {"part_id": part["part_id"], "part_name": part["part_name"], "required_qty": 2000,
                 "available": 1500 if part["part_id"] == "P-MCU-G1" else 5000,
                 "short_qty": 500 if part["part_id"] == "P-MCU-G1" else 0,
                 "status": "SHORT" if part["part_id"] == "P-MCU-G1" else "OK",
                 "incoming_qty": 5000, "incoming_date": "2027-01-20",
                 "incoming_before_due": False, "incoming_delay_days": 36,
                 "new_order_arrival": "2027-01-17", "lead_time_days": 90}
                for part in reversed(self.bom["parts"])
            ],
        }
        self.substitutes = {"P-MCU-G1": {
            "part_id": "P-MCU-G1", "part_name": "제어칩 (고온용)", "unit_cost_krw": 5300,
            "substitutes": [{"substitute_part_id": "P-MCU-G1-ALT", "part_name": "제어칩 (고온용, 대체)",
                "supplier": "누리칩스", "available": 3000, "covers_shortage": True,
                "unit_cost_krw": 5600, "unit_cost_diff_krw": 300, "validation_days": 14,
                "usable_date": "2026-11-02", "lead_time_days": 30, "pin_compatible": True,
                "ref_chunk_id": "alt-002"}],
        }}

    @property
    def chip(self):
        return next(p for p in self.supply["parts"] if p["part_id"] == "P-MCU-G1")

    @property
    def substitute(self):
        return self.substitutes["P-MCU-G1"]["substitutes"][0]

    async def call(self, name, **arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name in self.failures:
            raise self.failures[name]
        if name == "business_get_bom":
            result = self.bom
        elif name == "business_check_parts_supply":
            result = self.supply
        elif name == "business_get_substitutes":
            result = self.substitutes[arguments["part_id"]]
        elif name == "rag_search_docs":
            result = self.alt_docs if arguments["doc_type"] == "alt_parts" else self.policy_docs
        elif name == "business_save_review":
            result = {"review_id": 1, "thread_id": arguments["thread_id"], "status": "WAITING"}
        else:
            raise AssertionError(name)
        return deepcopy(result)


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(deps, "gateway", lambda: fake)
    return fake


@pytest.fixture
def bom_plan():
    return {
        "P-CON-4P": 2000, "P-HSG-AL": 2000, "P-IMP-25": 2000,
        "P-MCU-G1": 2000, "P-MTR-BL12": 2000, "P-SEAL-9K": 2000,
    }


@pytest.fixture
def substitute_plan(bom_plan):
    return {**bom_plan, "P-MCU-G1": 1500, "P-MCU-G1-ALT": 500}


@pytest.fixture
def request_data():
    return {
        "thread_id": "th_sg2_test", "rfq_number": "HB-EV7-EWP-001", "review_id": 1,
        "selected_model": "EWP-12-B", "options": ["OPT-HT", "OPT-IP9K"],
        "demand": {"sample_qty": 2000, "sample_due_date": "2026-12-15"},
        "base_date": "2026-10-19", "contract_risks": [{"item": "지연 배상 상한 없음", "ref": "제13조"}],
    }
