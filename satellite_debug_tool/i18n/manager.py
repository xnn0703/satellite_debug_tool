"""Qt translation lifecycle and runtime widget retranslation."""

from __future__ import annotations

import logging
import os
import re
import weakref
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QLocale,
    QObject,
    QTranslator,
    Signal,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
)

from satellite_debug_tool.core.config import Settings


_LOG = logging.getLogger(__name__)

LANGUAGE_AUTO = "auto"
LANGUAGE_ZH_CN = "zh_CN"
LANGUAGE_EN_US = "en_US"
SUPPORTED_LANGUAGE_PREFERENCES = (
    LANGUAGE_AUTO,
    LANGUAGE_ZH_CN,
    LANGUAGE_EN_US,
)

_TRANSLATION_CONTEXT = ""
_TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"
_APP_TRANSLATION_BASENAME = "satellite_debug_tool"
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}|%n|%\d+")

_manager: Optional["TranslationManager"] = None


def normalize_language_preference(value: object) -> str:
    """Normalize persisted/environment language aliases."""
    text = str(value or "").strip().replace("-", "_")
    lower = text.lower()
    if lower in ("", "auto", "system", "system_default"):
        return LANGUAGE_AUTO
    if (
        lower in ("zh", "chinese", "simplified_chinese")
        or lower.startswith("zh_")
    ):
        return LANGUAGE_ZH_CN
    if lower in ("en", "english") or lower.startswith("en_"):
        return LANGUAGE_EN_US
    return LANGUAGE_AUTO


def resolve_effective_locale(
    preference: object,
    *,
    system_locale: Optional[str] = None,
    environment_override: Optional[str] = None,
) -> str:
    """Resolve a preference to one of the two shipped locales."""
    override = environment_override
    if override is None:
        override = os.getenv("SATELLITE_DEBUG_LOCALE")
    if override:
        normalized_override = normalize_language_preference(override)
        if normalized_override != LANGUAGE_AUTO:
            return normalized_override

    normalized = normalize_language_preference(preference)
    if normalized != LANGUAGE_AUTO:
        return normalized

    locale_name = str(system_locale or QLocale.system().name()).lower()
    return LANGUAGE_ZH_CN if locale_name.startswith("zh") else LANGUAGE_EN_US


def trc(context: str, source: str, /, **values: Any) -> str:
    """Translate a source string in an explicit Qt context."""
    translated = QCoreApplication.translate(context, source)
    if not values:
        return translated
    try:
        return translated.format(**values)
    except (KeyError, ValueError):
        _LOG.exception("Invalid translation placeholders for %r", source)
        return source.format(**values)


def tr(source: str, /, **values: Any) -> str:
    """Translate an English source string and optionally format named fields."""
    return trc(_TRANSLATION_CONTEXT, source, **values)


def tr_source(source: str) -> str:
    """Mark a deferred English source string for translation extraction."""
    return source


def trn(source: str, count: int, /, **values: Any) -> str:
    """Translate a Qt numerus source string containing ``%n``."""
    translated = QCoreApplication.translate(
        _TRANSLATION_CONTEXT,
        source,
        None,
        int(count),
    )
    if effective_locale() == LANGUAGE_EN_US:
        # Without a translator Qt still expands %n before returning the source,
        # so comparing the complete string with ``source`` is insufficient.
        translated = translated.replace("(s)", "" if int(count) == 1 else "s")
    translated = translated.replace("%n", str(int(count)))
    if not values:
        return translated
    try:
        return translated.format(**values)
    except (KeyError, ValueError):
        _LOG.exception("Invalid numerus translation placeholders for %r", source)
        return source.replace("%n", str(int(count))).format(**values)


class TranslationManager(QObject):
    """Own application and Qt translators for the QApplication lifetime."""

    language_changed = Signal(str)

    def __init__(
        self,
        app: QApplication,
        settings: Optional[Settings] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent or app)
        self._app = app
        self._settings = settings
        self._preference = normalize_language_preference(
            settings.get("ui.language", LANGUAGE_AUTO) if settings else LANGUAGE_AUTO
        )
        self._effective_locale = LANGUAGE_EN_US
        self._app_translator: Optional[QTranslator] = None
        self._qt_translator: Optional[QTranslator] = None
        self.apply_preference(self._preference, emit=False)

    @property
    def preference(self) -> str:
        return self._preference

    @property
    def effective_locale(self) -> str:
        return self._effective_locale

    def attach_settings(self, settings: Settings) -> None:
        self._settings = settings
        self._preference = normalize_language_preference(
            settings.get("ui.language", LANGUAGE_AUTO)
        )
        self.apply_preference(self._preference)

    def set_preference(self, preference: object, *, persist: bool = False) -> str:
        self._preference = normalize_language_preference(preference)
        if persist and self._settings is not None:
            self._settings.set("ui.language", self._preference)
            self._settings.persist_preferences()
        return self.apply_preference(self._preference)

    def apply_preference(self, preference: object, *, emit: bool = True) -> str:
        normalized = normalize_language_preference(preference)
        self._preference = normalized
        effective = resolve_effective_locale(normalized)

        self._remove_translators()
        if effective == LANGUAGE_ZH_CN:
            self._install_chinese_translators()

        self._effective_locale = effective
        if emit:
            # Re-emit deliberately: callers use this to refresh a UI after
            # changing from auto to an explicit preference with the same locale.
            self.language_changed.emit(effective)
        return effective

    def _remove_translators(self) -> None:
        if self._app_translator is not None:
            self._app.removeTranslator(self._app_translator)
            self._app_translator = None
        if self._qt_translator is not None:
            self._app.removeTranslator(self._qt_translator)
            self._qt_translator = None

    def _install_chinese_translators(self) -> None:
        qt_translator = QTranslator(self)
        qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if qt_translator.load("qtbase_zh_CN", qt_dir):
            self._app.installTranslator(qt_translator)
            self._qt_translator = qt_translator
        else:
            qt_translator.deleteLater()

        app_translator = QTranslator(self)
        qm_path = _TRANSLATIONS_DIR / f"{_APP_TRANSLATION_BASENAME}_zh_CN.qm"
        if app_translator.load(str(qm_path)):
            self._app.installTranslator(app_translator)
            self._app_translator = app_translator
        else:
            _LOG.warning("Application translation not found or invalid: %s", qm_path)
            app_translator.deleteLater()


