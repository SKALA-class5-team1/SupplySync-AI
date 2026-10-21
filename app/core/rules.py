"""rules.yaml 로더. 판정 기준(한도·공식 파라미터)은 이 파일 값만 쓴다.

파일은 프로젝트 루트의 data/dataset_SG1/rules.yaml (팀 공용, Git으로 관리).
"""
from functools import lru_cache
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parents[2] / "data" / "dataset_SG1" / "rules.yaml"


@lru_cache
def load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
