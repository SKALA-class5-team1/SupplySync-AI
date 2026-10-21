"""
rules.yaml 기반 결정적 판정 (LLM 사용 금지).

- validate(): 누락·범위·계산 일관성·날짜 순서·계약 한도 → issues, passed_checks, normalized_spec
- match_models(): spec_compare 기준 모델 비교 + 허용 옵션 적용
판정과 숫자 계산은 모두 rules.yaml 값과 도구 응답 값으로만 합니다.
"""
import copy
import json
import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from app.rfq_review.state import ISSUE_LABELS, Issue

DATASET_DIR = Path(__file__).resolve().parents[2] / "data" / "dataset_SG1"
RULES_PATH = DATASET_DIR / "rules.yaml"
RFQ_SCHEMA_PATH = DATASET_DIR / "rfq_schema.json"

SEVERITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}

# 메시지·질의서용 한글 항목명
FIELD_LABELS = {
    "issue_date": "발행일",
    "quote_due_date": "견적 회신 마감일",
    "volume.eau": "연간 물량",
    "volume.lifetime_volume": "총 공급 물량",
    "volume.sample_due_date": "시작품 납품일",
    "schedule.sop_date": "양산 시작일(SOP)",
    "schedule.ppap_date": "양산 승인(PPAP) 완료일",
    "spec.flow": "유량",
    "spec.flow_lpm": "유량",
    "spec.head_kpa": "수두",
    "spec.voltage_min": "최저 전압",
    "spec.voltage_max": "최고 전압",
    "spec.temp_min": "최저 온도",
    "spec.temp_max": "최고 온도",
    "spec.ip_rating": "방수 등급",
    "spec.noise_db_max": "소음",
    "spec.comm": "통신 방식",
    "spec.max_size_mm": "외형 크기",
    "spec.max_weight_kg": "무게",
    "spec.connector": "커넥터",
    "spec.coolant": "냉각수",
    "commercial.annual_cr_pct": "연간 가격 인하",
    "commercial.payment_days": "결제 기간",
    "contract.warranty": "품질 보증",
    "contract.warranty_years": "품질 보증 기간",
    "contract.warranty_km": "품질 보증 거리",
    "contract.penalty_cap_pct": "지연 배상 상한",
    "contract.eol_notice_months": "단종 통지",
}

# 값 표기용 단위
FIELD_UNITS = {
    "spec.flow_lpm": " L/min",
    "spec.head_kpa": " kPa",
    "spec.voltage_min": "V",
    "spec.voltage_max": "V",
    "spec.temp_min": "℃",
    "spec.temp_max": "℃",
    "spec.noise_db_max": " dB",
    "spec.max_weight_kg": " kg",
    "commercial.annual_cr_pct": "%",
    "commercial.payment_days": "일",
    "contract.warranty_years": "년",
    "contract.warranty_km": " km",
    "contract.penalty_cap_pct": "%",
    "contract.eol_notice_months": "개월",
}

# 범위 검사 필드 → 이슈에 표시할 필드 (환산값은 원래 필드로 표시)
RANGE_ISSUE_FIELD = {"spec.flow_lpm": "spec.flow"}


