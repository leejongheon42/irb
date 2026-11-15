"""Vocabulary for a public IRB submission."""
from typing import Final

CATEGORIES: Final = {"human": "인간대상연구", "specimen": "인체유래물연구", "both": "인간대상·인체유래물",
                     "stem_cell": "배아줄기세포주 이용연구"}
METHODS: Final = {"survey": "조사", "intervention": "개입", "records": "기존 정보 이용",
                  "specimen": "인체유래물 이용", "stem_cell": "배아줄기세포주 이용", "collect": "신규 직접 수집"}
ACCESS_ROUTES: Final = {"agreement": "기관협약", "individual": "개인연구자", "heads_agreement": "기관장 합의"}
CONSENT_ROUTES: Final = {"obtain": "동의 취득", "waiver_request": "서면동의면제 요청",
                        "existing_consent": "기존 동의 이용", "not_applicable": "동의 비적용"}
STUDY_DEFAULTS: Final = {"survey": ("human", ["survey"]),
                         "retrospective_chart_review": ("human", ["records"]),
                         "cdw_analysis": ("human", ["records"])}