def initialize_translation_manager(
    app: QApplication,
    settings: Optional[Settings] = None,
) -> TranslationManager:
    """Create or reconfigure the process-wide translation manager."""
    global _manager
    if _manager is None or _manager._app is not app:
        _manager = TranslationManager(app, settings)
    elif settings is not None:
        _manager.attach_settings(settings)
    return _manager


def get_translation_manager() -> Optional[TranslationManager]:
    return _manager


def effective_locale() -> str:
    manager = get_translation_manager()
    if manager is not None:
        return manager.effective_locale
    return resolve_effective_locale(LANGUAGE_AUTO)


class _UiRetranslator(QObject):
    """Bind locale changes to one widget's explicit retranslation contract."""

    def __init__(self, root: QWidget, manager: TranslationManager) -> None:
        super().__init__(root)
        self._root_ref = weakref.ref(root)
        manager.language_changed.connect(self.retranslate_ui)
        self.retranslate_ui()

    def retranslate_ui(self, _locale: str = "") -> None:
        root = self._root_ref()
        if root is None:
            return
        callback = getattr(root, "retranslate_ui", None)
        if not callable(callback):
            raise TypeError(
                f"{type(root).__name__} must implement retranslate_ui()"
            )
        callback()


def register_translatable(root: QWidget) -> _UiRetranslator:
    """Register a widget's explicit stable-key retranslation callback."""
    existing = getattr(root, "_i18n_retranslator", None)
    if isinstance(existing, _UiRetranslator):
        existing.retranslate_ui()
        return existing

    manager = get_translation_manager()
    if manager is None:
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("QApplication must exist before registering translations")
        manager = initialize_translation_manager(app)
    binding = _UiRetranslator(root, manager)
    setattr(root, "_i18n_retranslator", binding)
    return binding


def refresh_translations(root: QWidget) -> None:
    binding = register_translatable(root)
    binding.retranslate_ui()


def set_translatable_text(
    source: str,
    widget: Any,
    /,
    *,
    context: str = _TRANSLATION_CONTEXT,
    n: Optional[int] = None,
    **values: Any,
) -> str:
    setattr(widget, "_i18n_raw_text", False)
    rendered = (
        trn(source, n, **values)
        if n is not None
        else trc(context, source, **values)
    )
    setattr(
        widget,
        "_i18n_state_text",
        {
            "source": source,
            "context": context,
            "last": rendered,
            "n": n,
            "values": dict(values),
        },
    )
    widget.setText(rendered)
    return rendered


def set_translatable_n_text(
    source: str,
    widget: Any,
    count: int,
    /,
    **values: Any,
) -> str:
    setattr(widget, "_i18n_raw_text", False)
    rendered = trn(source, count, **values)
    setattr(
        widget,
        "_i18n_state_text",
        {
            "source": source,
            "last": rendered,
            "n": int(count),
            "values": dict(values),
        },
    )
    widget.setText(rendered)
    return rendered


def set_translatable_tooltip(
    source: str,
    widget: Any,
    /,
    **values: Any,
) -> str:
    rendered = tr(source, **values)
    setattr(
        widget,
        "_i18n_state_tooltip",
        {
            "source": source,
            "last": rendered,
            "values": dict(values),
        },
    )
    widget.setToolTip(rendered)
    return rendered


def mark_raw_text(widget: Any) -> Any:
    """Exclude a device/user-owned text property from application translation."""
    setattr(widget, "_i18n_raw_text", True)
    return widget


def set_raw_text(text: str, widget: Any, /) -> str:
    """Set device/user-owned text without allowing reverse-translation heuristics."""
    mark_raw_text(widget)
    widget.setText(text)
    return text


def translation_placeholders(text: str) -> set[str]:
    """Public test helper used by the catalog checker."""
    return set(_PLACEHOLDER_RE.findall(text))
