---
title: 입고 대안 선택은 예상 배치와 다음 예약에도 반영해야 한다
date: 2026-11-21
category: runtime
tags: [inbound, placement, reservations, audit]
agent: codex
---

## Symptom
입고 계획의 추천 대신 대안을 결정하면, 다음 입고가 그 대안 자리를 추천할 수 있다.
화면도 계속 계획의 추천만 읽으면 실제로 결정한 자리와 다르게 보인다.

## Root cause
계획은 불변이며 `plan.result['recommendation']`은 알고리즘의 최초 추천이다.
`placement_reservations`와 배치 화면은 이 추천을 직접 읽고 있었다.
대안은 같은 계획 안에서 유효해도 다른 입고가 이미 결정한 자리와 겹칠 수 있다.

## Fix
`inbound_decision.decide_placement`가 대안 순번과 선택한 배치를 감사 기록에 고정한다.
기존 `detail['recommendation']`에 선택한 배치를 저장해 `projected_snapshot`과 이전 기록의 호환을 유지한다.
`autoplanning.placement_reservations`와 `placement_view.placement_context`도 결정 기록을 읽는다.
결정 트랜잭션 안에서 예상 배치의 기존 공간 검증을 재사용해 충돌이면 감사 기록까지 롤백한다.

## Prevention
`harbor_web/tests/test_inbound_alternative.py`에서 대안 선택 → 충돌 결정 거부 →
재계산 → 결정된 자리를 예약으로 잡아 후보에서 제외 → 두 입고 예상 배치까지 검증한다.
동일 선택 재전송은 멱등이고, 다른 선택 재전송은 기존 결정을 바꾸지 않는지도 확인한다.
