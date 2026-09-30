"""The src/orbi runtime import graph must stay acyclic.

Structural guard grown out of the scheduler layer cycle (Issue #849
follow-up): the platform backends took the platform-independent unit
facts from the interface module, while the interface dispatches back
into the backends at runtime — three modules one nominal interface.
The fix moved the shared facts into the ``orbi.scheduler_units`` leaf;
this test keeps the whole package from regrowing a cycle.

Conventions this guard relies on (true of src/orbi today, and cheap
to keep true): intra-package imports use the ``from orbi...`` form
(the bare ``import orbi.x`` statement is still recognized), and the
annotation-only blocks use the bare ``if TYPE_CHECKING:`` name form
and are excluded from the runtime graph.
"""
import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"


def _module_names() -> set:
    return {path.stem for path in SRC_DIR.glob("*.py")}


class _RuntimeImportVisitor(ast.NodeVisitor):
    """Collect the intra-package imports reachable at runtime."""

    def __init__(self, modules: set):
        self.modules = modules
        self.edges: set = set()

    def visit_If(self, node) -> None:
        test = node.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return  # annotation-only: erased at runtime
        self.generic_visit(node)

    def visit_Import(self, node) -> None:
        for alias in node.names:
            target = alias.name.split(".")[1] if "." in alias.name else ""
            if alias.name.startswith("orbi.") and target in self.modules:
                self.edges.add(target)

    def visit_ImportFrom(self, node) -> None:
        if node.module == "orbi":
            for alias in node.names:
                if alias.name in self.modules:
                    self.edges.add(alias.name)
        elif node.module and node.module.startswith("orbi."):
            target = node.module.split(".")[1]
            if target in self.modules:
                self.edges.add(target)


def _runtime_import_graph() -> dict:
    modules = _module_names()
    graph: dict = {}
    for path in sorted(SRC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _RuntimeImportVisitor(modules)
        visitor.visit(tree)
        graph[path.stem] = visitor.edges - {path.stem}
    return graph


def _strongly_connected_components(graph: dict) -> list:
    """Tarjan's algorithm; each component is a list of module names."""
    index: dict = {}
    low: dict = {}
    stack: list = []
    on_stack: set = set()
    components: list = []
    counter = [0]

    def strongconnect(node) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph[node]):
            if neighbor not in index:
                strongconnect(neighbor)
                low[node] = min(low[node], low[neighbor])
            elif neighbor in on_stack:
                low[node] = min(low[node], index[neighbor])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)
    return components


def _multi_node_components(graph: dict) -> list:
    return sorted(
        sorted(component)
        for component in _strongly_connected_components(graph)
        if len(component) > 1
    )


def test_import_graph_has_no_runtime_cycle() -> None:
    cycles = _multi_node_components(_runtime_import_graph())
    assert not cycles, (
        "src/orbi grew a runtime import cycle; every cycle member "
        f"can never be loaded independently: {cycles}"
    )


def test_cycle_guard_detects_a_cycle() -> None:
    # Positive control: the guard must actually see a cycle, otherwise
    # the acyclicity test above is a always-green fake guard.
    graph = {"a": {"b"}, "b": {"a"}, "c": {"a"}}
    assert _multi_node_components(graph) == [["a", "b"]]


def test_import_visitor_skips_unknown_and_keeps_known_targets() -> None:
    tree = ast.parse(
        "import orbi.github\n"
        "import orbi.not_a_module\n"
        "from orbi.also_not_a_module import something\n"
    )
    visitor = _RuntimeImportVisitor({"github"})
    visitor.visit(tree)
    assert visitor.edges == {"github"}
