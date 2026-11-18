"""Keep storage and Fleet protocol out of application orchestration and presentation."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ModuleBoundaryTests(unittest.TestCase):
    def test_domain_does_not_import_web_framework(self):
        for path in (ROOT / 'harbor').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf8'))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    self.assertNotIn(name.split('.')[0], ('django', 'harbor_web'), path.name)

    def test_product_code_reaches_rosy_only_over_fleet_http(self):
        # ROSY-only top-level packages (rosy-platform git ls-files, 2026-11-04) and ROS itself; generic
        # names such as web/api/core are left out because they collide with stdlib or Harbor modules.
        rosy = {'fleet', 'core_common', 'core_events', 'core_features', 'core_api_web', 'rosy_account', 'rosy_agent',
                'rosy_cell', 'rosy_gateway', 'rosy_vision', 'rosy_situation', 'rosylib', 'omx_adapter',
                'lerobot_robot_rosy_omx', 'perception', 'perception_runtime', 'manipulation', 'navigation',
                'decision', 'swarm', 'device_core', 'rclpy'}
        for package in ('harbor', 'harbor_web'):
            for path in (ROOT / package).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module or ''] if isinstance(node, ast.ImportFrom) and not node.level else []
                    for name in names:
                        self.assertNotIn(name.split('.')[0], rosy, path)

    def test_executor_only_schedules_application_commands(self):
        tree = ast.parse((ROOT / 'harbor_web/executor.py').read_text(encoding='utf8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ('ledger', 'client', '_save', '_prepare_ledger', '_record_fact', '_validate'))

    def test_consumers_do_not_open_business_sql_or_decode_storage_payloads(self):
        for name in ('harbor/web.py', 'harbor_web/commands.py', 'harbor_web/evidence.py'):
            tree = ast.parse((ROOT / name).read_text(encoding='utf8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    self.assertFalse(node.value.id == 'sqlite3' and node.attr == 'connect', name)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'loads':
                    self.assertNotIn("['payload']", ast.unparse(node), name)

    def test_business_ledgers_do_not_interpret_fleet_status(self):
        for name in ('harbor/workflow.py', 'harbor/transport.py', 'harbor_web/pipeline.py'):
            tree = ast.parse((ROOT / name).read_text(encoding='utf8'))
            values = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            self.assertTrue(values.isdisjoint({'GOAL_CONFIRMED', 'ACTION_SUCCEEDED', 'PROPOSED', 'RUNNING'}), name)

    def test_consumers_do_not_call_fleet_execution_endpoints(self):
        for name in ('harbor/workflow.py', 'harbor/transport.py', 'harbor/web.py',
                     'harbor_web/commands.py', 'harbor_web/executor.py', 'harbor_web/pipeline.py'):
            tree = ast.parse((ROOT / name).read_text(encoding='utf8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ('propose_arm', 'arm_request', 'arm_job', 'physical_receipt',
                                                      'trip', 'trip_request', 'trip_propose', 'trip_proposal_request', 'compile', 'cancel_arm'), name)

    def test_commands_internal_import_fanout_does_not_grow(self):
        tree = ast.parse((ROOT / 'harbor_web/commands.py').read_text(encoding='utf8'))
        modules = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(('harbor.', 'harbor_web.'))
        } | {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names if alias.name.startswith(('harbor.', 'harbor_web.'))
        }
        self.assertLessEqual(len(modules), 15, f'commands.py internal fan-out grew: {sorted(modules)}')

    def test_spatial_module_does_not_grow_past_ratchet(self):
        lines = (ROOT / 'harbor/spatial.py').read_text(encoding='utf8').splitlines()
        self.assertLessEqual(len(lines), 725)


def _module_graph():
    """Internal import edges of harbor and harbor_web, any depth (lazy imports included), like the SRP measurement."""
    mods = {}
    for package in ('harbor', 'harbor_web'):
        for path in (ROOT / package).rglob('*.py'):
            if 'migrations' in path.parts or '__pycache__' in path.parts:
                continue
            name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            mods[name.removesuffix('.__init__')] = path
    graph = {name: set() for name in mods}
    for name, path in mods.items():
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    parts = package.split('.')[:len(package.split('.')) - (node.level - 1)]
                    base = '.'.join(parts + ([base] if base else []))
                targets = [base] + [f'{base}.{a.name}' for a in node.names]
            else:
                continue
            for target in targets:
                while target and target not in mods:
                    target = target.rpartition('.')[0]
                if target and target != name:
                    graph[name].add(target)
    return graph


def _cycle_edges(graph):
    """Edges whose two ends lie in one strongly connected component (Tarjan)."""
    index, low, stack, on_stack, comp_of = {}, {}, [], set(), {}

    def visit(v):
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            members = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                members.append(w)
                if w == v:
                    break
            for w in members:
                comp_of[w] = v

    for v in graph:
        if v not in index:
            visit(v)
    return {(a, b) for a in graph for b in graph[a] if comp_of[a] == comp_of[b]}


# Import edges inside today's cycles (SCC), one block per cycle. Each refactor PR deletes the lines it breaks.
# Adding an edge that closes or joins a cycle fails; so does leaving a line that is no longer a cycle edge.
ALLOWED_CYCLE_EDGES = {
    # fleet_discovery, rosy_http
    ('harbor.fleet_discovery', 'harbor.rosy_http'),
    ('harbor.rosy_http', 'harbor.fleet_discovery'),
    # autoplanning, command_binding, commands, evidence, fleet_connection, human_evidence, order_queue, plan_service, readiness, rearrangement, services
    ('harbor_web.autoplanning', 'harbor_web.services'),
    ('harbor_web.command_binding', 'harbor_web.evidence'),
    ('harbor_web.command_binding', 'harbor_web.fleet_connection'),
    ('harbor_web.commands', 'harbor_web.command_binding'),
    ('harbor_web.commands', 'harbor_web.evidence'),
    ('harbor_web.commands', 'harbor_web.fleet_connection'),
    ('harbor_web.commands', 'harbor_web.human_evidence'),
    ('harbor_web.commands', 'harbor_web.readiness'),
    ('harbor_web.commands', 'harbor_web.rearrangement'),
    ('harbor_web.commands', 'harbor_web.services'),
    ('harbor_web.evidence', 'harbor_web.commands'),
    ('harbor_web.evidence', 'harbor_web.services'),
    ('harbor_web.fleet_connection', 'harbor_web.commands'),
    ('harbor_web.fleet_connection', 'harbor_web.services'),
    ('harbor_web.human_evidence', 'harbor_web.commands'),
    ('harbor_web.order_queue', 'harbor_web.autoplanning'),
    ('harbor_web.plan_service', 'harbor_web.autoplanning'),
    ('harbor_web.plan_service', 'harbor_web.order_queue'),
    ('harbor_web.plan_service', 'harbor_web.rearrangement'),
    ('harbor_web.readiness', 'harbor_web.command_binding'),
    ('harbor_web.readiness', 'harbor_web.fleet_connection'),
    ('harbor_web.rearrangement', 'harbor_web.commands'),
    ('harbor_web.rearrangement', 'harbor_web.order_queue'),
    ('harbor_web.rearrangement', 'harbor_web.services'),
    ('harbor_web.services', 'harbor_web.autoplanning'),
    ('harbor_web.services', 'harbor_web.order_queue'),
    ('harbor_web.services', 'harbor_web.plan_service'),
    # command_models, models
    ('harbor_web.command_models', 'harbor_web.models'),
    ('harbor_web.models', 'harbor_web.command_models'),
}


class ImportCycleRatchetTests(unittest.TestCase):
    def test_import_cycle_edges_match_allow_list(self):
        edges = _cycle_edges(_module_graph())
        new = sorted(edges - ALLOWED_CYCLE_EDGES)
        stale = sorted(ALLOWED_CYCLE_EDGES - edges)
        self.assertFalse(new, f'new import cycle edges (break the cycle instead of listing them): {new}')
        self.assertFalse(stale, f'edges no longer in a cycle; delete these lines from ALLOWED_CYCLE_EDGES: {stale}')
