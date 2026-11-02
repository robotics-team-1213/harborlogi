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
                                                      'trip', 'trip_request', 'compile', 'cancel_arm'), name)
