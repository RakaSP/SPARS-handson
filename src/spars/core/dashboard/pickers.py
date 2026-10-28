"""A text field with a filtered pick-list, like a command prompt.

Typing narrows the list, Up/Down move in it, Enter or Tab take the highlighted
entry, Esc closes it. Any text can still be typed, so a value that is not
listed (a custom ``module:Class``) is allowed.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, OptionList

from spars.core.experiments.defaults import PROJECT_ROOT, RESULTS_ROOT

MAX_LISTED = 50


def filter_options(options: list[str], query: str) -> list[str]:
    """Options containing ``query`` (case-insensitive), then those with its letters in order."""
    query = query.lower().strip()
    if not query:
        return options[:MAX_LISTED]

    def subsequence(text: str) -> bool:
        letters = iter(text)
        return all(char in letters for char in query)

    lowered = [(option, option.lower()) for option in options]
    contains = [o for o, low in lowered if query in low]
    loose = [o for o, low in lowered if query not in low and subsequence(low)]
    return (contains + loose)[:MAX_LISTED]


def data_files(subdirectory: str) -> list[str]:
    """Paths under data/<subdirectory> as experiments write them, e.g. workloads/json/x.json."""
    data = PROJECT_ROOT / "data"
    return sorted(
        path.relative_to(data).as_posix()
        for path in (data / subdirectory).rglob("*.json")
    )


def checkpoint_files() -> list[str]:
    if not RESULTS_ROOT.is_dir():
        return []
    paths = sorted(RESULTS_ROOT.rglob("*.pt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(path.relative_to(PROJECT_ROOT)) for path in paths[:200]]


class _Entry(Input):
    """The text box; forwards list keys to its Picker."""

    async def _on_key(self, event: events.Key) -> None:
        picker = next(a for a in self.ancestors if isinstance(a, Picker))
        if picker.use_key(event.key):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class Picker(Vertical):
    DEFAULT_CSS = """
    Picker { height: auto; }
    Picker > .picker-row { height: 1; }
    Picker .picker-label { width: 34; text-wrap: nowrap; text-overflow: ellipsis; }
    Picker Input { width: 64; }
    Picker OptionList { display: none; height: auto; max-height: 8; margin: 0 0 0 34; width: 1fr; }
    Picker.-open OptionList { display: block; }
    """

    class Changed(Message):
        def __init__(self, picker: "Picker") -> None:
            super().__init__()
            self.picker = picker

    def __init__(
        self,
        label: str,
        suggest: Callable[[], list[str]],
        value: str = "",
        *,
        id: str,
        placeholder: str = "",
        describe: Callable[[], dict[str, tuple[str, str]]] | None = None,
    ) -> None:
        super().__init__(id=id)
        self.label = label
        self.suggest = suggest
        self.describe = describe  # value -> (title, dim detail); shown instead of the bare value
        self.initial = value
        self.placeholder = placeholder
        self._shown: list[str] = []
        self._suppress = False

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Horizontal(classes="picker-row"):
            from .experiment_form import row_label

            yield row_label(self.label, "picker-label")
            yield _Entry(self.initial, placeholder=self.placeholder)
        yield OptionList()

    @property
    def entry(self) -> Input:
        return self.query_one(_Entry)

    @property
    def value(self) -> str:
        return self.entry.value.strip()

    def set_value(self, value: str) -> None:
        self._suppress = True
        self.entry.value = value
        self._suppress = False
        self.close()

    def open(self) -> None:
        self._refresh_list(self.entry.value)

    def close(self) -> None:
        self.remove_class("-open")

    @staticmethod
    def _row(value: str, details: dict[str, tuple[str, str]]) -> "Text | str":
        if value not in details:
            return value
        title, detail = details[value]
        return Text.assemble((title, "bold"), "  ", (detail, "dim"))

    def _refresh_list(self, query: str) -> None:
        options = self.suggest()
        self._shown = filter_options(options, query)
        listing = self.query_one(OptionList)
        listing.clear_options()
        details = self.describe() if self.describe else {}
        listing.add_options([self._row(value, details) for value in self._shown])
        if self._shown and not (len(self._shown) == 1 and self._shown[0] == query.strip()):
            listing.highlighted = 0
            self.add_class("-open")
        else:
            self.close()

    def on_input_changed(self, message: Input.Changed) -> None:
        message.stop()
        if self._suppress:
            return
        if self.entry.has_focus:
            self._refresh_list(message.value)
        self.post_message(self.Changed(self))

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, _Entry):
            self.open()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if isinstance(event.widget, _Entry):
            # Wait a moment: a mouse click on the list blurs the entry first.
            self.set_timer(0.2, self._close_unless_focused)

    def _close_unless_focused(self) -> None:
        if not (self.entry.has_focus or self.query_one(OptionList).has_focus):
            self.close()

    def use_key(self, key: str) -> bool:
        """True when the key was used for the list."""
        if not self.has_class("-open"):
            return False
        listing = self.query_one(OptionList)
        if key in ("down", "up"):
            listing.action_cursor_down() if key == "down" else listing.action_cursor_up()
            return True
        if key in ("enter", "tab") and listing.highlighted is not None:
            self.set_value(self._shown[listing.highlighted])
            self.entry.cursor_position = len(self.entry.value)
            self.post_message(self.Changed(self))
            return True
        if key == "escape":
            self.close()
            return True
        return False

    def on_option_list_option_selected(self, message: OptionList.OptionSelected) -> None:
        message.stop()
        self.set_value(self._shown[message.option_index])
        self.post_message(self.Changed(self))
        self.entry.focus()
