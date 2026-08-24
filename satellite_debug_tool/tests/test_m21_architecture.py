"""M21 architecture boundaries that must remain explicit as features grow."""

from __future__ import annotations

import ast
from pathlib import Path

from satellite_debug_tool.core.protocol.domain_registry import DOMAIN_DECODERS
from satellite_debug_tool.ui.semantic_style import set_semantic_property


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _python_sources(directory: Path):
    yield from sorted(directory.glob("*.py"))


def test_protocol_command_ownership_is_disjoint() -> None:
    ownership: dict[int, str] = {}
    for domain, decoders in DOMAIN_DECODERS.items():
        for command in decoders:
            assert command not in ownership, (
                f"command 0x{command:02X} belongs to both "
                f"{ownership.get(command)} and {domain}"
            )
            ownership[command] = domain

    assert set(DOMAIN_DECODERS) == {"debug", "product", "orbit"}
    assert ownership


def test_frame_receiver_depends_on_domain_dispatch_only() -> None:
    path = PACKAGE_ROOT / "core" / "protocol" / "frame_receiver_v2.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "decoder_for" in imported_names
    assert not any(name.startswith("decode_") for name in imported_names)


def test_ui_does_not_own_protocol_parser_or_handshake() -> None:
    forbidden = {"FrameReceiverV2", "Handshake"}
    findings: list[str] = []
    for path in _python_sources(PACKAGE_ROOT / "ui"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                findings.append(f"{path.name}:{node.lineno}:{node.id}")
    assert findings == []


def test_production_ui_does_not_transition_attempt_state_directly() -> None:
    paths = (
        PACKAGE_ROOT / "ui" / "production_workspace.py",
        PACKAGE_ROOT / "ui" / "fixture_debug_workspace.py",
    )
    findings: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "transition_attempt"
            ):
                findings.append(f"{path.name}:{node.lineno}")
    assert findings == []


def test_registered_ui_classes_define_explicit_retranslation() -> None:
    findings: list[str] = []
    for path in _python_sources(PACKAGE_ROOT / "ui"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for class_node in (
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ):
            methods = [
                node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            registers = any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "register_translatable"
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register_translatable"
                )
                for method in methods
                for node in ast.walk(method)
            )
            if registers and not any(
                method.name == "retranslate_ui" for method in methods
            ):
                findings.append(f"{path.name}:{class_node.lineno}:{class_node.name}")
    assert findings == []


def test_translation_manager_has_no_object_tree_reverse_scan() -> None:
    source = (PACKAGE_ROOT / "i18n" / "manager.py").read_text(encoding="utf-8")
    assert "findChildren" not in source
    assert "source_for_display" not in source


def test_semantic_property_refreshes_only_on_change(qapplication_session) -> None:
    from PySide6.QtWidgets import QWidget

    widget = QWidget()
    assert set_semantic_property(widget, "state", "ready") is True
    assert set_semantic_property(widget, "state", "ready") is False
    assert widget.property("state") == "ready"
