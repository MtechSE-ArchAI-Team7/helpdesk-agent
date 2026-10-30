from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
PACKAGE = Path(__file__).resolve().parents[2] / "src" / "helpdesk_agent"
LAYERS = {
    "api",
    "domain",
    "application",
    "workflows",
    "reasoning",
    "prompts",
    "ports",
    "infrastructure",
    "config",
    "observability",
}
FORBIDDEN = {
    "domain": LAYERS - {"domain"},
    "ports": LAYERS - {"domain", "ports"},
    "application": {"api", "workflows", "infrastructure", "config", "reasoning"},
    "reasoning": {"api", "workflows", "infrastructure", "config", "application"},
    "prompts": LAYERS - {"domain", "prompts", "ports"},
    "workflows": {"api", "infrastructure"},
}
PROVIDER_MODULES = {"openai", "boto3", "botocore", "mcp", "fastmcp", "a2a", "fastapi", "langgraph", "opentelemetry"}


def imported_modules(source: str, module: str, is_package: bool = False) -> set[str]:
    imports = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module
            )
            if base:
                imports.add(base)
                imports.update(f"{base}.{alias.name}" for alias in node.names)
    return imports


@pytest.mark.parametrize(
    "path",
    sorted(p for p in PACKAGE.rglob("*.py") if p.relative_to(PACKAGE).parts[0] in LAYERS),
    ids=lambda p: str(p.relative_to(PACKAGE)),
)
def test_dependency_direction(path: Path) -> None:
    relative = path.relative_to(PACKAGE)
    layer = relative.parts[0]
    module = "helpdesk_agent." + ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
    dependencies = imported_modules(path.read_text(), module, path.name == "__init__.py")
    for dependency in dependencies:
        if dependency.startswith("helpdesk_agent."):
            destination = dependency.split(".")[1]
            assert destination in LAYERS, f"{relative} imports legacy module {dependency}"
            assert destination not in FORBIDDEN.get(layer, set()), f"{layer} depends on {dependency}"
        elif layer in {"domain", "ports", "application", "reasoning", "prompts"}:
            assert dependency.split(".")[0] not in PROVIDER_MODULES, f"{layer} imports provider {dependency}"


def test_import_resolution_includes_relative_and_module_imports() -> None:
    imports = imported_modules(
        "from ..domain import models\nfrom helpdesk_agent.infrastructure.mcp import transport\nimport openai",
        "helpdesk_agent.application.example",
    )
    assert {
        "helpdesk_agent.domain.models",
        "helpdesk_agent.infrastructure.mcp",
        "helpdesk_agent.infrastructure.mcp.transport",
        "openai",
    } <= imports
