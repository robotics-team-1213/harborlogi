"""Fixed synthetic R1 comparison; no device execution or physical timing."""
import json
import subprocess
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from statistics import median
from time import perf_counter

from harbor.layout import YardCell, YardLayout
from harbor.model import OutboundRequest
from harbor.spatial import Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot, plan_spatial


def fixture(vertical):
    rows, columns = (3, 1) if vertical else (1, 3)
    layout = YardLayout(rows, columns, 3, tuple(YardCell(f'Y{r}{c}', r, c)
        for r in range(1, rows + 1) for c in range(1, columns + 1)))
    source = SpatialSnapshot(4, datetime(2026, 11, 2, tzinfo=timezone.utc), layout,
        (Area('yard', rows, columns, 3), Area('temp', rows, columns, 1),
         Area('stand', 2 if vertical else 1, 1 if vertical else 2, 1)),
        tuple(CargoShape(cargo, length, 1) for cargo, length in [('target', 2), ('b', 2), ('a', 1)]),
        tuple(Placement(cargo, 'yard', 1, 1, level, 90 if vertical else 0)
              for cargo, level in [('target', 1), ('b', 2), ('a', 3)]))
    policy = SpatialPolicy('stand', ('temp',),
        frozenset({('yard', 'temp'), ('temp', 'yard'), ('yard', 'stand')}),
        frozenset(('A', place) for place in ('temp', 'stand', *(c.bay_id for c in layout.cells))),
        timedelta(minutes=1))
    return source, policy


def run_sequence(source, policy, order, schedule):
    current = source
    steps = []
    total_moves = outbound_moves = 0
    for cargo in order:
        started = perf_counter()
        result = plan_spatial(current, OutboundRequest(cargo, current.revision), policy,
                              now=current.observed_at, schedule=schedule)
        elapsed = perf_counter() - started
        report = result.optimization
        steps.append(dict(cargo_id=cargo, status=result.status, reason=result.reason,
            planner_seconds=elapsed, total_moves=len(result.moves),
            optimization=dict(objective_version=report.objective_version,
                score=asdict(report.score) if report.score else None,
                expanded_states=report.expanded_states,
                examined_candidates=report.examined_candidates,
                search_complete=report.search_complete, budget_reason=report.budget_reason) if report else None))
        if result.status != 'READY':
            break
        for move in result.moves:
            if current.placement(move.cargo_id) != move.source_placement:
                raise ValueError('Plan replay source mismatch')
            current = current.with_placements(tuple(move.destination_placement
                if p.cargo_id == move.cargo_id else p for p in current.placements))
        if current.placement(cargo).area_id != policy.outbound_area:
            raise ValueError('Plan did not export the requested cargo')
        total_moves += len(result.moves)
        outbound_moves += 1
        # Synthetic stand reset represents removal between requests, not a physical receipt.
        current = replace(current, revision=current.revision + 1,
            placements=tuple(p for p in current.placements if p.cargo_id != cargo),
            shapes=tuple(s for s in current.shapes if s.cargo_id != cargo))
    return dict(completed=outbound_moves == len(order), total_moves=total_moves,
        outbound_moves=outbound_moves, rehandle_moves=total_moves - outbound_moves,
        planner_seconds=sum(step['planner_seconds'] for step in steps),
        physical_work_seconds=None, steps=steps)


def compare(repeats=5):
    if type(repeats) is not int or repeats < 1:
        raise ValueError('Repeats must be a positive integer')
    rows = []
    for vertical in (False, True):
        source, policy = fixture(vertical)
        for remaining in (('a', 'b'), ('b', 'a')):
            order = ('target', *remaining)
            schedule = {cargo: (rank, None) for rank, cargo in enumerate(order)}
            for repeat in range(repeats):
                treatments = (('unknown', {}), ('scheduled', schedule))
                for name, ranks in treatments[::(-1 if repeat % 2 else 1)]:
                    rows.append(dict(layout='vertical' if vertical else 'horizontal',
                        order=order, repeat=repeat, treatment=name,
                        **run_sequence(source, policy, order, ranks)))
    return rows


def main():
    rows = compare()
    summary = []
    for layout, order, treatment in dict.fromkeys(
            (r['layout'], r['order'], r['treatment']) for r in rows):
        group = [r for r in rows if (r['layout'], r['order'], r['treatment']) == (layout, order, treatment)]
        summary.append(dict(layout=layout, order=order, treatment=treatment,
            completed_runs=sum(r['completed'] for r in group), runs=len(group),
            total_moves=[r['total_moves'] for r in group],
            rehandle_moves=[r['rehandle_moves'] for r in group],
            planner_median_seconds=median(r['planner_seconds'] for r in group)))
    print(json.dumps(dict(source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], text=True).strip(),
        command='python -m tools.rehandling_baseline', synthetic=True,
        timing_scope='planner calls only; excludes replay and all physical work',
        fixed_cases=4, repeats_per_treatment=5,
        physical_work_seconds=None, summary=summary, runs=rows), indent=2))


if __name__ == '__main__':
    main()