@lru_cache
def load_rules() -> dict:
    with RULES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def load_rfq_schema_text() -> str:
    return json.dumps(json.loads(RFQ_SCHEMA_PATH.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().upper() == "TBD"
    if isinstance(value, dict) and "value" in value:  # spec.flow
        return is_missing(value.get("value"))
    return False


def fmt_num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def fmt_value(field: str, value: Any) -> str:
    return f"{fmt_num(value)}{FIELD_UNITS.get(field, '')}"


def _to_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_quantity(text: str) -> tuple[Optional[float], Optional[str]]:
    """'22 L/min' → (22.0, 'L/min')"""
    m = re.match(r"^\s*(-?[\d,]+(?:\.\d+)?)\s*(.*?)\s*$", str(text))
    if not m:
        return None, None
    return float(m.group(1).replace(",", "")), (m.group(2) or None)


def _coerce(text: str) -> Any:
    """승인값 문자열을 숫자로 바꿀 수 있으면 숫자로."""
    try:
        num = float(str(text).replace(",", ""))
        return int(num) if num.is_integer() else num
    except ValueError:
        return text


def _flow_lpm(flow: Any) -> Optional[float]:
    if not isinstance(flow, dict) or flow.get("value") is None:
        return None
    factors = load_rules()["unit_conversion"]["flow"]["factors"]
    factor = factors.get(flow.get("unit"))
    if factor is None:
        return None
    return round(float(flow["value"]) * factor, 4)


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------
def effective_rfq(parsed: dict, assumptions: list[dict]) -> dict:
    """원문은 보존하고 담당자가 확인한 물량·일정 등만 실행 값에 반영한다."""
    effective = copy.deepcopy(parsed)
    for assumption in assumptions:
        path = assumption.get("field", "")
        if assumption.get("approved_by") is None or path.startswith("spec."):
            continue
        keys = path.split(".")
        if not keys or keys[0] not in effective:
            continue
        target = effective
        for key in keys[:-1]:
            if not isinstance(target.get(key), dict):
                target[key] = {}
            target = target[key]
        target[keys[-1]] = _coerce(assumption["value"])
    return effective


def normalize_spec(parsed: dict, assumptions: list[dict]) -> dict:
    """원본 spec + 단위 환산(flow_lpm) + 가정값 반영. parsed_rfq 원본은 바꾸지 않습니다."""
    spec = copy.deepcopy(parsed.get("spec") or {})
    for a in assumptions:
        field = a.get("field", "")
        if not field.startswith("spec."):
            continue
        key = field.split(".", 1)[1]
        if key == "flow":
            value, unit = _parse_quantity(a["value"])
            spec["flow"] = {"value": value, "unit": unit}
        else:
            spec[key] = _coerce(a["value"])
    spec["flow_lpm"] = _flow_lpm(spec.get("flow"))
    return spec


def _range_value(field: str, parsed: dict, spec: dict) -> Any:
    if field.startswith("spec."):
        return spec.get(field.split(".", 1)[1])
    return get_path(parsed, field)


def _range_violations(parsed: dict, spec: dict) -> set[str]:
    out = set()
    for r in load_rules()["ranges"]:
        value = _range_value(r["field"], parsed, spec)
        if value is not None and not (r["min"] <= value <= r["max"]):
            out.add(RANGE_ISSUE_FIELD.get(r["field"], r["field"]))
    return out


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------
def _check_required(parsed: dict) -> list[dict]:
    issues = []
    for r in load_rules()["required_fields"]:
        value = get_path(parsed, r["field"])
        if not is_missing(value):
            continue
        default = r.get("default_assumption")
        raw = value.get("value") if isinstance(value, dict) else value
        issues.append({
            "type": "MISSING",
            "field": r["field"],
            "severity": r["severity"],
            "found": "TBD" if isinstance(raw, str) and raw.strip() else "미기재",
            "reason": f"{label(r['field'])} 값이 없음",
            "suggestion": f"{default}로 가정, 고객 확인" if default else "담당자 확인 후 고객 확인",
            "proposed_value": default,
        })
    return issues


def _check_ranges(parsed: dict, spec: dict) -> tuple[list[dict], list[str]]:
    rules = load_rules()
    flow_rule = rules["unit_conversion"]["flow"]
    target_unit = flow_rule["target_unit"]
    issues, passed = [], []

    for r in rules["ranges"]:
        field = r["field"]
        issue_field = RANGE_ISSUE_FIELD.get(field, field)
        lo, hi = r["min"], r["max"]
        range_text = f"{fmt_num(lo)}~{fmt_num(hi)}{FIELD_UNITS.get(field, '')}"

        if field == "spec.flow_lpm":
            flow = spec.get("flow")
            if is_missing(flow):
                continue  # 누락 검사에서 처리
            if flow.get("unit") not in flow_rule["factors"]:
                issues.append({
                    "type": "OUTLIER", "field": issue_field, "severity": r["severity"],
                    "found": f"{fmt_num(flow['value'])} {flow.get('unit')}",
                    "reason": f"환산할 수 없는 단위 (허용: {', '.join(flow_rule['factors'])})",
                    "suggestion": "담당자 확인",
                })
                continue

        value = _range_value(field, parsed, spec)
        if value is None:
            continue
        if lo <= value <= hi:
            passed.append(f"{label(field)} {fmt_value(field, value)} 정상 범위 ({range_text})")
            continue

        issue = {
            "type": "OUTLIER", "field": issue_field, "severity": r["severity"],
            "found": fmt_value(field, value),
            "reason": f"정상 범위 {range_text}을 벗어남",
            "suggestion": "고객 확인",
        }
        if field == "spec.flow_lpm":
            flow = spec["flow"]
            issue["found"] = f"{fmt_num(flow['value'])} {flow['unit']} (= {fmt_num(round(value, 2))} {target_unit})"
            # 원래 숫자를 목표 단위로 읽으면 정상 범위 → 단위 오기로 보고 그 값을 제안
            if flow["unit"] != target_unit and lo <= float(flow["value"]) <= hi:
                proposed = f"{fmt_num(flow['value'])} {target_unit}"
                issue["reason"] += ". 단위 오기 의심"
                issue["suggestion"] = f"{proposed}으로 가정"
                issue["proposed_value"] = proposed
        issues.append(issue)
    return issues, passed


def _check_consistency(parsed: dict) -> list[dict]:
    issues = []
    for r in load_rules()["consistency"]:
        m = re.match(r"^\s*([\w.]+)\s*==\s*([\w.]+)\s*\*\s*([\w.]+)\s*$", r["rule"])
        if not m:
            continue
        left, a, b = m.groups()
        lv, av, bv = get_path(parsed, left), get_path(parsed, a), get_path(parsed, b)
        if None in (lv, av, bv) or lv == av * bv:
            continue
        issues.append({
            "type": "OUTLIER", "field": left, "severity": r["severity"],
            "found": fmt_num(lv),
            "reason": f"{fmt_num(av)} × {fmt_num(bv)} = {fmt_num(av * bv)}과 불일치",
            "suggestion": "고객 확인",
        })
    return issues


def _check_date_order(parsed: dict) -> list[dict]:
    issues = []
    for r in load_rules()["date_order"]:
        before, after = get_path(parsed, r["before"]), get_path(parsed, r["after"])
        bd, ad = _to_date(before), _to_date(after)
        if bd is None or ad is None or bd <= ad:
            continue
        issues.append({
            "type": "OUTLIER", "field": r["before"], "severity": r["severity"],
            "found": f"{label(r['before'])} {before} > {label(r['after'])} {after}",
            "reason": r.get("hint") or f"{label(r['before'])}이 {label(r['after'])}보다 늦음",
            "suggestion": "고객 확인",
        })
    return issues


def _check_policy_limits(parsed: dict) -> tuple[list[dict], list[str]]:
    """같은 조항(ref)의 위반은 한 이슈로 묶습니다 (예: 보증 연수·거리 → 제12조)."""
    groups: dict[str, list[dict]] = {}
    for r in load_rules()["policy_limits"]:
        groups.setdefault(r.get("ref") or r["field"], []).append(r)

    issues, passed = [], []
    for ref, rules in groups.items():
        found, limits, violated, severities = [], [], [], []
        for r in rules:
            value = get_path(parsed, r["field"])
            limit_text = fmt_value(r["field"], r["max"])
            if value is None:
                if r.get("null_is_violation"):
                    found.append("상한 없음")
                    limits.append(limit_text)
                    violated.append(r["field"])
                    severities.append(r["severity"])
                continue
            found.append(fmt_value(r["field"], value))
            limits.append(limit_text)
            if value > r["max"]:
                violated.append(r["field"])
                severities.append(r["severity"])

        if not found:
            continue
        found_text, limit_text = " / ".join(found), " / ".join(limits)
        fields = [r["field"] for r in rules]
        if not violated:
            name = label(fields[0]) if len(fields) == 1 else label(_common_field(fields))
            passed.append(f"{name} {found_text} (한도 {limit_text} 이내, {ref})")
            continue

        field = fields[0] if len(fields) == 1 else _common_field(fields)
        issues.append({
            "type": "CONTRACT_RISK", "field": field,
            "severity": min(severities, key=SEVERITY_ORDER.get),
            "found": found_text,
            "reason": f"사내 한도 {limit_text} 초과",
            "suggestion": f"사내 한도({limit_text}) 내로 조정 협의",
            "ref": ref,
            "limit": limit_text,
        })
    return issues, passed


def _common_field(fields: list[str]) -> str:
    """['contract.warranty_years', 'contract.warranty_km'] → 'contract.warranty'"""
    return os.path.commonprefix(fields).rstrip("_.") or fields[0]


def validate(parsed: dict, assumptions: list[dict]) -> tuple[list[dict], list[str], dict]:
    """
    원본 parsed_rfq로 이슈를 만들고, 승인된 가정값으로 해결 여부를 표시합니다.
    확정 후 재검증해도 이슈 목록은 유지되고 resolved만 바뀝니다.
    """
    raw_spec = normalize_spec(parsed, [])
    normalized = normalize_spec(parsed, assumptions)

    range_issues, range_passed = _check_ranges(parsed, raw_spec)
    policy_issues, policy_passed = _check_policy_limits(parsed)
    raw = (
        _check_required(parsed)
        + range_issues
        + _check_consistency(parsed)
        + _check_date_order(parsed)
        + policy_issues
    )
    raw.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])  # 안정 정렬: 같은 심각도는 규칙 순서 유지

    approved = {a["field"] for a in assumptions if a.get("approved_by") is not None}
    effective = effective_rfq(parsed, assumptions)
    effective["spec"] = normalized
    still_invalid = {
        i["field"] for i in (
            _check_required(effective) + _check_consistency(effective) + _check_date_order(effective)
        )
    } | _range_violations(effective, normalized)

    issues = []
    for n, item in enumerate(raw, start=1):
        issue = Issue(id=f"ISS-{n:02d}", label=ISSUE_LABELS[item["type"]], **item)
        issue.resolved = issue.field in approved and issue.field not in still_invalid
        issues.append(issue.model_dump())
    return issues, policy_passed + range_passed, normalized


