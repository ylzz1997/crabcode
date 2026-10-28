"""Guided terminal menus for adding, discovering and deleting models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, Sequence, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

from crabcode_cli.session_picker import PICKER_STYLE, _plain
from crabcode_core.api.model_discovery import ModelDiscoveryError, discover_models
from crabcode_core.config.manager import ConfigManager
from crabcode_core.config.model_catalog import (
    SETTINGS_SOURCE_LABELS,
    WRITABLE_SETTINGS_SOURCES,
    CatalogKind,
    CatalogImportResult,
    CatalogMutationResult,
    ModelCatalogError,
    WritableSettingsSource,
    add_catalog_entry,
    catalog_names,
    delete_catalog_entry,
    import_group_models,
    list_catalog_definitions,
    referencing_models,
    settings_source_path,
    validate_catalog_name,
)
from crabcode_core.events import CoreSession


ManagementAction = Literal["add", "delete"]
ManagementKind = Literal["model", "group", "discover"]

_SENSITIVE_CONFIG_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class MenuOption:
    value: str
    label: str
    hint: str = ""
    disabled: bool = False


class Prompter(Protocol):
    async def select(
        self,
        message: str,
        options: Sequence[MenuOption],
        *,
        searchable: bool = True,
        initial: str | None = None,
    ) -> str | None: ...

    async def multiselect(
        self, message: str, options: Sequence[MenuOption],
    ) -> list[str] | None: ...

    async def text(self, message: str, *, initial: str = "") -> str | None: ...

    async def confirm(self, message: str, *, default: bool = False) -> bool | None: ...

    def note(self, title: str, body: str) -> None: ...

    def error(self, message: str) -> None: ...


class SearchableMenu:
    """Small OpenClaw-style searchable select with labels and contextual hints."""

    def __init__(
        self,
        title: str,
        message: str,
        options: Sequence[MenuOption],
        *,
        searchable: bool = True,
        initial: str | None = None,
        multiple: bool = False,
    ) -> None:
        self.title = title
        self.message = message
        self.options = list(options)
        self.searchable = searchable
        self.initial = initial
        self.multiple = multiple
        self.checked: set[str] = set()
        self.selection_error = ""
        self.matches = self.options.copy()
        self.selected = next(
            (index for index, option in enumerate(self.matches) if option.value == initial),
            0,
        )
        self.query = Buffer(on_text_changed=lambda _: self.refresh())
        self.search = BufferControl(buffer=self.query)
        self.list_control = FormattedTextControl(
            self._list,
            focusable=not searchable,
            get_cursor_position=lambda: Point(x=0, y=self.selected),
        )
        self.list_window = Window(
            self.list_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        @bindings.add("c-d", eager=True)
        def cancel(event: Any) -> None:
            event.app.exit(result=None)

        @bindings.add("enter", eager=True)
        def confirm(event: Any) -> None:
            if self.multiple:
                if self.checked:
                    event.app.exit(result=[option.value for option in self.options if option.value in self.checked])
            elif self.matches:
                event.app.exit(result=self.matches[self.selected].value)

        if multiple:
            @bindings.add("tab", eager=True)
            @bindings.add(" ", eager=True)
            def toggle(event: Any) -> None:
                if self.matches:
                    option = self.matches[self.selected]
                    if option.disabled:
                        return
                    self.selection_error = ""
                    if option.value in self.checked:
                        self.checked.remove(option.value)
                    elif len(self.checked) < 1000:
                        self.checked.add(option.value)
                    else:
                        self.selection_error = "Select at most 1000 models per import."

            @bindings.add("c-a", eager=True)
            def toggle_visible(event: Any) -> None:
                visible = {option.value for option in self.matches if not option.disabled}
                self.selection_error = ""
                if visible <= self.checked:
                    self.checked.difference_update(visible)
                elif len(self.checked | visible) <= 1000:
                    self.checked.update(visible)
                else:
                    self.selection_error = "Select at most 1000 models; narrow the search first."

        @bindings.add("up")
        @bindings.add("down")
        @bindings.add("pageup")
        @bindings.add("pagedown")
        def navigate(event: Any) -> None:
            page = (
                max(1, self.list_window.render_info.window_height - 1)
                if self.list_window.render_info
                else 10
            )
            delta = {
                "up": -1,
                "down": 1,
                "pageup": -page,
                "pagedown": page,
            }[event.key_sequence[0].key]
            self.selected = max(0, min(len(self.matches) - 1, self.selected + delta))

        content: list[Any] = [
            Window(
                FormattedTextControl([("class:accent", f"  ╭─ CrabCode · {_plain(title)}")]),
                height=1,
            ),
            Window(
                FormattedTextControl([("class:hint", f"  {_plain(message)}")]),
                height=1,
                wrap_lines=False,
            ),
            Window(height=1),
        ]
        if searchable:
            content.extend(
                [
                    VSplit(
                        [
                            Window(
                                FormattedTextControl([("class:accent", "  Search ❯ ")]),
                                width=11,
                            ),
                            Window(self.search, height=1, wrap_lines=False),
                        ],
                        height=1,
                    ),
                    Window(height=1),
                ]
            )
        content.extend(
            [
                self.list_window,
                Window(FormattedTextControl(self._details), height=2, wrap_lines=True),
                Window(char="─", height=1, style="class:hint"),
                Window(FormattedTextControl(self._footer), height=3 if multiple else 2, wrap_lines=True),
            ]
        )
        focused = self.search if searchable else self.list_control
        self.app: Application[str | list[str] | None] = Application(
            layout=Layout(HSplit(content), focused_element=focused),
            key_bindings=bindings,
            full_screen=True,
            erase_when_done=True,
            style=PICKER_STYLE,
        )

    def refresh(self) -> None:
        selected_value = (
            self.matches[self.selected].value
            if self.matches and self.selected < len(self.matches)
            else self.initial
        )
        terms = self.query.text.casefold().split()
        self.matches = [
            option
            for option in self.options
            if all(
                term
                in f"{option.label} {option.value} {option.hint}".casefold()
                for term in terms
            )
        ]
        self.selected = next(
            (
                index
                for index, option in enumerate(self.matches)
                if option.value == selected_value
            ),
            0,
        )
        self.app.invalidate()

    def _list(self) -> list[tuple[str, str]]:
        if not self.options:
            return [("class:hint", "  No choices are available.")]
        if not self.matches:
            return [("class:hint", "  No matching choices. Clear the search to continue.")]
        fragments: list[tuple[str, str]] = []
        for index, option in enumerate(self.matches):
            style = "class:selected" if index == self.selected else ""
            marker = ""
            if self.multiple:
                marker = "[✓] " if option.disabled else "[x] " if option.value in self.checked else "[ ] "
            fragments.extend(
                [
                    (style, "  ❯ " if index == self.selected else "    "),
                    (style, marker + _plain(option.label)),
                    ("", "\n" if index < len(self.matches) - 1 else ""),
                ]
            )
        return fragments

    def _details(self) -> list[tuple[str, str]]:
        if not self.matches:
            return []
        option = self.matches[self.selected]
        return [
            ("class:hint", f"  {_plain(option.label)}\n  {_plain(option.hint)}")
        ]

    def _footer(self) -> list[tuple[str, str]]:
        count = len(self.matches)
        if self.multiple:
            return [("class:hint",
                "  ↑↓ browse · Space/Tab toggle · Enter import · Esc cancel\n"
                "  Ctrl+A toggle results · Type to search · Ctrl+U clear\n"
                f"  {self.selection_error or f'{len(self.checked)} selected · {count} shown'}"
            )]
        search_help = " · Type to search" if self.searchable else ""
        return [
            (
                "class:hint",
                f"  ↑↓ browse · PgUp/PgDn page · Enter select · Esc/Ctrl+C cancel"
                f"    {self.selected + 1 if count else 0}/{count}\n  {search_help.lstrip(' ·')}",
            )
        ]

    async def run(self) -> str | list[str] | None:
        if not self.options:
            return None
        return await self.app.run_async()


class TerminalPrompter:
    def __init__(self, writer: Callable[[str], None] | None = None) -> None:
        self._writer = writer or print

    async def select(
        self,
        message: str,
        options: Sequence[MenuOption],
        *,
        searchable: bool = True,
        initial: str | None = None,
    ) -> str | None:
        result = await SearchableMenu(
            "Model management",
            message,
            options,
            searchable=searchable,
            initial=initial,
        ).run()
        return cast(str | None, result)

    async def multiselect(
        self, message: str, options: Sequence[MenuOption],
    ) -> list[str] | None:
        result = await SearchableMenu(
            "Get models from group", message, options, multiple=True,
        ).run()
        return cast(list[str] | None, result)

    async def text(self, message: str, *, initial: str = "") -> str | None:
        prompt: PromptSession[str] = PromptSession()
        try:
            return (
                await prompt.prompt_async(
                    HTML(f"<b><ansicyan>{message} ❯ </ansicyan></b>"),
                    default=initial,
                )
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    async def confirm(self, message: str, *, default: bool = False) -> bool | None:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = await self.text(f"{message} {suffix}")
            if answer is None:
                return None
            if not answer:
                return default
            if answer.casefold() in {"y", "yes"}:
                return True
            if answer.casefold() in {"n", "no"}:
                return False
            self.error("Please answer y or n.")

    def note(self, title: str, body: str) -> None:
        self._writer(f"\n  {title}\n{_indent(body)}\n")

    def error(self, message: str) -> None:
        self._writer(f"  Error: {message}")


class _Cancelled(Exception):
    pass


_PROVIDERS: tuple[MenuOption, ...] = (
    MenuOption("anthropic", "Anthropic", "Anthropic Messages API"),
    MenuOption("openai", "OpenAI", "OpenAI-compatible Chat Completions"),
    MenuOption("codex", "Codex", "OpenAI Responses / Codex OAuth"),
    MenuOption("router", "Router / custom endpoint", "Choose the wire format separately"),
    MenuOption("ollama", "Ollama", "Local or remote Ollama API"),
    MenuOption("gemini", "Gemini", "Google Gemini API"),
    MenuOption("azure", "Azure OpenAI", "Azure deployment settings can use advanced JSON"),
    MenuOption("bedrock", "Amazon Bedrock", "Anthropic models through Bedrock"),
    MenuOption("vertex", "Google Vertex AI", "Anthropic models through Vertex AI"),
)

_FORMATS: tuple[MenuOption, ...] = tuple(
    MenuOption(value, label, "Router request/response format")
    for value, label in (
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic"),
        ("codex", "Codex Responses"),
        ("ollama", "Ollama"),
        ("gemini", "Gemini"),
        ("azure", "Azure OpenAI"),
    )
)


async def manage_model_catalog(
    session: CoreSession,
    action: ManagementAction,
    kind: ManagementKind | None = None,
    *,
    prompter: Prompter | None = None,
) -> CatalogMutationResult | CatalogImportResult | None:
    """Run the guided management flow and refresh the current session catalog."""
    active_prompter = prompter or TerminalPrompter()
    try:
        selected_kind = kind or await _choose_kind(action, active_prompter)
        if selected_kind is None:
            return None
        if selected_kind == "discover":
            if action != "add":
                raise ModelCatalogError("Use /add discover to fetch models from a group.")
            result = await _discover_group_models(session, active_prompter)
        elif action == "add":
            result = await _add_entry(session, selected_kind, active_prompter)
        else:
            result = await _delete_entry(session, selected_kind, active_prompter)
        if result is not None:
            session.reload_model_catalog()
        return result
    except _Cancelled:
        return None


async def _choose_kind(action: ManagementAction, prompter: Prompter) -> ManagementKind | None:
    options = [
        MenuOption("model", "Model", "A named model profile shown by /model"),
        MenuOption("group", "Group", "Shared provider and API settings inherited by models"),
    ]
    if action == "add":
        options.append(MenuOption("discover", "Get models from group", "Fetch, search and import models using a saved group"))
    value = await prompter.select(
        "What do you want to add?" if action == "add" else "What do you want to delete?",
        options,
        searchable=False,
        initial="model",
    )
    return cast(ManagementKind | None, value)


async def _discover_group_models(
    session: CoreSession, prompter: Prompter,
) -> CatalogImportResult | None:
    settings = ConfigManager(cwd=session.cwd).load()
    if not settings.groups:
        raise ModelCatalogError("No configuration groups found. Create one with /add group first.")
    name = await prompter.select(
        "Select a group to fetch models from",
        [MenuOption(name, name, _config_hint(group.model_dump(exclude_none=True)))
         for name, group in settings.groups.items()],
    )
    if name is None:
        return None
    prompter.note("Fetching model list", f"Group: {_plain(name)} · timeout: 15 seconds")
    try:
        models = await discover_models(settings.groups[name], settings.env)
    except ModelDiscoveryError as exc:
        raise ModelCatalogError(str(exc)) from None
    existing_ids = {config.model for config in settings.models.values() if config.group == name}
    available = [model for model in models if model["id"] not in existing_ids]
    if not available:
        prompter.note("Model list", "All returned models are already in this group." if models else "The API returned no models.")
        return None
    options = []
    for model in models:
        existing = model["id"] in existing_ids
        metadata = model.get("config", {})
        hints = [model.get("name", "")]
        hints.extend(f"{key}: {value}" for key, value in metadata.items())
        options.append(MenuOption(
            model["id"], model["id"] + (" · already added" if existing else ""),
            " · ".join(hint for hint in hints if hint), disabled=existing,
        ))
    selected = await prompter.multiselect(f"Group: {name} · select models to import", options)
    if not selected:
        return None
    selected_ids = set(selected)
    if not selected_ids <= {model["id"] for model in available}:
        raise ModelCatalogError("Select only available models from the fetched list.")
    source = await _choose_source(session.cwd, prompter)
    preview = "\n".join(_plain(model_id) for model_id in selected[:20])
    if len(selected) > 20:
        preview += f"\n… and {len(selected) - 20} more"
    prompter.note(
        f'Import {len(selected)} models from "{_plain(name)}" into {SETTINGS_SOURCE_LABELS[source]}', preview,
    )
    if not await prompter.confirm("Import selected models?", default=True):
        return None
    return import_group_models(
        session.cwd, source, name, selected,
        {model["id"]: model["config"] for model in models if model["id"] in selected_ids and model.get("config")},
    )


async def _choose_source(cwd: str, prompter: Prompter) -> WritableSettingsSource:
    options = [
        MenuOption(
            source,
            SETTINGS_SOURCE_LABELS[source],
            str(settings_source_path(cwd, source)),
        )
        for source in WRITABLE_SETTINGS_SOURCES
    ]
    value = await prompter.select(
        "Where should this configuration be saved?",
        options,
        searchable=False,
        initial="projectSettings",
    )
    if value is None:
        raise _Cancelled
    return cast(WritableSettingsSource, value)


async def _add_entry(
    session: CoreSession,
    kind: CatalogKind,
    prompter: Prompter,
) -> CatalogMutationResult | None:
    source = await _choose_source(session.cwd, prompter)
    name = await _prompt_new_name(session.cwd, kind, prompter)
    config = await _prompt_config(session.cwd, kind, prompter)
    preview = json.dumps(_redact_config(config), indent=2, ensure_ascii=False)
    prompter.note(
        f'Add {kind} "{name}" to {SETTINGS_SOURCE_LABELS[source]}',
        preview,
    )
    confirmed = await prompter.confirm("Save this configuration?", default=True)
    if not confirmed:
        return None
    return add_catalog_entry(session.cwd, source, kind, name, config)


async def _prompt_new_name(cwd: str, kind: CatalogKind, prompter: Prompter) -> str:
    existing = catalog_names(cwd, kind)
    while True:
        value = await prompter.text(
            "Model profile name" if kind == "model" else "Group name"
        )
        if value is None:
            raise _Cancelled
        try:
            name = validate_catalog_name(value)
        except ModelCatalogError as exc:
            prompter.error(str(exc))
            continue
        if name in existing:
            prompter.error(
                f'{kind.title()} "{name}" already exists. /add never overwrites entries.'
            )
            continue
        return name


async def _prompt_config(
    cwd: str,
    kind: CatalogKind,
    prompter: Prompter,
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    group_name: str | None = None
    if kind == "model":
        settings = ConfigManager(cwd=cwd).load()
        group_options = [
            MenuOption("__none__", "No group", "Configure the provider on this model")
        ]
        for name, group in settings.groups.items():
            group_options.append(
                MenuOption(
                    name,
                    name,
                    _config_hint(group.model_dump(exclude_none=True)),
                )
            )
        selected_group = await prompter.select(
            "Select a configuration group",
            group_options,
            searchable=True,
            initial="__none__",
        )
        if selected_group is None:
            raise _Cancelled
        group_name = None if selected_group == "__none__" else selected_group
        model_id = await _required_text("Provider model ID", prompter)
        config["model"] = model_id

    provider_options = list(_PROVIDERS)
    provider_initial = "anthropic"
    if kind == "group":
        provider_options.insert(
            0,
            MenuOption("__none__", "No provider", "Use advanced JSON for shared non-provider fields"),
        )
        provider_initial = "__none__"
    elif group_name is not None:
        provider_options.insert(
            0,
            MenuOption("__inherit__", "Inherit from group", f"Use provider settings from {group_name}"),
        )
        provider_initial = "__inherit__"

    provider = await prompter.select(
        "Select a provider",
        provider_options,
        searchable=True,
        initial=provider_initial,
    )
    if provider is None:
        raise _Cancelled
    if provider not in {"__none__", "__inherit__"}:
        config["provider"] = provider
        base_url = await prompter.text("Base URL (optional)")
        if base_url is None:
            raise _Cancelled
        if base_url:
            config["base_url"] = base_url
        api_key_env = await prompter.text("API key environment variable (optional)")
        if api_key_env is None:
            raise _Cancelled
        if api_key_env:
            config["api_key_env"] = api_key_env
        if provider == "router":
            api_format = await prompter.select(
                "Select the router API format",
                _FORMATS,
                searchable=False,
                initial="openai",
            )
            if api_format is None:
                raise _Cancelled
            config["format"] = api_format

    advanced = await _prompt_advanced_json(prompter)
    config.update(advanced)
    if kind == "model" and group_name is not None:
        config["group"] = group_name
    if kind == "group":
        config.pop("group", None)
    return config


async def _required_text(message: str, prompter: Prompter) -> str:
    while True:
        value = await prompter.text(message)
        if value is None:
            raise _Cancelled
        if value:
            return value
        prompter.error(f"{message} is required.")


async def _prompt_advanced_json(prompter: Prompter) -> dict[str, Any]:
    while True:
        raw = await prompter.text("Advanced ApiConfig JSON (optional)")
        if raw is None:
            raise _Cancelled
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            prompter.error(f"Invalid JSON: {exc.msg} at column {exc.colno}.")
            continue
        if not isinstance(value, dict):
            prompter.error("Advanced configuration must be a JSON object.")
            continue
        return value


async def _delete_entry(
    session: CoreSession,
    kind: CatalogKind,
    prompter: Prompter,
) -> CatalogMutationResult | None:
    definitions = list_catalog_definitions(session.cwd, kind, writable_only=True)
    if not definitions:
        raise ModelCatalogError(
            f"No deletable {kind}s are defined in user, project, or local settings."
        )
    options = [
        MenuOption(
            str(index),
            definition.name,
            f"{SETTINGS_SOURCE_LABELS[definition.source]} · "
            f"{_config_hint(definition.config)}",
        )
        for index, definition in enumerate(definitions)
    ]
    selected = await prompter.select(
        f"Select a {kind} to delete",
        options,
        searchable=True,
    )
    if selected is None:
        return None
    definition = definitions[int(selected)]
    _protect_active_selection(session, definition.kind, definition.name)

    warnings: list[str] = [
        f"Source: {SETTINGS_SOURCE_LABELS[definition.source]}",
        f"File: {definition.path}",
    ]
    if kind == "model" and session.settings.default_model == definition.name:
        warnings.append("This is the configured default model.")
    if kind == "group":
        references = referencing_models(session.cwd, definition.name)
        if references:
            warnings.append(
                "Models that will lose inherited settings: " + ", ".join(references)
            )
    prompter.note(f'Delete {kind} "{definition.name}"', "\n".join(warnings))
    confirmed = await prompter.confirm(
        "Delete only this definition from the selected layer?",
        default=False,
    )
    if not confirmed:
        return None
    return delete_catalog_entry(
        session.cwd,
        cast(WritableSettingsSource, definition.source),
        kind,
        definition.name,
    )


def _protect_active_selection(session: CoreSession, kind: CatalogKind, name: str) -> None:
    current = getattr(session, "_current_model_name", None)
    if current is None:
        return
    if kind == "model" and current == name:
        raise ModelCatalogError(
            f'Cannot delete active model "{name}". Switch with /model first.'
        )
    if kind == "group" and session.settings.get_api_config(current).group == name:
        raise ModelCatalogError(
            f'Cannot delete group "{name}" while active model "{current}" inherits it. '
            "Switch to a model outside this group first."
        )


def _config_hint(config: dict[str, Any]) -> str:
    parts = [
        str(config.get(key))
        for key in ("group", "provider", "model", "base_url")
        if config.get(key)
    ]
    return " · ".join(parts) if parts else "Empty/shared configuration"


def _redact_config(value: Any, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    is_reference = normalized_key.endswith("_env") or normalized_key.endswith("_path")
    if (
        key
        and not is_reference
        and any(part in normalized_key for part in _SENSITIVE_CONFIG_KEYS)
    ):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            child_key: _redact_config(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(child) for child in value]
    return value


def _indent(value: str) -> str:
    return "\n".join(f"    {line}" for line in value.splitlines())