def has_unresolved_high(issues: list[dict]) -> bool:
    return any(i["severity"] == "HIGH" and not i.get("resolved") for i in issues)


# ---------------------------------------------------------------------------
# 사양 매칭
# ---------------------------------------------------------------------------
def _requirement(spec: dict, req_field: str) -> Any:
    return spec.get(req_field.split(".", 1)[1])


def _meets(op: str, model_value: Any, req: Any, ip_rank: dict) -> bool:
    if op == ">=":
        return float(model_value) >= float(req)
    if op == "<=":
        return float(model_value) <= float(req)
    if op == "rank>=":
        m, r = ip_rank.get(str(model_value)), ip_rank.get(str(req))
        return m is not None and r is not None and m >= r
    if op == "contains":
        wanted = str(req).split()[0].upper()  # 'LIN 2.2' → 'LIN'
        return wanted in [t.strip().upper() for t in str(model_value).split(",")]
    if op == "each<=":
        return all(float(mv) <= float(rv) for mv, rv in zip(model_value, req))
    raise ValueError(f"알 수 없는 비교 연산: {op}")


def _model_value(eff: dict, model_field: str) -> Any:
    if "," in model_field:
        return [eff[f.strip()] for f in model_field.split(",")]
    return eff[model_field]


def _req_value(req: Any, op: str) -> Any:
    if op == "each<=" and isinstance(req, dict):
        return [req.get("l"), req.get("w"), req.get("h")]
    return req


def _compare(eff: dict, spec: dict) -> list[dict]:
    """미달 항목 목록. 요구값이 없는 항목은 비교하지 않습니다."""
    rules = load_rules()
    fails = []
    for r in rules["spec_compare"]:
        req = _req_value(_requirement(spec, r["req"]), r["op"])
        if req is None or (isinstance(req, list) and None in req):
            continue
        mv = _model_value(eff, r["model"])
        if not _meets(r["op"], mv, req, rules["ip_rank"]):
            fails.append({"rule": r, "model_value": mv, "req": req})
    return fails


def _fail_text(fail: dict, note: str = "") -> str:
    r = fail["rule"]
    name = label(r["req"])
    mv, req = fail["model_value"], fail["req"]
    if isinstance(mv, list):
        mv, req = "×".join(fmt_num(v) for v in mv), "×".join(fmt_num(v) for v in req)
    else:
        mv, req = fmt_num(mv), fmt_num(req)
    text = f"{name}: 모델 {mv} / 요구 {req} ({r['op']})"
    return f"{text} — {note}" if note else text


def _evaluate_model(model: dict, allowed: list[dict], spec: dict) -> dict:
    eff = dict(model)
    price = int(model["base_price_krw"])
    breakdown = [fmt_num(price)]
    applied: list[str] = []
    allowed_by_code = {o["option_code"]: o for o in allowed}

    unfixable, unfixable_reqs = [], set()
    for fail in _compare(eff, spec):
        code = fail["rule"].get("fixable_by")
        option = allowed_by_code.get(code) if code else None
        if option is None:
            note = f"{code} 옵션 적용 불가" if code else "옵션으로 해결 불가"
            unfixable.append(_fail_text(fail, note))
            unfixable_reqs.add(fail["rule"]["req"])
            continue
        if code in applied:
            continue
        field = option["changes_field"]
        eff[field] = _coerce(option["new_value"]) if isinstance(model.get(field), (int, float)) else option["new_value"]
        eff["weight_kg"] = round(float(eff["weight_kg"]) + float(option.get("add_weight_kg") or 0), 3)
        eff["noise_db"] = float(eff["noise_db"]) + float(option.get("add_noise_db") or 0)
        price += int(option["add_price_krw"])
        breakdown.append(f"{fmt_num(int(option['add_price_krw']))} ({code})")
        applied.append(code)

    # 옵션 적용 후 무게·소음 등이 바뀌므로 전체를 다시 비교
    fails = unfixable + [_fail_text(f) for f in _compare(eff, spec) if f["rule"]["req"] not in unfixable_reqs]

    return {
        "model_id": model["model_id"],
        "model_name": model.get("model_name"),
        "passed": not fails,
        "fails": fails,
        "options": applied,
        "price_krw": price,
        "price_breakdown": " + ".join(breakdown),
        "effective_spec": {
            k: eff[k] for k in ("flow_lpm", "head_kpa", "temp_max", "ip_rating", "noise_db", "weight_kg")
        },
    }


def match_models(spec: dict, models: list[dict], options_by_model: dict[str, list[dict]],
                 target_price_krw: Optional[int]) -> dict:
    candidates = [_evaluate_model(m, options_by_model.get(m["model_id"], []), spec) for m in models]
    passing = sorted((c for c in candidates if c["passed"]), key=lambda c: (len(c["options"]), c["price_krw"]))
    selected = passing[0] if passing else None

    for c in candidates:
        if c is selected:
            c["result"] = "추천"
        else:
            c["result"] = "적합" if c["passed"] else "탈락"
        if target_price_krw:
            within = c["price_krw"] <= target_price_krw
            c["within_target_price"] = within
            c["note"] = f"목표 단가 {fmt_num(target_price_krw)}원 {'이내' if within else '초과'}"

    if selected is None:
        fit_type = "신규 개발"
    else:
        fit_type = "파생 사양" if selected["options"] else "표준"
    if selected:
        selected["fit_type"] = fit_type

    requirement_used = {
        r["req"].split(".", 1)[1]: _requirement(spec, r["req"]) for r in load_rules()["spec_compare"]
    }
    return {
        "requirement_used": requirement_used,
        "candidates": candidates,
        "selected_model": selected["model_id"] if selected else None,
        "options": selected["options"] if selected else [],
        "fit_type": fit_type,
        "price_krw": selected["price_krw"] if selected else None,
        "target_price_krw": target_price_krw,
    }
