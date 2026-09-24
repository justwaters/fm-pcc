"""Chat TUI for Apple's on-device and Cloud Pro Foundation Models.

On-device goes through the `fm` CLI, using its own --resume/--save-transcript
flags for real multi-turn context. Cloud Pro has no public CLI/API yet, so it
shells out to a saved Shortcut (Receive input -> Use Cloud Pro model, bound to
Shortcut Input -> Stop and Output Response) via `shortcuts run`, stripping the
RTF it returns. Shortcuts has no scriptable session concept, so multi-turn
context there is approximated by re-sending prior turns as plain text.

The first time Cloud Pro is actually used, if the bridge shortcut isn't
installed yet, this opens its iCloud share link so the person can add it
through the normal Shortcuts "Add Shortcut" confirmation -- there's no way
(or business) silently writing into the Shortcuts library without that.
"""
import argparse
import difflib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable

from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, codework, taskplan

MODEL_LABELS = {
    "on-device": "on-device",
    "cloud": "cloud",
    "cloud-pro": "cloud pro",
    "ollama": "ollama",
}
MODEL_COLORS = {
    "on-device": "#f0b429",
    "cloud": "#20c997",
    "cloud-pro": "#38d9c9",
    "ollama": "#9775fa",
}
MODEL_ORDER = ["on-device", "cloud", "cloud-pro", "ollama"]


def model_label(model: str) -> str:
    """Display label for a model id -- including a specific "ollama:<tag>"."""
    if model in MODEL_LABELS:
        return MODEL_LABELS[model]
    if model.startswith("ollama:"):
        return model.split(":", 1)[1]
    return model


def model_color(model: str) -> str:
    """Accent color for a model id -- any "ollama:<tag>" gets ollama's color."""
    if model in MODEL_COLORS:
        return MODEL_COLORS[model]
    if model.startswith("ollama:"):
        return MODEL_COLORS["ollama"]
    return MODEL_COLORS["on-device"]


def model_family(model: str) -> str:
    """Collapse a specific "ollama:<tag>" back to the general "ollama" family."""
    return "ollama" if model.startswith("ollama:") else model


STATUS_ACCENTS = {
    "on-device": "#f0b429",
    "pcc": "#38d9c9",
    "ollama": "#9775fa",
}


def status_accent(model: str) -> str:
    """Coarser 3-way accent for the statusline: on-device, pcc (cloud and
    cloud-pro share one color here), or ollama -- unlike model_color(),
    which distinguishes all four model tiers individually for the input
    border and prompt glyph.
    """
    family = model_family(model)
    if family in ("cloud", "cloud-pro"):
        return STATUS_ACCENTS["pcc"]
    return STATUS_ACCENTS.get(family, STATUS_ACCENTS["on-device"])

# Each of these is a "Use Model" Shortcut (Receive input -> Use <tier> model,
# bound to Shortcut Input -> Stop and Output Response) sharing the same
# shape, just pointed at a different model tier.
CLOUD_SHORTCUTS = {
    "cloud": {
        "name": "PCC-Cloud",
        "url": "https://www.icloud.com/shortcuts/9f4e45968b974ef7ad8d29eb06f98a9b",
    },
    "cloud-pro": {
        "name": "PCC-CloudPro",
        "url": "https://www.icloud.com/shortcuts/7f7f8e41dfee459a89c28ca0b8c60984",
    },
}

_FILE_REF_RE = re.compile(r"@(\S+)")
MAX_FILE_CHARS = 8000
README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md"]


def find_readme(cwd: str) -> str | None:
    for name in README_NAMES:
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return path
    return None


def expand_file_references(prompt: str, cwd: str) -> tuple[str, list[str]]:
    """Inline the contents of any @path/to/file mentions found in `prompt`.

    Paths are resolved relative to `cwd` unless already absolute (~ is
    expanded too). `@readme` is a special alias that resolves to whichever
    actual README variant exists in `cwd` (README.md, README, etc.), since
    the real filename/extension varies by project. Mentions that don't
    resolve to a readable file are left alone -- so stray "@" text (an
    email, a handle) is harmless. Returns the prompt with file contents
    appended, and the list of resolved labels for display.
    """
    attachments: list[str] = []
    seen: set[str] = set()
    extra = ""

    for ref in _FILE_REF_RE.findall(prompt):
        if ref.lower() == "readme":
            path = find_readme(cwd)
            if path is None:
                continue
        else:
            path = os.path.expanduser(ref)
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            path = os.path.normpath(path)

        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)

        try:
            with open(path, "r", errors="replace") as f:
                content = f.read(MAX_FILE_CHARS + 1)
        except OSError:
            continue

        truncated = len(content) > MAX_FILE_CHARS
        content = content[:MAX_FILE_CHARS]
        label = os.path.relpath(path, cwd)
        attachments.append(label)
        suffix = "\n… (truncated)" if truncated else ""
        extra += f"\n\n--- {label} ---\n{content}{suffix}\n--- end {label} ---"

    return prompt + extra, attachments


ENVIRONMENT_HEADER_MAX_ENTRIES = 200


def build_environment_header(cwd: str) -> str:
    """A deterministic, real snapshot of the working directory -- top-level
    names only (matching /task's own directory scope: no subdirectory
    recursion, no dotfiles), not file contents (that's what @file is for).

    Without this, the model has zero actual filesystem awareness and will
    confidently guess a plausible-sounding but made-up cwd/listing -- verified
    directly against `fm respond` itself, not just fm-pcc's own prompting.
    Injected once, into the first message of a conversation (see
    on_input_submitted), rather than every turn: each backend's own
    multi-turn memory (on-device's --resume transcript, the cloud tiers'
    resend-as-text history, Ollama's native messages array) keeps it in view
    for the rest of that conversation without paying the cost again.
    """
    try:
        entries = sorted(e for e in os.listdir(cwd) if not e.startswith("."))
    except OSError as e:
        return f"Current directory: {cwd} (couldn't list contents: {e})"

    shown = entries[:ENVIRONMENT_HEADER_MAX_ENTRIES]
    listing = ", ".join(
        f"{e}/" if os.path.isdir(os.path.join(cwd, e)) else e for e in shown
    )
    if len(entries) > ENVIRONMENT_HEADER_MAX_ENTRIES:
        listing += f", … ({len(entries) - ENVIRONMENT_HEADER_MAX_ENTRIES} more not shown)"

    return f"Current directory: {cwd}\nContents: {listing or '(empty)'}"


class EditError(Exception):
    pass


def resolve_safe_path(path: str, cwd: str) -> str:
    """Resolve `path` relative to `cwd` and guarantee it stays inside it.

    Unlike the old flat "no path separators at all" rule, this allows real
    nesting (e.g. "test/path.txt" once "test" exists, or even before it
    does -- callers that write files create missing parent directories
    themselves) since folder creation/organization requires it. What it
    still refuses: absolute paths, and any ".." that would escape `cwd`.
    """
    if not path or path in (".", ".."):
        raise EditError(f"refusing an invalid path: {path!r}")
    if os.path.isabs(path):
        raise EditError(f"refusing an absolute path: {path!r}")

    full = os.path.normpath(os.path.join(cwd, path))
    cwd_norm = os.path.normpath(cwd)
    if full != cwd_norm and not full.startswith(cwd_norm + os.sep):
        raise EditError(f"refusing a path outside the working directory: {path!r}")
    return full


_REWRITE_SCHEMA = {
    "type": "object", "title": "Rewrite", "additionalProperties": False,
    "properties": {"content": {"type": "string", "description": "The complete updated text"}},
    "required": ["content"], "x-order": ["content"],
}
_NEW_FILE_SCHEMA = {
    "type": "object", "title": "NewFile", "additionalProperties": False,
    "properties": {"content": {"type": "string", "description": "The complete contents of the new file"}},
    "required": ["content"], "x-order": ["content"],
}
# The file appears in both the prompt and the reply, and on-device has a
# 4096-token context -- past this, edit one section at a time instead.
REWRITE_MAX_CHARS = 4000
# First attempt is greedy (deterministic); retries sample, with the
# previous attempt's problem spelled out, since a greedy retry of the
# same prompt would just reproduce the same mistake.
EDIT_ATTEMPTS = 4
# Past this size, a request that names one function edits just that
# function rather than rewriting the whole file.
NAMED_FUNCTION_EDIT_MIN_CHARS = 1500
# No new attempt starts after this long -- a runaway generation can take
# over a minute on its own.
EDIT_TIME_BUDGET_SECONDS = 90


# Code is generated as plain text in a fenced block, not inside a JSON
# string: measured on the same eight coding prompts, the on-device model
# got 8/8 right in plain text and 5/8 through guided generation, and the
# JSON route sometimes wrote literal "\n" sequences into source files.
# Prose-like files keep guided generation, which has no fence to confuse
# with a markdown file's own code blocks.
PROSE_EXTS = {".md", ".markdown", ".txt", ".rst", ".cfg", ".ini", ".env", ".yml", ".yaml", ".toml", ".csv", ""}
GENERATION_TIMEOUT_SECONDS = 75
CONTEXT_BUDGET_ON_DEVICE = 3000   # characters of other files shown alongside an edit
CONTEXT_BUDGET_CLOUD = 16000


_FENCE_LANGS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx", ".swift": "swift", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".php": "php",
    ".sh": "bash", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".json": "json",
    ".sql": "sql",
}


def _is_prose(label: str) -> bool:
    return os.path.splitext(label)[1].lower() in PROSE_EXTS


def fm_code(prompt: str, greedy: bool = True) -> str:
    """One on-device call answered in plain text; returns the code from its
    fenced block."""
    args = ["fm", "respond", "--model", "system", "--no-stream"]
    if greedy:
        args.append("--greedy")
    result = _run(
        [*args, prompt + "\n\nReply with only the complete code in one ``` code block, nothing else."],
        timeout=GENERATION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise EditError(result.stderr.strip() or "fm respond failed")
    return taskplan.extract_code_block(result.stdout)


def _generator(label: str) -> Callable[[str, bool], str]:
    if _is_prose(label):
        return lambda prompt, greedy: fm_structured(_REWRITE_SCHEMA, prompt, greedy).get("content") or ""
    return fm_code


def _context_block(context: str, task: str, instructions: str) -> str:
    parts = []
    if context:
        parts.append(f"Other project files, for reference only (don't rewrite them):\n\n{context}\n")
    if task and task.strip() != instructions.strip():
        parts.append(f"The overall request this is part of: {task}\n")
    return "\n".join(parts) + ("\n" if parts else "")


def fm_structured(schema: dict, prompt: str, greedy: bool = True) -> dict:
    """One on-device call constrained to `schema` (guided generation), so
    the reply is always parseable and enum fields can't hold anything
    outside their allowed values."""
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        with open(schema_path, "w") as f:
            json.dump(schema, f)
        args = ["fm", "respond", "--model", "system", "--no-stream", "--schema", schema_path]
        if greedy:
            args.append("--greedy")
        result = _run([*args, prompt], timeout=GENERATION_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise EditError(result.stderr.strip() or "fm respond failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise EditError(f"model didn't return valid structured output: {result.stdout[:200]!r}")


def _generate_checked(
    prompt: str,
    generate: Callable[[str, bool], str],
    check: Callable[[str], list[str]],
    original: str = "",
    repair: Callable[[str], str] = lambda text: text,
    sample_first: bool = False,
) -> str:
    """Generate text, retrying with feedback until `check` finds no
    problems. Raises EditError if no attempt passes -- a wrong change is
    never returned as if it were right. `sample_first` skips the greedy
    first attempt (for fixing something a greedy attempt already got
    wrong).
    """
    problems: list[str] = []
    deadline = time.monotonic() + EDIT_TIME_BUDGET_SECONDS
    for attempt in range(EDIT_ATTEMPTS):
        if attempt and time.monotonic() > deadline:
            break
        feedback = ""
        if problems:
            feedback = (
                "\n\nA previous attempt was rejected: " + "; ".join(problems)
                + ". Fix that this time."
            )
        try:
            reply = generate(prompt + feedback, attempt == 0 and not sample_first)
        except EditError as e:
            # Seen for real: a sampled retry that never stops generating
            # until it overflows the context. That's one failed attempt,
            # not a reason to give up on the rest.
            problems = [f"the model's reply failed ({e})"]
            continue
        reply = taskplan.unescape_literal_newlines(original, reply)
        text = repair(taskplan.strip_code_fence(reply, original))
        problems = check(text)
        if not problems:
            return text
    raise EditError(f"couldn't produce a correct change after {EDIT_ATTEMPTS} tries: {'; '.join(problems)}")


def propose_edit(
    path: str,
    instructions: str,
    cwd: str,
    line_range: tuple[int, int] | None = None,
    speculative: bool = False,
    context: str = "",
    task: str = "",
    feedback: str = "",
    expectations: bool = True,
) -> dict:
    """Ask the on-device model to rewrite `path` (or just `line_range` of
    it, 1-based inclusive) with `instructions` applied.

    Measured on the real model, rewriting the text outright is far more
    reliable than the alternatives tried: line-anchored single edits
    couldn't delete or change more than one line, and structured line
    operations (replace/insert/delete by number) routinely overwrote the
    wrong line. Rewriting gets the change right in most cases, and the
    rest are caught by taskplan.check_edit() (derived from the request
    itself) and retried rather than written.

    `speculative` marks an edit the planner chose on its own (the request
    named no file): its change must also be relevant to the request, or
    it's rejected rather than written. `context` (other files), `task`
    (the whole request this edit is part of), and `feedback` (e.g. a
    failing test's output) go into the prompt only; the checks use
    `instructions`.
    """
    full_path = os.path.expanduser(path)
    if not os.path.isabs(full_path):
        full_path = os.path.join(cwd, full_path)
    full_path = os.path.normpath(full_path)
    label = os.path.relpath(full_path, cwd)

    if not os.path.isfile(full_path):
        raise EditError(f"no such file: {label}")

    with open(full_path, "r", errors="replace") as f:
        original = f.read()

    # A whole-theme re-color is done across the entire file in code, so
    # it isn't limited to one section and needs no model call.
    if taskplan.is_theme_request(instructions):
        recolored = None
        if os.path.splitext(label)[1].lower() in (".css", ".scss", ".less", ".html", ".htm", ".svg"):
            recolored = taskplan.recolor_theme(original, instructions)
        if recolored is not None:
            return {
                "path": full_path, "label": label, "original": original, "updated": recolored,
                "summary": f"re-colored {label}: {instructions}",
            }
        if speculative:
            raise EditError("it has no colors to change")

    def result(updated: str, summary: str) -> dict:
        return {"path": full_path, "label": label, "original": original, "updated": updated, "summary": summary}

    # Adding or setting a key in JSON is done by parsing it, not by a model
    # rewrite (which, measured, broke the closing brace).
    if label.endswith(".json") and not line_range and (assignment := taskplan.json_assignment(instructions)):
        try:
            updated = codework.json_set(original, *assignment)
        except (ValueError, TypeError, AttributeError):
            updated = None
        if updated is not None and updated != original:
            return result(updated, f"set {assignment[0]} in {label}")

    # "Add a docstring to every function": one function at a time -- asked
    # to do all of them in one rewrite, the model returned the file
    # unchanged, every time.
    if not line_range and not _is_prose(label) and taskplan.applies_to_every_function(instructions):
        updated = _edit_each_function(label, original, instructions, context, task)
        if updated is not None:
            return result(updated, f"edited {label}: {instructions}")

    lines = original.splitlines()
    lo, hi = line_range if line_range else (1, len(lines))
    excerpt = "\n".join(lines[lo - 1 : hi])
    if len(excerpt) > REWRITE_MAX_CHARS:
        raise EditError(
            f"{label}{f' (lines {lo}-{hi})' if line_range else ''} is too "
            f"large to edit on-device (over {REWRITE_MAX_CHARS} characters)"
        )

    if line_range:
        what = f"lines {lo}-{hi} of {label} (the rest of the file is unchanged)"
        noun = "version of just these lines"
    else:
        what = f"the file {label}"
        noun = "file"
    if _is_prose(label):
        prompt = (
            _context_block(context, task, instructions)
            + f"Here is {what}:\n\n{excerpt}\n\n"
            + f"Change request: {instructions}\n\n"
            + (f"{feedback}\n\n" if feedback else "")
            + f"Write the complete updated {noun}. Keep every existing line exactly "
            f"as it is unless the change request is about it, and make the "
            f"requested change."
        )
    else:
        # Task first, asking for a correct implementation rather than a
        # minimal one, and a function taken from a bigger file shown as if
        # it were the whole file (it's spliced back in afterwards) --
        # measured on six single-function fixes: 6/6 this way, 5/6 when
        # framed as "lines X-Y of the file" or "the function X", and
        # "keep every existing line exactly as it is" wording produced
        # timid, wrong changes.
        lang = _FENCE_LANGS.get(os.path.splitext(label)[1].lower(), "")
        prompt = (
            _context_block(context, task, instructions)
            + f"Task: {instructions}\n\n"
            + (f"{feedback}\n\n" if feedback else "")
            + f"Current {label}:\n```{lang}\n{excerpt}\n```\n\n"
            + "Write the complete updated file that does this. Make sure the code actually "
            "implements the request correctly; keep unrelated code unchanged."
            # (No "standard library only" line here: measured, it cost a
            # correct answer on one of six single-function fixes. New
            # files get it, and new scripts are test-run anyway.)
        )
    literal = taskplan.literal_edit(instructions, excerpt)
    if _is_palette_request(label, instructions, excerpt):
        new_excerpt = _propose_palette(label, instructions, excerpt)
    elif literal is not None and not feedback and not taskplan.check_edit(instructions, excerpt, literal):
        new_excerpt = literal
    else:
        new_excerpt = _generate_checked(
            prompt, _generator(label),
            lambda text: taskplan.check_edit(
                instructions, excerpt, text, code=not _is_prose(label), expectations=expectations
            )
            + taskplan.check_structure(label, instructions, excerpt, text, whole_file=original)
            + taskplan.check_comment_request(label, instructions, excerpt, text)
            + ([] if _is_prose(label) else codework.check_definitions(
                label, instructions, excerpt, text, elsewhere=codework.names_defined_elsewhere(cwd, label)))
            + (taskplan.check_relevant(instructions, excerpt, text) if speculative else []),
            original=excerpt,
            repair=lambda text: _drop_copied(label, excerpt, taskplan.match_indentation(
                excerpt,
                taskplan.restore_layout(excerpt, text) if taskplan.looks_reflowed(excerpt, text) else text,
            ), cwd),
            sample_first=bool(feedback),
        )

    updated_lines = lines[: lo - 1] + new_excerpt.splitlines() + lines[hi:]
    updated = "\n".join(updated_lines)
    if original.endswith("\n") or not original:
        updated += "\n"

    return {
        "path": full_path,
        "label": label,
        "original": original,
        "updated": updated,
        "summary": f"edited {label}: {instructions}",
    }


def _drop_copied(label: str, before: str, text: str, cwd: str) -> str:
    """Remove definitions the model copied in from another file (shown to
    it as context) -- asked not to, it still did; removing them keeps its
    actual change."""
    if _is_prose(label):
        return text
    copied = (codework.top_level_names(label, text) - codework.top_level_names(label, before)) \
        & codework.names_defined_elsewhere(cwd, label)
    return codework.drop_definitions(label, text, copied) if copied else text


def _edit_each_function(label: str, original: str, instructions: str, context: str, task: str) -> str | None:
    """Apply `instructions` to each function separately, bottom-up so line
    numbers stay valid. None if the file has no functions to go through."""
    blocks = codework.function_blocks(label, original)
    # Nested functions are handled as part of the function around them.
    blocks = [b for b in blocks if not any(o != b and o[1] <= b[1] and b[2] <= o[2] for o in blocks)]
    if not blocks:
        return None
    lines = original.splitlines()
    for name, start, end in sorted(blocks, key=lambda b: -b[1]):
        block = "\n".join(lines[start:end])
        per = f"{instructions} (this is one of those functions: apply it to {name})"
        prompt = (
            _context_block(context, task, instructions)
            + f"Here is the function {name} from {label}:\n\n```\n{block}\n```\n\n"
            f"Change request: {per}\n\nWrite the complete updated function."
        )
        new_block = _generate_checked(
            prompt, fm_code,
            lambda text, block=block, name=name: taskplan.check_edit(instructions, block, text, code=True)
            + taskplan.check_structure(label, instructions, block, text)
            + ([] if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text) else [f"{name} is no longer defined"]),
            original=block,
            repair=lambda text, block=block: taskplan.match_indentation(block, text),
        )
        indent = re.match(r"[ \t]*", lines[start]).group()
        new_lines = new_block.splitlines()
        if indent and new_lines and not new_lines[0].startswith(indent):
            new_lines = [indent + l if l.strip() else l for l in new_lines]
        lines[start:end] = new_lines
    return "\n".join(lines) + ("\n" if original.endswith("\n") else "")


def _is_palette_request(label: str, instructions: str, text: str) -> bool:
    return (
        os.path.splitext(label)[1].lower() in (".css", ".scss", ".less")
        and bool(re.search(r"colou?r|theme|palette|accent|brand|dark\s+mode|light\s+mode", instructions, re.I)
                 or taskplan.requested_color_words(instructions))
        and len(taskplan.palette_variables(text)) >= 2
    )


def _propose_palette(label: str, instructions: str, text: str) -> str:
    """Re-color a stylesheet through its color custom properties: the model
    only picks new values for existing variable names (guided generation,
    names constrained to an enum), and code substitutes them -- so the
    rewrite can't rename or drop a variable or break a comment, which a
    free-text rewrite of a real stylesheet did."""
    variables = taskplan.palette_variables(text)
    old_values = {name: value for _, name, value in variables}
    schema = {
        "type": "object", "title": "Palette", "additionalProperties": False,
        "properties": {"colors": {"type": "array", "items": {"$ref": "#/$defs/Color"}}},
        "required": ["colors"], "x-order": ["colors"],
        "$defs": {"Color": {
            "type": "object", "title": "Color", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "enum": list(old_values)},
                "value": {"type": "string", "description": "The new CSS color, as a hex code like #1f7a3a"},
            },
            "required": ["name", "value"], "x-order": ["name", "value"],
        }},
    }
    listing = "\n".join(f"{name}: {value}" for name, value in old_values.items())
    prompt = (
        f"These are the color variables in {label}:\n\n{listing}\n\n"
        f"Change request: {instructions}\n\n"
        f"Give new hex colors for every variable that should change to carry out this "
        f"request. Keep each variable's role: backgrounds stay light or dark as they are "
        f"now, and text keeps enough contrast against its background."
    )
    problems: list[str] = []
    for attempt in range(EDIT_ATTEMPTS):
        feedback = f"\n\nA previous attempt was rejected: {'; '.join(problems)}." if problems else ""
        try:
            reply = fm_structured(schema, prompt + feedback, greedy=attempt == 0)
        except EditError as e:
            problems = [f"the model's reply failed ({e})"]
            continue
        new_values = {c["name"]: c["value"].strip() for c in reply.get("colors") or [] if c.get("name") in old_values}
        problems = taskplan.check_palette(instructions, old_values, new_values)
        if not problems:
            changed = {k: v for k, v in new_values.items() if v.lower() != old_values[k].lower()}
            return taskplan.apply_palette(text, changed)
    raise EditError(f"couldn't produce a correct palette after {EDIT_ATTEMPTS} tries: {'; '.join(problems)}")


def propose_new_file(
    path: str, instructions: str, cwd: str, context: str = "", task: str = "", expectations: bool = True
) -> dict:
    """Write a brand new file -- empty if `instructions` is blank (no model
    call needed), otherwise written by the on-device model and checked
    against the request ("containing the word hello" -> it does).

    Returns the same shape propose_edit() does (path, label, original,
    updated, summary), with `original` always "", so /task's write/diff/undo
    plumbing doesn't need to distinguish creating a file from editing one.
    `path` may be nested (e.g. "test/path.txt") -- any missing parent
    directories are created by the caller. Refuses to escape `cwd`, and
    refuses to clobber a file that already exists.
    """
    full_path = resolve_safe_path(path, cwd)
    label = os.path.relpath(full_path, cwd)

    if os.path.exists(full_path):
        raise EditError(f"{label} already exists -- edit it instead of creating it")

    if not instructions.strip():
        return {"path": full_path, "label": label, "original": "", "updated": "",
                "summary": f"created empty file {label}"}

    prompt = (
        _context_block(context, task, instructions)
        + f"Create a new file at {label}.\n\n"
        f"What it should contain: {instructions}\n\n"
        f"Write the complete contents of this file -- only the file's own "
        f"contents, no commentary."
        + ("" if _is_prose(label) else
           " Use only the standard library and the project's own code unless the project already uses a package.")
    )
    generate = (
        (lambda p, g: fm_structured(_NEW_FILE_SCHEMA, p, g).get("content") or "")
        if _is_prose(label) else fm_code
    )
    content = _generate_checked(
        prompt, generate,
        lambda text: taskplan.check_new_file(instructions, text, expectations=expectations, code=not _is_prose(label))
        + taskplan.check_structure(label, instructions, "", text),
    )
    if content and not content.endswith("\n"):
        content += "\n"
    return {
        "path": full_path,
        "label": label,
        "original": "",
        "updated": content,
        "summary": f"created {label}",
    }


def create_folder(path: str, cwd: str) -> dict:
    """Create a new, empty directory. Deterministic, no model involved --
    making a folder needs no judgment call, just a real filesystem op.
    """
    full_path = resolve_safe_path(path, cwd)
    label = os.path.relpath(full_path, cwd)
    if os.path.exists(full_path):
        raise EditError(f"{label} already exists")
    os.makedirs(full_path)
    return {"path": full_path, "label": label, "summary": f"created folder {label}"}


def move_or_rename(src: str, dest: str, cwd: str) -> dict:
    """Move or rename a file or folder within `cwd`. Same reasoning as
    create_folder: no model judgment needed, just a real filesystem op --
    the model only ever supplies the source and destination paths.
    """
    src_full = resolve_safe_path(src, cwd)
    dest_full = resolve_safe_path(dest, cwd)
    src_label = os.path.relpath(src_full, cwd)
    dest_label = os.path.relpath(dest_full, cwd)

    if not os.path.exists(src_full):
        raise EditError(f"no such file or folder: {src_label}")
    if os.path.exists(dest_full):
        raise EditError(f"{dest_label} already exists")

    os.makedirs(os.path.dirname(dest_full), exist_ok=True)
    shutil.move(src_full, dest_full)
    return {
        "src_path": src_full,
        "src_label": src_label,
        "dest_path": dest_full,
        "dest_label": dest_label,
        "summary": f"moved {src_label} to {dest_label}",
    }


def git_run(args: list[str], cwd: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)


def git_status_porcelain(cwd: str) -> str:
    result = git_run(["status", "--porcelain"], cwd, timeout=10)
    if result.returncode != 0:
        raise EditError("not a git repository here (or git isn't available)")
    return result.stdout


def git_has_unpushed_commits(cwd: str) -> bool:
    """True if HEAD is ahead of its upstream. Best-effort: no upstream
    configured (e.g. a branch that's never been pushed) isn't treated as a
    real answer either way -- the caller should push anyway and let git's
    own error explain what's actually wrong, rather than this silently
    reporting "false" and _handle_push refusing to even try.
    """
    result = git_run(["rev-list", "@{u}..HEAD", "--count"], cwd, timeout=10)
    if result.returncode != 0:
        return True
    return result.stdout.strip() not in ("", "0")


def git_add_all(cwd: str) -> None:
    result = git_run(["add", "-A"], cwd)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())


def git_commit(message: str, cwd: str) -> str:
    result = git_run(["commit", "-m", message], cwd)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def git_pull(cwd: str) -> str:
    result = git_run(["pull"], cwd, timeout=120)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def git_push(cwd: str) -> str:
    result = git_run(["push"], cwd, timeout=120)
    if result.returncode != 0 and "no upstream branch" in (result.stderr or ""):
        # A branch that's never been pushed. The user already asked to
        # push (/push is always explicit), so finish the job: publish it
        # to the repo's remote -- "origin", or the only remote there is.
        remotes = git_run(["remote"], cwd, timeout=10).stdout.split()
        remote = "origin" if "origin" in remotes else (remotes[0] if len(remotes) == 1 else None)
        branch = git_branch(cwd)
        if remote and branch:
            result = git_run(["push", "--set-upstream", remote, branch], cwd, timeout=120)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def git_create_branch(name: str, cwd: str) -> str:
    result = git_run(["checkout", "-b", name], cwd, timeout=30)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def git_switch_branch(name: str, cwd: str) -> str:
    result = git_run(["checkout", name], cwd, timeout=30)
    if result.returncode != 0:
        raise EditError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def diff_preview(original: str, updated: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
    )
    return "".join(diff)


_BRACE_LANGUAGES = {".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".java", ".c", ".cpp", ".swift"}
_SECTION_CHUNK_LINES = 40


def split_sections(content: str, filename: str) -> list[dict]:
    """Split a file into named, 1-based-inclusive line-range sections.

    This is deliberately not model-driven: brace-delimited languages have
    unambiguous top-level block boundaries a few lines of Python can find
    reliably, which is exactly the kind of mechanical task a small model is
    bad at (see propose_edit's own docstring). Anything else falls back to
    fixed-size chunks -- cruder, but still gives the picker something
    bounded to choose between.
    """
    lines = content.splitlines()
    if not lines:
        return [{"name": "(empty file)", "start": 1, "end": 1}]

    ext = os.path.splitext(filename)[1].lower()
    if ext in _BRACE_LANGUAGES:
        sections = _split_top_level_braces(lines)
        if sections:
            return sections

    return [
        {
            "name": f"lines {i + 1}-{min(i + _SECTION_CHUNK_LINES, len(lines))}",
            "start": i + 1,
            "end": min(i + _SECTION_CHUNK_LINES, len(lines)),
        }
        for i in range(0, len(lines), _SECTION_CHUNK_LINES)
    ]


def _split_top_level_braces(lines: list[str]) -> list[dict]:
    sections = []
    depth = 0
    start = None
    for i, line in enumerate(lines):
        if depth == 0 and start is None and line.strip():
            start = i
        depth += line.count("{") - line.count("}")
        if depth <= 0 and start is not None:
            sections.append(
                {"name": lines[start].strip()[:48], "start": start + 1, "end": i + 1}
            )
            start = None
            depth = 0
    if start is not None:
        sections.append({"name": lines[start].strip()[:48], "start": start + 1, "end": len(lines)})
    return sections


_TASK_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
TASK_MAX_CANDIDATES = 50


def gather_task_tree(cwd: str) -> tuple[list[str], list[str]]:
    """Relative file and folder paths inside `cwd`, walked recursively but
    skipping dotfiles/dirs and common noise directories -- each capped so a
    large tree doesn't blow the planner's context budget, and so a new
    folder's contents are visible to /task's very next step rather than
    only the top level.
    """
    files: list[str] = []
    folders: list[str] = []
    for root, dirs, filenames in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _TASK_SKIP_DIRS)
        for d in dirs:
            folders.append(os.path.relpath(os.path.join(root, d), cwd))
        for f in sorted(filenames):
            if not f.startswith("."):
                files.append(os.path.relpath(os.path.join(root, f), cwd))
        if len(files) >= TASK_MAX_CANDIDATES and len(folders) >= TASK_MAX_CANDIDATES:
            break
    return files[:TASK_MAX_CANDIDATES], folders[:TASK_MAX_CANDIDATES]


# /push already requires an explicit, separate action from the user rather
# than firing automatically -- these four get the same treatment: /task can
# plan them, but never executes them itself, only surfaces the matching
# command (/push, /branch create|switch, /pull) for the user to run.
TASK_CONFIRM_GATED = {"PUSH", "BRANCH_CREATE", "BRANCH_SWITCH", "PULL", "RUN"}
# After changing code, /task runs the project's own checks (syntax checks,
# its test suite, anything the request said to run) and, on a failure,
# shows the model the output and has it fix the file -- up to this many
# rounds before giving up and saying so.
VERIFY_ROUNDS = 3

_PLAN_SCHEMA = {
    "type": "object", "title": "Plan", "additionalProperties": False,
    "properties": {"steps": {"type": "array", "items": {"$ref": "#/$defs/Step"}}},
    "required": ["steps"], "x-order": ["steps"],
    "$defs": {"Step": {
        "type": "object", "title": "Step", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": taskplan.MODEL_ACTIONS},
            "path": {"type": "string", "description":
                     "The existing file/folder acted on, the new path for CREATE_*, or the branch name"},
            "destination": {"type": "string", "description": "RENAME/MOVE only: the new path. Otherwise empty."},
            "details": {"type": "string", "description":
                        "EDIT/CREATE_FILE: what to change or write. COMMIT: the commit message. Otherwise empty."},
        },
        "required": ["action", "path", "destination", "details"],
        "x-order": ["action", "path", "destination", "details"],
    }},
}


def _plan_prompt(request: str, task: str, files: list[str], folders: list[str], code: str = "") -> str:
    context = f"Overall task: {task}\nPart to plan now: {request}\n\n" if request != task else f"Task: {task}\n\n"
    return (
        context
        + (f"Relevant project code:\n\n{code}\n\n" if code else "")
        + f"Existing files: {', '.join(files) or '(none)'}\n"
        + f"Existing folders: {', '.join(folders) or '(none)'}\n\n"
        "List the steps that carry out exactly this request, in order -- "
        "nothing it didn't ask for.\n"
        "- EDIT: path = existing file to change; details = the change.\n"
        "- CREATE_FILE: path = new file; details = what it should contain.\n"
        "- CREATE_FOLDER: path = new folder.\n"
        "- RENAME: path = current path; destination = new path.\n"
        "- MOVE: path = current path; destination = full new path (folder/filename).\n"
        "- COMMIT: details = the commit message. Commits all changes.\n"
        "- PUSH, PULL: no fields.\n"
        "- BRANCH_CREATE, BRANCH_SWITCH: path = branch name.\n"
        "All paths are relative to the current directory.\n\n"
        "Examples (different files, same kinds of request):\n"
        '- "notes.txt should be called todo.txt" -> RENAME path=notes.txt destination=todo.txt\n'
        '- "get report.pdf out of the old folder" (it is at old/report.pdf) -> '
        "MOVE path=old/report.pdf destination=report.pdf\n"
        '- "the css files belong in styles" (a.css and b.css exist) -> MOVE path=a.css '
        "destination=styles/a.css, MOVE path=b.css destination=styles/b.css\n"
        "For RENAME/MOVE, path is always something that exists now, and destination is "
        "always different from path."
    )


def _parse_json_plan(reply: str) -> list[dict] | None:
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return None
    try:
        steps = json.loads(match.group()).get("steps")
    except (json.JSONDecodeError, AttributeError):
        return None
    return steps if isinstance(steps, list) else None


def plan_with_model(
    request: str, task: str, files: list[str], folders: list[str], backend: "Backend", model: str,
    cwd: str | None = None,
) -> tuple[list[dict], str]:
    """Ask the planning model for raw steps (not yet normalized). A cloud
    model gets asked for JSON in plain text (Shortcuts has no schema
    support); on-device -- directly, or because the cloud tiers fell back
    to it -- uses guided generation, so its action is always one of the
    allowed ones. Returns (steps, model actually used).
    """
    # The planner sees real code, not just file names -- without it,
    # measured, it asked to "fix" a failing test by editing the test.
    budget = CONTEXT_BUDGET_ON_DEVICE if model == "on-device" else CONTEXT_BUDGET_CLOUD
    code = codework.build_context(cwd, task, budget) if cwd else ""
    prompt = _plan_prompt(request, task, files, folders, code)
    if model != "on-device":
        reply, used = backend.classify_with_fallback(
            prompt + '\n\nReply with only JSON: {"steps": [{"action": ..., "path": ..., '
            '"destination": ..., "details": ...}]}',
            model,
        )
        if used != "on-device":
            steps = _parse_json_plan(reply)
            if steps is not None:
                return steps, used
        model = used
    if budget != CONTEXT_BUDGET_ON_DEVICE and cwd:
        # Fell back from a cloud tier: on-device needs the smaller context.
        small = codework.build_context(cwd, task, CONTEXT_BUDGET_ON_DEVICE)
        prompt = _plan_prompt(request, task, files, folders, small)
    return fm_structured(_PLAN_SCHEMA, prompt).get("steps") or [], model


_ABOUT_TESTS_RE = re.compile(
    r"\b(?:fix|update|change|edit|correct|rewrite|write|add|create|remove|delete)\s+(?:the\s+|a\s+|some\s+|more\s+)?"
    r"(?:unit\s+)?tests?\b(?!\s+(?:fails?|failing|passes?|pass))|\btests?\s+(?:itself|file)\b",
    re.IGNORECASE,
)


def _retarget_test_edits(steps: list[dict], task: str, cwd: str, files: list[str]) -> list[dict]:
    """"The test in test_x.py fails -- fix the code": edit the code under
    test, not the test (measured: the planner edited the test). The test
    file's contents go along as context."""
    if _ABOUT_TESTS_RE.search(task):
        return steps
    out = []
    for s in steps:
        if s["action"] != "EDIT" or not codework.is_test_file(s["path"]):
            out.append(s)
            continue
        test_src = codework.read_text(cwd, s["path"])
        names = [a or b for a, b in re.findall(
            r"^\s*from\s+([\w.]+)\s+import|^\s*import\s+([\w.]+)", test_src, re.MULTILINE)]
        names += [a or b for a, b in re.findall(
            r"require\(\s*['\"]\./([\w./-]+)['\"]\s*\)|from\s+['\"]\./([\w./-]+)['\"]", test_src)]
        targets = []
        for module in names:
            name = module.replace(".", "/") if not module.endswith((".js", ".ts")) else module
            for cand in (f"{name}.py", f"{name}.js", f"{name}.ts", name):
                if cand in files and not codework.is_test_file(cand) and cand not in targets:
                    targets.append(cand)
        if not targets:
            out.append(s)
            continue
        for t in targets:
            out.append({**s, "path": t, "details": s["details"],
                        "context_files": [s["path"]]})
    return out


def plan_task(
    task: str, files: list[str], folders: list[str], backend: "Backend", model: str,
    cwd: str | None = None,
) -> tuple[list[dict], str | None]:
    """The whole plan for `task`, up front: deterministic where the request
    has a recognizable shape (taskplan.parse_task), the planning model for
    any part that doesn't. Returns (steps, planning model used or None).
    A part the model can't turn into any valid step comes back as an
    UNSUPPORTED step, so it's reported rather than silently skipped.
    """
    parsed = taskplan.parse_task(task, files, folders)
    unparsed = [s for s in parsed if s["action"] == "UNPARSED"]
    if not unparsed:
        return (_retarget_test_edits(parsed, task, cwd, files) if cwd else parsed), None

    whole = len(unparsed) == len(parsed)
    steps: list[dict] = []
    model_used: str | None = None
    for s in parsed:
        if s["action"] != "UNPARSED":
            steps.append(s)
            continue
        request = task if whole else s["details"]
        now_files, now_folders = s.get("files", files), s.get("folders", folders)
        raw, model_used = plan_with_model(request, task, now_files, now_folders, backend, model, cwd)
        named = taskplan.mentioned_files(request, now_files)
        related = tuple(r for n in named for r in codework.local_imports(cwd, n, now_files)) if cwd else ()
        try:
            planned = taskplan.normalize_model_steps(raw, request, now_files, now_folders, related)
        except ValueError as e:
            planned = [taskplan.step("UNSUPPORTED", details=f"{request!r} ({e})")]
        # The planner's summary of an edit replaces the user's own words
        # -- measured, it turned "it should include b" into "Add b to the
        # range_sum function". Keep the request itself as the instructions;
        # with several files, add the planner's note for this one.
        content_steps = [p for p in planned if p["action"] in ("EDIT", "CREATE_FILE")]
        for p in content_steps:
            note = p["details"].strip()
            p["details"] = request
            if len(content_steps) > 1:
                # One request covering several files: the planner's note
                # says what this file is for. It goes to the model as a
                # hint -- never into the checks, which would otherwise
                # demand the whole request's wording in every file (seen
                # for real: a note holding a whole draft of index.html).
                p["note"] = note[:1500]
                p["shared"] = True
        steps.extend(planned or [taskplan.step("UNSUPPORTED", details=f"work out how to do {request!r}")])
        if whole:
            break
    if cwd:
        steps = _retarget_test_edits(steps, task, cwd, files)
    return steps, model_used


def describe_step(s: dict) -> str:
    action, path, dest, details = s["action"], s["path"], s["destination"], s["details"]
    if action in ("RENAME", "MOVE"):
        return f"{action.lower()} {path} → {dest}"
    if action == "EDIT":
        return f"edit {path}: {details}"
    if action == "CREATE_FILE":
        return f"create file {path}" + (f": {details}" if details else "")
    if action == "CREATE_FOLDER":
        return f"create folder {path}"
    if action == "COMMIT":
        return f"commit: {details}" if details else "commit"
    if action == "STAGE":
        return "stage all changes"
    if action in ("BRANCH_CREATE", "BRANCH_SWITCH"):
        return f"{'create' if action == 'BRANCH_CREATE' else 'switch to'} branch {path}"
    if action == "UNSUPPORTED":
        return f"can't do: {details}"
    if action == "RENAME_SYMBOL":
        return f"rename {dest} → {details}" + (f" in {path}" if path else " everywhere")
    if action == "MOVE_CODE":
        return f"move {details} from {path} into {dest}"
    if action == "TEST":
        return "run the tests"
    if action == "FIX_CHECKS":
        return "run the tests/checks and fix what fails"
    if action == "RUN":
        return f"run {details}"
    return action.lower()


def choose_edit_range(
    content: str, filename: str, instructions: str, backend: "Backend", model: str
) -> tuple[int, int] | None:
    """None if the whole file fits in one on-device rewrite; otherwise the
    section to edit -- picked deterministically when the request names
    something (a function, a heading) that only one section contains, and
    by the planning model otherwise.
    """
    # A request naming a function edits exactly that function, in any code
    # file past a small size: measured, the model can't faithfully
    # reproduce a 3 KB file of 60 functions to change one of them, and a
    # fixed 40-line chunk can cut a function in half.
    if len(content) > NAMED_FUNCTION_EDIT_MIN_CHARS:
        names = set(re.findall(r"[A-Za-z_$][\w$]*", instructions))
        blocks = [b for b in codework.function_blocks(filename, content) if b[0] in names]
        if len(blocks) == 1:
            _name, start, end = blocks[0]
            if len("\n".join(content.splitlines()[start:end])) <= REWRITE_MAX_CHARS:
                return start + 1, end
    if len(content) <= REWRITE_MAX_CHARS:
        return None
    sections = split_sections(content, filename)
    lines = content.splitlines()
    # A color/theme change to a stylesheet belongs in its custom-property
    # palette (":root { --brand: ...; }") when it has one: one block that
    # restyles everything, instead of one of hundreds of rules.
    if os.path.splitext(filename)[1].lower() in (".css", ".scss", ".less") and re.search(
        r"colou?r|theme|palette|background|accent|brand|dark\s+mode|light\s+mode|"
        r"\b(?:red|green|blue|yellow|orange|purple|pink|black|white|gr[ae]y|teal|navy)\b",
        instructions, re.IGNORECASE,
    ):
        for s in sections:
            body = "\n".join(lines[s["start"] - 1 : s["end"]])
            if s["name"].startswith(":root") and "--" in body and len(body) <= REWRITE_MAX_CHARS:
                return s["start"], s["end"]
    words = {w.lower() for w in re.findall(r"[A-Za-z_][\w\-]{2,}", instructions)}
    words -= {w.lower() for w in re.findall(r"[\w.\-]+", filename)}
    scored = []
    for s in sections:
        body = "\n".join(lines[s["start"] - 1 : s["end"]]).lower()
        scored.append((sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", body)), s))
    best = max(score for score, _ in scored)
    winners = [s for score, s in scored if score == best]
    section = winners[0] if best > 0 and len(winners) == 1 else pick_section(
        instructions, filename, sections, backend, model
    )
    return section["start"], section["end"]


def default_commit_message(summaries: list[str], cwd: str) -> str:
    """A descriptive message when the request didn't give one: what this
    /task run did, or else which files changed."""
    if summaries:
        first = summaries[0][0].upper() + summaries[0][1:]
        return first if len(summaries) == 1 else f"{first} (+{len(summaries) - 1} more changes)"
    changed = [line[3:].strip().strip('"') for line in git_status_porcelain(cwd).splitlines() if line.strip()]
    names = ", ".join(os.path.basename(c.split(" -> ")[-1]) for c in changed[:3])
    more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
    return f"Update {names}{more}" if names else "Update files"


def pick_section(task: str, filename: str, sections: list[dict], backend: "Backend", model: str) -> dict:
    """Ask a cloud model which (deterministically-split) section to edit."""
    if len(sections) == 1:
        return sections[0]
    # Numbered from 1: small models answer "1" for "the first one" far more
    # often than "0", which with 0-based numbering silently picked the
    # wrong section (or, for a one-section file, none at all).
    listing = "\n".join(
        f"{i}: {s['name']} (lines {s['start']}-{s['end']})" for i, s in enumerate(sections, 1)
    )
    prompt = (
        f"Task: {task}\n\n{filename} has these sections:\n{listing}\n\n"
        f"Which section number is most relevant to this task? "
        f"Reply with just the number, nothing else."
    )
    reply, _model_used = backend.classify_with_fallback(prompt, model)
    reply = reply.strip()
    match = re.search(r"\d+", reply)
    index = int(match.group()) - 1 if match else -1
    if not (0 <= index < len(sections)):
        raise EditError(f"couldn't tell which section to edit from the model's reply: {reply!r}")
    return sections[index]


ASK_MAX_SUBQUESTIONS = 8


def decompose_question(question: str, backend: "Backend", model: str, context: str = "") -> dict:
    """Ask the "planning" role to either answer directly, or split into
    sub-questions for the "building" role to research first.

    Returns {"answer": str} if it answered directly, or
    {"subquestions": [str, ...]} if it decomposed. If the reply doesn't
    match either expected shape, it's treated as a direct answer rather
    than raising -- a plain but usable reply beats a hard failure here.
    """
    prompt = (
        (f"The project's files (the question is about these):\n\n{context}\n\n" if context else "")
        + f"Question: {question}\n\n"
        "If this is simple enough to answer directly and completely, reply "
        "with exactly:\nANSWER: <your answer>\n\n"
        "If it would be answered better by researching a few simpler "
        "sub-questions first, reply with exactly:\nSUBQUESTIONS:\n"
        "1. <sub-question>\n2. <sub-question>\n"
        f"(as many as needed, no more than {ASK_MAX_SUBQUESTIONS})"
    )
    reply, model_used = backend.classify_with_fallback(prompt, model)
    reply = reply.strip()
    upper = reply.upper()

    if upper.startswith("ANSWER:"):
        return {"answer": reply.split(":", 1)[1].strip(), "model_used": model_used}

    if upper.startswith("SUBQUESTIONS:"):
        # Numbering was requested but isn't always honored -- treat every
        # non-empty line as one sub-question, stripping a leading
        # "1.", "2)", "-", or "*" if present rather than requiring one.
        subquestions = []
        for line in reply.split(":", 1)[1].splitlines():
            line = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
            if line:
                subquestions.append(line)
        if subquestions:
            return {"subquestions": subquestions[:ASK_MAX_SUBQUESTIONS], "model_used": model_used}

    return {"answer": reply, "model_used": model_used}


def synthesize_answer(
    question: str, subanswers: list[tuple[str, str]], backend: "Backend", model: str, context: str = ""
) -> str:
    """Ask the "planning" role to combine sub-answers into one final answer."""
    research = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in subanswers)
    prompt = (
        (f"The project's files:\n\n{context}\n\n" if context else "")
        + f"Original question: {question}\n\nResearch:\n{research}\n\n"
        "Using this research, write one clear, complete final answer to the "
        "original question."
    )
    text, _model_used = backend.classify_with_fallback(prompt, model)
    return text.strip()


def _gradient(text: str, start: str, end: str) -> Text:
    """Render `text` with a per-character color ramp from `start` to `end`."""
    sr, sg, sb = (int(start[i : i + 2], 16) for i in (1, 3, 5))
    er, eg, eb = (int(end[i : i + 2], 16) for i in (1, 3, 5))
    out = Text()
    span = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        t = i / span
        r = round(sr + (er - sr) * t)
        g = round(sg + (eg - sg) * t)
        b = round(sb + (eb - sb) * t)
        out.append(ch, style=f"bold #{r:02x}{g:02x}{b:02x}")
    return out


class GenerationCancelled(Exception):
    pass


class ICloudPlusRequired(RuntimeError):
    """Raised when a Shortcuts-backed cloud tier fails specifically because
    the signed-in account lacks iCloud+ -- Apple's "Use Model" action's own
    error text for this ("...you must be signed in to an iCloud+ account
    to use the <tier> model") is specific enough to match on reliably.
    """
    pass


class CloudTierUnavailable(RuntimeError):
    """Raised for any OTHER Shortcuts-backed cloud tier failure -- usage
    limit reached, network trouble, a missing/broken shortcut, etc. Unlike
    ICloudPlusRequired (a permanent account fact, persisted to disk), this
    is treated as transient and only tracked for the current session: a
    usage limit resets on its own, so remembering it forever would be
    actively wrong, not just unhelpful.
    """
    pass


_op_lock = threading.Lock()
_active_op: subprocess.Popen | http.client.HTTPConnection | None = None
_cancel_requested = False


def _begin_op(op: subprocess.Popen | http.client.HTTPConnection) -> None:
    global _active_op, _cancel_requested
    with _op_lock:
        _active_op = op
        _cancel_requested = False


def _end_op(op: subprocess.Popen | http.client.HTTPConnection) -> bool:
    """Unregister `op`. Returns True if it had been cancelled."""
    global _active_op
    with _op_lock:
        was_cancelled = _cancel_requested and _active_op is op
        if _active_op is op:
            _active_op = None
    return was_cancelled


def cancel_active_process() -> bool:
    """Interrupt whatever fm/shortcuts subprocess or Ollama request is running.

    Only one of these runs at a time in this app (a single background
    worker per turn), so a single module-level slot is enough. A subprocess
    gets terminated; an HTTP connection gets closed out from under its
    blocked read, which raises in the thread that's waiting on it.
    """
    global _cancel_requested
    with _op_lock:
        op = _active_op
        if op is None:
            return False
        _cancel_requested = True
    if isinstance(op, subprocess.Popen):
        if op.poll() is None:
            op.terminate()
    else:
        try:
            op.close()
        except OSError:
            pass
    return True


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """subprocess.run-alike whose Popen is registered so Esc-Esc can kill it.
    With `timeout`, a run that takes longer is killed and reported as a
    failure (returncode -9) rather than hanging -- seen for real: an
    on-device generation that ran on for over two minutes."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _begin_op(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            return subprocess.CompletedProcess(cmd, -9, stdout, f"timed out after {int(timeout)}s")
    finally:
        cancelled = _end_op(proc)
    if cancelled:
        raise GenerationCancelled()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


OLLAMA_HOST_DEFAULT = "http://localhost:11434"
# FM_PCC_HOME relocates saved sessions and state -- tests/run.sh points it
# at a temp dir so tests never read or write the real ~/.fm-pcc.
FM_PCC_HOME = os.environ.get("FM_PCC_HOME") or os.path.expanduser("~/.fm-pcc")
SESSIONS_DIR = os.path.join(FM_PCC_HOME, "sessions")
STATE_PATH = os.path.join(FM_PCC_HOME, "state.json")
NOTIFY_MIN_SECONDS = 5.0
ON_DEVICE_CONTEXT_TOKENS = 4096  # documented limit for the on-device system model
CLOUD_CONTEXT_TOKENS_ESTIMATE = 32000  # no published figure for cloud/cloud-pro -- a guess
UPDATE_CHECK_URL = "https://api.github.com/repos/justwaters/fm-pcc/releases/latest"
# The plain web page for the latest release redirects to
# .../releases/tag/v<version>. Unlike the API, it isn't limited to 60
# unauthenticated requests an hour per IP -- which /update hit for real
# ("HTTP Error 403: rate limit exceeded") on a network shared with other
# GitHub tooling -- so it's tried first.
UPDATE_CHECK_WEB_URL = "https://github.com/justwaters/fm-pcc/releases/latest"


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _latest_version_from_web(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": f"fm-pcc/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
    match = re.search(r"/releases/tag/v?([^/?#]+)$", final or "")
    if not match:
        raise ValueError(f"latest-release page didn't redirect to a tag ({final})")
    return urllib.parse.unquote(match.group(1))


def _latest_version_from_api(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    tag = data.get("tag_name") or ""
    version = tag[1:] if tag.startswith("v") else tag
    if not version:
        raise ValueError(f"unexpected response: {data!r}"[:200])
    return version


def on_device_problem() -> str | None:
    """None if the on-device model is ready to use; otherwise what's wrong
    and how to fix it. Agreeing to Apple's terms is the user's decision --
    fm-pcc never does it for them, it only points at /license."""
    try:
        status = subprocess.run(["fm", "license", "--status"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return ("the on-device model isn't available: Apple's `fm` command wasn't found. "
                "fm-pcc needs macOS 27 with Apple Intelligence.")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"couldn't check the on-device model: {e}"
    text = (status.stdout + status.stderr).strip()
    if status.returncode != 0 or not re.search(r"\bagreed\b", text, re.IGNORECASE) or re.search(
        r"\bnot\s+(?:yet\s+)?agreed\b|\bhas\s+not\b", text, re.IGNORECASE
    ):
        return ("one-time setup: the on-device model needs you to read and agree to Apple's "
                "Foundation Models terms first. Type /license to do that now.")
    try:
        available = subprocess.run(
            ["fm", "available", "--model", "system"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"couldn't check the on-device model: {e}"
    if available.returncode != 0:
        reason = (available.stdout + available.stderr).strip() or "unavailable"
        return (f"the on-device model isn't available yet: {reason}. Make sure Apple Intelligence "
                f"is turned on in System Settings (and has finished downloading its model).")
    return None


def fetch_latest_version(
    url: str = UPDATE_CHECK_URL, timeout: float = 4.0, web_url: str = UPDATE_CHECK_WEB_URL
) -> tuple[str | None, str | None]:
    """Find the latest GitHub release's version: first from where the web
    "latest release" page redirects (no rate limit), then from the API.
    Returns (version, None) on success or (None, error) if both fail --
    never raises, so the startup check can just skip showing a button on
    failure, while /update can surface the actual errors.
    """
    errors = []
    for source, fetch in (("github.com", lambda: _latest_version_from_web(web_url, timeout)),
                          ("GitHub API", lambda: _latest_version_from_api(url, timeout))):
        try:
            return fetch(), None
        except Exception as e:
            errors.append(f"{source}: {e}")
    return None, "; ".join(errors)


def _git_repo_name(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return os.path.basename(result.stdout.strip().rstrip("/"))


def git_branch(cwd: str) -> str | None:
    """Current branch name, or None if `cwd` isn't a git repo or HEAD is
    detached (an empty branch name isn't worth showing in the statusline).
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _readme_first_line(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    return stripped
    except OSError:
        return None
    return None


def launch_context_hint(cwd: str) -> str | None:
    """Best-effort one-line orientation shown on startup: the git repo name
    and a file count if `cwd` is inside a repo, plus the README's first
    non-empty line if one exists. Never raises; returns None if there's
    nothing worth showing (not a repo, no README).
    """
    parts = []
    repo_name = _git_repo_name(cwd)
    if repo_name:
        try:
            file_count = sum(
                1 for f in os.listdir(cwd)
                if os.path.isfile(os.path.join(cwd, f)) and not f.startswith(".")
            )
            parts.append(f"git repo '{repo_name}' ({file_count} files here)")
        except OSError:
            parts.append(f"git repo '{repo_name}'")

    readme_path = find_readme(cwd)
    if readme_path:
        first_line = _readme_first_line(readme_path)
        if first_line:
            parts.append(f"README: {first_line}")

    return " · ".join(parts) if parts else None


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification -- never raises, never blocks the
    caller on anything but the (short, timed-out) osascript call itself.
    """
    try:
        message = message.strip().replace("\n", " ")
        if len(message) > 200:
            message = message[:197] + "…"
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        subprocess.run(
            ["osascript", "-e", script], check=False, capture_output=True, timeout=5
        )
    except Exception:
        pass


def _ollama_connect(host: str) -> http.client.HTTPConnection:
    parsed = urllib.parse.urlparse(host)
    return http.client.HTTPConnection(parsed.hostname or "localhost", parsed.port or 11434, timeout=180)


def _ollama_list_models(host: str) -> list[str]:
    conn = _ollama_connect(host)
    try:
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError as e:
        raise RuntimeError(f"couldn't reach Ollama at {host} -- run 'ollama serve'") from e
    finally:
        conn.close()
    if status != 200:
        raise RuntimeError(f"Ollama returned HTTP {status}")
    return [m["name"] for m in json.loads(data).get("models", [])]


def _ollama_chat(model: str, messages: list[dict], host: str) -> tuple[str, int | None]:
    """POST to Ollama's /api/chat, cancellable the same way as _run().

    Uses http.client directly (stdlib, no new dependency) rather than
    `ollama run <model>` so multi-turn history can be passed natively as
    a messages list instead of re-stuffing prior turns into a text prompt
    the way the Shortcuts-backed cloud tiers have to.

    Returns (reply text, total tokens now in context) -- the second value
    is `prompt_eval_count + eval_count` from Ollama's own accounting (real
    tokenizer counts, not an estimate), or None if either is missing from
    the response.
    """
    conn = _ollama_connect(host)
    _begin_op(conn)
    try:
        body = json.dumps({"model": model, "messages": messages, "stream": False})
        conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError as e:
        cancelled = _end_op(conn)
        conn.close()
        if cancelled:
            raise GenerationCancelled()
        raise RuntimeError(f"couldn't reach Ollama at {host} -- run 'ollama serve'") from e

    cancelled = _end_op(conn)
    conn.close()
    if cancelled:
        raise GenerationCancelled()

    if status != 200:
        raise RuntimeError(f"Ollama returned HTTP {status}: {data.decode('utf-8', 'replace')[:200]}")
    payload = json.loads(data)
    if "error" in payload:
        raise RuntimeError(f"Ollama error: {payload['error']}")

    prompt_tokens = payload.get("prompt_eval_count")
    eval_tokens = payload.get("eval_count")
    total_tokens = (
        prompt_tokens + eval_tokens
        if prompt_tokens is not None and eval_tokens is not None
        else None
    )
    return payload["message"]["content"], total_tokens


def _ollama_context_length(model: str, host: str) -> int | None:
    """Look up `model`'s context window from /api/show. The key is always
    "<architecture>.context_length" (e.g. "llama.context_length",
    "qwen2.context_length") -- the architecture prefix varies by model
    family, so this matches on the suffix generically rather than trying
    to enumerate every architecture name.
    """
    conn = _ollama_connect(host)
    try:
        body = json.dumps({"name": model})
        conn.request("POST", "/api/show", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
    except OSError:
        return None
    finally:
        conn.close()
    if status != 200:
        return None
    try:
        info = json.loads(data).get("model_info", {})
    except json.JSONDecodeError:
        return None
    for key, value in info.items():
        if key.endswith("context_length") and isinstance(value, int):
            return value
    return None


def _installed_shortcuts() -> set[str]:
    result = subprocess.run(["shortcuts", "list"], capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def ensure_shortcut_installed(
    name: str,
    url: str,
    on_status: Callable[[str], None],
    timeout: float = 90.0,
) -> bool:
    """Make sure `name` is installed, prompting the Shortcuts add-flow if not."""
    if name in _installed_shortcuts():
        return True

    on_status(f"'{name}' shortcut not found — opening its install link…")
    subprocess.run(["open", url])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in _installed_shortcuts():
            on_status(f"'{name}' installed.")
            return True
        time.sleep(1.5)

    on_status(
        f"Still waiting on '{name}'. On-device chat works without it — "
        f"try again once you've added it."
    )
    return False


def load_icloud_plus_unavailable() -> set[str]:
    """Which cloud tiers are known to require iCloud+ this account doesn't
    have -- persisted so this survives restarts and updates, not just the
    current session (once seen, that error isn't going away until the
    account itself changes).
    """
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        return set(data.get("icloud_plus_unavailable", []))
    except (OSError, json.JSONDecodeError):
        return set()


def save_icloud_plus_unavailable(unavailable: set[str]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump({"icloud_plus_unavailable": sorted(unavailable)}, f, indent=2)
    except OSError:
        pass


FALLBACK_CHAIN = {"cloud-pro": "cloud", "cloud": "on-device"}


class Backend:
    """Talks to fm (on-device), Shortcuts (cloud, cloud pro), and Ollama."""

    def __init__(
        self,
        shortcut_overrides: dict[str, str] | None = None,
        on_status: Callable[[str], None] = print,
        ollama_model: str | None = None,
        ollama_host: str = OLLAMA_HOST_DEFAULT,
    ):
        self.on_status = on_status
        self._shortcut_overrides = shortcut_overrides or {}
        self._shortcut_ready: dict[str, bool] = {}
        self._transcript_path: str | None = None
        self._cloud_history: dict[str, list[tuple[str, str]]] = {
            model: [] for model in CLOUD_SHORTCUTS
        }
        self.ollama_model = ollama_model
        self.ollama_host = ollama_host
        self._ollama_resolved_model: str | None = None
        self._ollama_history: dict[str, list[dict[str, str]]] = {}
        self._ollama_context_tokens: dict[str, int] = {}
        self._ollama_context_length: dict[str, int] = {}
        self._icloud_plus_unavailable: set[str] = load_icloud_plus_unavailable()
        # model -> short reason, NOT persisted (unlike iCloud+) since these
        # are transient -- a usage limit resets on its own, so remembering
        # one forever across restarts would be actively wrong.
        self._session_unavailable: dict[str, str] = {}

    def shortcut_name(self, model: str) -> str:
        return self._shortcut_overrides.get(model, CLOUD_SHORTCUTS[model]["name"])

    def reset(self) -> None:
        self._transcript_path = None
        self._ollama_context_tokens.clear()
        for history in self._cloud_history.values():
            history.clear()
        self._ollama_history.clear()

    def respond(self, prompt: str, model: str) -> tuple[str, str]:
        """Dispatch `prompt` to `model`, cascading cloud-pro -> cloud ->
        on-device on failure (any reason: iCloud+, usage limit, network,
        a missing shortcut) instead of surfacing a raw error the moment a
        cloud tier turns out to be unusable. Returns (answer, model
        actually used) since that can differ from `model` when a fallback
        happens -- the caller decides whether/how to reflect that.

        A tier already known to be unavailable (iCloud+-restricted this
        session or persisted, or flagged unavailable earlier this
        session) is skipped without even attempting it.
        """
        current = model
        while True:
            if current in self._icloud_plus_unavailable or current in self._session_unavailable:
                fallback = FALLBACK_CHAIN.get(current)
                if fallback is None:
                    reason = self._session_unavailable.get(current, "requires iCloud+")
                    raise RuntimeError(f"{model_label(current)} is unavailable ({reason})")
                current = fallback
                continue
            try:
                if current == "on-device":
                    return self._respond_on_device(prompt), current
                if current == "ollama" or current.startswith("ollama:"):
                    return self._respond_ollama(prompt, current), current
                return self._respond_cloud(prompt, current), current
            except (ICloudPlusRequired, CloudTierUnavailable):
                fallback = FALLBACK_CHAIN.get(current)
                if fallback is None:
                    raise
                current = fallback

    def _resolve_ollama_model(self, model: str = "ollama") -> str:
        """`model` is either the generic "ollama" (auto-resolve) or a
        specific "ollama:<tag>" picked directly, e.g. from the /model menu.
        """
        if model.startswith("ollama:"):
            return model.split(":", 1)[1]
        if self.ollama_model:
            return self.ollama_model
        if self._ollama_resolved_model:
            return self._ollama_resolved_model
        models = _ollama_list_models(self.ollama_host)
        if not models:
            raise RuntimeError(
                "no Ollama models installed -- run e.g. 'ollama pull llama3.2'"
            )
        self._ollama_resolved_model = models[0]
        return self._ollama_resolved_model

    def _respond_ollama(self, prompt: str, model: str) -> str:
        tag = self._resolve_ollama_model(model)
        history = self._ollama_history.setdefault(tag, [])
        history.append({"role": "user", "content": prompt})
        try:
            text, total_tokens = _ollama_chat(tag, history, self.ollama_host)
        except Exception:
            history.pop()  # don't keep a failed turn in context
            raise
        history.append({"role": "assistant", "content": text})
        if total_tokens is not None:
            self._ollama_context_tokens[tag] = total_tokens
        return text

    def _respond_on_device(self, prompt: str) -> str:
        if self._transcript_path is None:
            self._transcript_path = os.path.join(
                tempfile.gettempdir(), f"fm-pcc-{uuid.uuid4().hex}.json"
            )

        args = ["fm", "respond", "--no-stream"]
        if os.path.exists(self._transcript_path):
            args += ["--resume", self._transcript_path]
        args += ["--save-transcript", self._transcript_path, prompt]

        result = _run(args)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "fm respond failed")
        return result.stdout.strip()

    def _ensure_ready(self, model: str) -> None:
        if not self._shortcut_ready.get(model):
            self._shortcut_ready[model] = ensure_shortcut_installed(
                self.shortcut_name(model), CLOUD_SHORTCUTS[model]["url"], self.on_status
            )

    def _run_shortcut(self, model: str, prompt: str) -> str:
        """Raw one-shot Shortcuts call -- no chat history involved."""
        shortcut = self.shortcut_name(model)
        self._ensure_ready(model)

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input.txt")
            out_path = os.path.join(tmp, "output.rtf")
            with open(in_path, "w") as f:
                f.write(prompt)

            result = _run(["shortcuts", "run", shortcut, "-i", in_path, "-o", out_path])
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                if "icloud+" in detail.lower():
                    self._icloud_plus_unavailable.add(model)
                    save_icloud_plus_unavailable(self._icloud_plus_unavailable)
                    raise ICloudPlusRequired(detail)
                reason = "usage limit reached" if "usage limit" in detail.lower() else "unavailable this session"
                self._session_unavailable[model] = reason
                raise CloudTierUnavailable(
                    f"Shortcut '{shortcut}' failed: {detail}\n"
                    f"Check it exists ('shortcuts list') and its 'Use Model' "
                    f"action is bound to Shortcut Input."
                )
            if not os.path.exists(out_path):
                self._session_unavailable[model] = "unavailable this session"
                raise CloudTierUnavailable(f"Shortcut '{shortcut}' produced no output.")

            return subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", out_path],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

    def classify(self, prompt: str, model: str) -> str:
        """One-shot, history-free call for routing/classification, not chat."""
        if model == "on-device":
            result = _run(["fm", "respond", "--no-stream", "--greedy", prompt])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "fm respond failed")
            return result.stdout.strip()
        if model == "ollama" or model.startswith("ollama:"):
            text, _ = _ollama_chat(
                self._resolve_ollama_model(model),
                [{"role": "user", "content": prompt}],
                self.ollama_host,
            )
            return text
        return self._run_shortcut(model, prompt)

    def classify_with_fallback(self, prompt: str, model: str) -> tuple[str, str]:
        """Like classify(), but cascades cloud-pro -> cloud -> on-device on
        a cloud-tier failure, same as respond(). Used by /task's and
        /ask's planning role so an exhausted Cloud Pro quota doesn't just
        break them outright. /compare deliberately does NOT use this --
        it wants each tier's own real answer or a clear skip, never a
        different tier's answer silently mislabeled as this one's.
        """
        current = model
        while True:
            if current in self._icloud_plus_unavailable or current in self._session_unavailable:
                fallback = FALLBACK_CHAIN.get(current)
                if fallback is None:
                    reason = self._session_unavailable.get(current, "requires iCloud+")
                    raise RuntimeError(f"{model_label(current)} is unavailable ({reason})")
                current = fallback
                continue
            try:
                return self.classify(prompt, current), current
            except (ICloudPlusRequired, CloudTierUnavailable):
                fallback = FALLBACK_CHAIN.get(current)
                if fallback is None:
                    raise
                current = fallback

    def _respond_cloud(self, prompt: str, model: str) -> str:
        history = self._cloud_history[model]
        full_prompt = prompt
        if history:
            context = "\n\n".join(f"User: {u}\nAssistant: {a}" for u, a in history)
            full_prompt = f"{context}\n\nUser: {prompt}"

        text = self._run_shortcut(model, full_prompt)
        history.append((prompt, text))
        return text

    def context_usage(self, model: str) -> tuple[int | None, int | None]:
        """Return (tokens used, context window size) for `model`'s current
        session, or (None, None)/(None, max) when a piece is unknown.

        - on-device: exact, via `fm count-tokens --transcript` against the
          documented 4,096-token session limit.
        - ollama: exact, from the real token counts Ollama returns with
          each chat response, against that model's own context length
          (from /api/show) -- both real numbers, not estimates.
        - cloud/cloud-pro: no API exposes either number for Apple's
          PCC-backed tiers, so this is a chars/4 estimate against a
          guessed context size, clearly a rough approximation.
        """
        family = model_family(model)

        if family == "on-device":
            if not self._transcript_path or not os.path.isfile(self._transcript_path):
                return 0, ON_DEVICE_CONTEXT_TOKENS
            try:
                result = subprocess.run(
                    ["fm", "count-tokens", "--quiet", "--transcript", self._transcript_path],
                    capture_output=True, text=True, timeout=15,
                )
                used = int(result.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return None, ON_DEVICE_CONTEXT_TOKENS
            return used, ON_DEVICE_CONTEXT_TOKENS

        if family == "ollama":
            try:
                tag = self._resolve_ollama_model(model)
            except Exception:
                return None, None
            used = self._ollama_context_tokens.get(tag)
            max_tokens = self._ollama_context_length.get(tag)
            if max_tokens is None:
                max_tokens = _ollama_context_length(tag, self.ollama_host)
                if max_tokens is not None:
                    self._ollama_context_length[tag] = max_tokens
            return used, max_tokens

        # cloud / cloud-pro
        history = self._cloud_history.get(model, [])
        chars = sum(len(u) + len(a) for u, a in history)
        return chars // 4, CLOUD_CONTEXT_TOKENS_ESTIMATE


_EXPORT_STATUS_PREFIXES = (
    "exported ", "copied the transcript", "nothing to export", "couldn't export", "couldn't copy",
)


def render_transcript(messages: list["Message"], fmt: str, header: dict) -> str:
    """The transcript as Markdown ("md"), plain text ("txt"), or JSON."""
    if fmt == "json":
        return json.dumps(
            {**header, "messages": [{"role": m.role, "text": m.text} for m in messages]}, indent=2
        ) + "\n"
    if fmt == "txt":
        out = [f"fm-pcc transcript -- {header['exported']} -- {header['directory']}", ""]
        for m in messages:
            prefix = {"user": "you › ", "assistant": "fm-pcc › "}.get(m.role, "")
            out += [prefix + m.text, ""]
        return "\n".join(out)

    out = [
        "# fm-pcc transcript",
        "",
        f"Exported {header['exported']} from `{header['directory']}` "
        f"(fm-pcc v{header['version']}, model: {header['model']})",
        "",
    ]
    for m in messages:
        if m.role == "user":
            out += ["---", "", f"**you ›** {m.text}", ""]
        elif m.role == "assistant":
            out += ["**fm-pcc ›**", "", m.text, ""]
        elif "\n" in m.text:
            # /task plans, file diffs, errors: keep them verbatim.
            lang = "diff" if re.search(r"^(?:---|\+\+\+|@@)", m.text, re.MULTILINE) else "text"
            fence = "````" if "```" in m.text else "```"
            out += [f"{fence}{lang}", m.text, fence, ""]
        else:
            out += [f"> {m.text}", ""]
    return "\n".join(out).rstrip() + "\n"


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system" | "thinking"
    text: str


class MessageWidget(Static):
    """A single turn, rendered as flowing text (no bubble/border chrome)."""

    def __init__(self, message: Message, accent: str):
        if message.role == "user":
            body = Text.from_markup(f"[#7b838a]you ›[/] {rich_escape(message.text)}")
        elif message.role == "assistant":
            prefix = Text("fm-pcc ›", style=f"bold {accent}")
            # Cloud/Ollama replies routinely come back as markdown (headers,
            # lists, bold); plain-text replies render through Markdown
            # unchanged, so this is a safe default for either.
            body = Group(prefix, Markdown(message.text))
        elif message.role == "thinking":
            body = Text.from_markup(f"[{accent}]· thinking…[/]")
        else:
            body = Text.from_markup(f"[#7b838a italic]{rich_escape(message.text)}[/]")
        super().__init__(body)
        self.add_class(f"msg-{message.role}")


class ChatApp(App):
    CSS = """
    Screen { background: #14181c; }
    #banner { padding: 1 2 0 2; }
    #subtitle { padding: 0 2 1 2; color: #7b838a; }
    #log { padding: 0 2; }
    /* Sent messages sit on a gray band, like Claude Code's CLI, so your
       side of the conversation is easy to pick out when scrolling back. */
    .msg-user { margin: 1 0 0 0; padding: 0 1; background: #2a3138; }
    .msg-assistant { margin: 1 0 0 0; }
    .msg-system { margin: 1 0 0 0; }
    .msg-thinking { margin: 1 0 0 0; }
    #palette {
        display: none;
        height: auto;
        max-height: 8;
        margin: 0 2 0 2;
        border: round #7b838a;
        background: #1b2126;
    }
    #palette > .option-list--option-highlighted { background: #2a3138; }
    #inputbar { height: 3; border: round #7b838a; margin: 0 2 0 2; padding: 0 1; }
    #prompt-glyph { width: 2; content-align: center middle; }
    #input { border: none; background: transparent; }
    #input:focus { border: none; }
    #statusbar { height: auto; margin: 0 2 1 2; }
    #status { width: 1fr; padding: 0 1; content-align: left middle; }
    #update-button {
        display: none;
        min-width: 0;
        height: 1;
        border: none;
        padding: 0 1;
        background: #1b2126;
        color: #ffd43b;
        text-style: bold;
    }
    #update-button:hover { background: #2a3138; }
    """

    BINDINGS = [
        ("ctrl+t", "toggle_model", "Toggle model"),
        ("ctrl+r", "reset", "Reset chat"),
        # priority=True so this fires even though the focused Input has its
        # own ctrl+c binding (copy) that would otherwise intercept it first.
        Binding("ctrl+c", "attempt_quit", "Quit", priority=True),
    ]

    model: reactive[str] = reactive("on-device")

    def __init__(
        self,
        initial_model: str = "on-device",
        shortcut_overrides: dict[str, str] | None = None,
        ollama_model: str | None = None,
        ollama_host: str = OLLAMA_HOST_DEFAULT,
    ):
        super().__init__()
        self.backend = Backend(
            shortcut_overrides,
            on_status=lambda msg: self.call_from_thread(
                self._add_message, Message("system", msg)
            ),
            ollama_model=ollama_model,
            ollama_host=ollama_host,
        )
        self.set_reactive(ChatApp.model, initial_model)
        self.turn = 0
        self._thinking: MessageWidget | None = None
        self._pending_edit: dict | None = None
        self._model_picker_active = False
        self._history: list[str] = []
        # The last plain-chat message that read like a change request, so
        # "yes, do that" right after can run it as /task.
        self._pending_action_request: str | None = None
        # /verify off turns off running the project's checks after /task.
        self._verify_enabled = True
        self._history_index: int | None = None
        self._history_draft = ""
        self._suppress_palette_once = False
        self._last_escape_time = 0.0
        self._last_quit_time = 0.0
        self._loop_running = False
        self._loop_cancel_requested = False
        # On-device by default for both roles: fm-pcc works fully offline
        # from the first launch, with no Shortcuts to install and no cloud
        # quota. /subagents opts planning into a cloud tier.
        self.subagent_roles: dict[str, str] = {"planning": "on-device", "building": "on-device"}
        self._message_log: list[Message] = []
        # Everything shown on screen (user, assistant, and system lines like
        # /task plans and diffs) for /export -- _message_log is just the
        # conversation /save needs to restore.
        self._transcript: list[Message] = []
        self._undo_stack: list[dict] = []
        self._branch = git_branch(os.getcwd())
        self._previous_model: str | None = None
        self._latest_version: str | None = None
        self._last_task_description: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static(id="subtitle")
        yield VerticalScroll(id="log")
        yield OptionList(id="palette")
        with Horizontal(id="inputbar"):
            yield Static("❯", id="prompt-glyph")
            yield Input(placeholder="Message fm-pcc… (/ for commands)", id="input")
        with Horizontal(id="statusbar"):
            yield Static(id="status")
            yield Button("", id="update-button")

    def on_mount(self) -> None:
        self.query_one("#banner", Static).update(
            _gradient(
                f" fm-pcc v{__version__} — Apple Foundation Models Chat",
                "#f0b429", "#38d9c9",
            )
        )
        self.query_one(Input).focus()
        self._update_chrome()
        hint = launch_context_hint(os.getcwd())
        if hint:
            self._add_message(Message("system", hint))
        self._check_on_device_ready()
        self._check_for_update()

    @work(thread=True)
    def _check_on_device_ready(self) -> None:
        """Everything fm-pcc does runs on the on-device model by default, so
        say right away -- not at the first failed request -- if it can't
        be used yet, and exactly what to do about it."""
        problem = on_device_problem()
        if problem:
            self.call_from_thread(self._log_progress, problem)

    @work(thread=True)
    def _check_for_update(self) -> None:
        latest, _error = fetch_latest_version()
        if latest and is_newer(latest, __version__):
            self.call_from_thread(self._show_update_button, latest)

    def _show_update_button(self, latest: str) -> None:
        self._latest_version = latest
        button = self.query_one("#update-button", Button)
        button.label = f"Update (v{__version__} -> v{latest})"
        button.display = True

    @work(thread=True)
    def _run_manual_update_check(self) -> None:
        latest, error = fetch_latest_version()
        self.call_from_thread(self._manual_update_check_finished, latest, error)

    def _manual_update_check_finished(self, latest: str | None, error: str | None) -> None:
        self._enable_input()
        if error:
            self._add_message(Message("system", f"update check failed: {error}"))
        elif latest and is_newer(latest, __version__):
            self._show_update_button(latest)
            self._add_message(Message("system", f"update available: v{__version__} -> v{latest}"))
        else:
            self._add_message(Message("system", f"up to date (v{__version__})"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "update-button":
            self._run_update()

    @work(thread=True)
    def _run_update(self) -> None:
        self.call_from_thread(self._update_started)
        try:
            result = subprocess.run(
                ["uv", "tool", "upgrade", "fm-pcc"],
                capture_output=True, text=True, timeout=180,
            )
            success = result.returncode == 0
            detail = (result.stderr or result.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            success = False
            detail = str(e)
        self.call_from_thread(self._update_finished, success, detail)

    def _update_started(self) -> None:
        button = self.query_one("#update-button", Button)
        button.disabled = True
        button.label = "Updating…"
        self._add_message(Message("system", "updating fm-pcc…"))

    def _update_finished(self, success: bool, detail: str) -> None:
        button = self.query_one("#update-button", Button)
        if success:
            button.label = f"Updated to v{self._latest_version} -- restart fm-pcc"
            self._add_message(
                Message("system", f"updated to v{self._latest_version} — restart fm-pcc to use it")
            )
        else:
            button.disabled = False
            button.label = f"Update (v{__version__} -> v{self._latest_version})"
            self._add_message(Message("system", f"update failed: {detail[:300]}"))

    def watch_model(self, old_value: str, _new_value: str) -> None:
        self._previous_model = old_value
        self._update_chrome()

    NORMAL_INPUT_COLOR = "#e7e5dd"

    def _status_line(self) -> str:
        directory = os.path.basename(os.getcwd()) or os.getcwd()
        location = f"{directory} ({self._branch})" if self._branch else directory

        used, max_tokens = self.backend.context_usage(self.model)
        if used is None or not max_tokens:
            context = "Context:n/a"
        else:
            context = f"Context:{min(100, round(100 * used / max_tokens))}%"

        return f"{location} | {model_label(self.model)} | {context}"

    def _update_chrome(self) -> None:
        accent = model_color(self.model)
        self.query_one("#subtitle", Static).update(
            f" model: {model_label(self.model)} · /help for help"
        )
        status = self.query_one("#status", Static)
        status.update(self._status_line())
        status.styles.color = status_accent(self.model)
        self.query_one("#inputbar").styles.border = ("round", accent)
        self.query_one("#prompt-glyph", Static).update(Text("❯", style=f"bold {accent}"))
        self.query_one(Input).styles.color = self.NORMAL_INPUT_COLOR

    def action_toggle_model(self) -> None:
        next_index = (MODEL_ORDER.index(model_family(self.model)) + 1) % len(MODEL_ORDER)
        self._select_model(MODEL_ORDER[next_index])

    def _ollama_available(self) -> bool:
        """Live check -- covers both "ollama serve isn't running" and "it's
        running but nothing is pulled," since either way there's nothing to
        actually talk to.
        """
        try:
            return bool(_ollama_list_models(self.backend.ollama_host))
        except Exception:
            return False

    def _select_model(self, model: str) -> None:
        if model == self.model:
            return
        if model in self.backend._icloud_plus_unavailable:
            self._add_message(
                Message(
                    "system",
                    f"{model_label(model)} requires iCloud+ on this account — not "
                    f"switching (run /model reset if that's changed)",
                )
            )
            return
        if model in self.backend._session_unavailable:
            self._add_message(
                Message(
                    "system",
                    f"{model_label(model)} {self.backend._session_unavailable[model]} — "
                    f"not switching (this clears automatically on restart, or once it's "
                    f"reachable again)",
                )
            )
            return
        if model_family(model) == "ollama" and not self._ollama_available():
            self._add_message(
                Message(
                    "system",
                    f"{model_label(model)} isn't reachable — Ollama doesn't seem "
                    f"to be running, or has no models installed",
                )
            )
            return
        self.model = model
        self._add_message(Message("system", f"switched to {model_label(self.model)}"))

    def _reset_icloud_plus_unavailable(self) -> None:
        cleared = sorted(self.backend._icloud_plus_unavailable | set(self.backend._session_unavailable))
        if not cleared:
            self._add_message(Message("system", "no models are currently marked as unavailable"))
            return
        self.backend._icloud_plus_unavailable.clear()
        save_icloud_plus_unavailable(self.backend._icloud_plus_unavailable)
        self.backend._session_unavailable.clear()
        labels = ", ".join(model_label(m) for m in cleared)
        self._add_message(
            Message("system", f"cleared the unavailable status for: {labels} — they'll be retried")
        )

    def _open_model_picker(self) -> None:
        palette = self.query_one("#palette", OptionList)
        palette.clear_options()

        try:
            ollama_models = _ollama_list_models(self.backend.ollama_host)
        except Exception:
            ollama_models = []

        # `None` marks a non-selectable group header -- only "ollama" gets
        # one, to show its locally installed models as a tree underneath.
        entries: list[str | None] = []
        for key in MODEL_ORDER:
            if key == "ollama" and ollama_models:
                entries.append(None)
                entries.extend(f"ollama:{tag}" for tag in ollama_models)
            else:
                entries.append(key)

        for i, entry in enumerate(entries):
            if entry is None:
                palette.add_option(Option("ollama", id="_header_ollama", disabled=True))
                continue
            marker = "● " if entry == self.model else "○ "
            if entry in self.backend._icloud_plus_unavailable:
                unavailable, note = True, "Requires iCloud+"
            elif entry in self.backend._session_unavailable:
                unavailable = True
                note = self.backend._session_unavailable[entry].capitalize()
            elif entry == "ollama" and not ollama_models:
                unavailable, note = True, "Not running"
            else:
                unavailable, note = False, None
            label = model_label(entry)
            if note:
                label = f"{label} ({note})"
            if entry.startswith("ollama:"):
                branch = "└─" if i == len(entries) - 1 else "├─"
                palette.add_option(
                    Option(f"{branch} {marker}{label}", id=entry, disabled=unavailable)
                )
            else:
                palette.add_option(Option(f"{marker}{label}", id=entry, disabled=unavailable))

        selectable = [e for e in entries if e is not None]
        highlight_target = self.model if self.model in selectable else selectable[0]
        palette.highlighted = next(i for i, e in enumerate(entries) if e == highlight_target)
        palette.display = True
        palette.focus()
        self._model_picker_active = True
        self.query_one(Input).disabled = True

    def _close_model_picker(self) -> None:
        self._model_picker_active = False
        self.query_one("#palette", OptionList).display = False
        input_widget = self.query_one(Input)
        input_widget.disabled = False
        input_widget.focus()

    def action_reset(self) -> None:
        self.backend.reset()
        self.turn = 0
        self._message_log = []
        self._transcript = []
        self.query_one("#log", VerticalScroll).remove_children()
        self._update_chrome()
        self._add_message(Message("system", "conversation reset"))

    def action_attempt_quit(self) -> None:
        now = time.monotonic()
        if self._last_quit_time and now - self._last_quit_time < 1.5:
            cancel_active_process()  # don't hang waiting on a blocked worker thread
            self.exit()
        else:
            self._last_quit_time = now
            self.query_one("#status", Static).update("press ctrl+c again to quit…")
            self.set_timer(1.5, self._update_chrome)

    UNDO_STACK_MAX = 20

    def _cap_undo_stack(self) -> None:
        if len(self._undo_stack) > self.UNDO_STACK_MAX:
            self._undo_stack.pop(0)

    def _push_undo(self, path: str, label: str, original: str, existed_before: bool = True) -> None:
        self._undo_stack.append(
            {
                "kind": "write", "path": path, "label": label,
                "original": original, "existed_before": existed_before,
            }
        )
        self._cap_undo_stack()

    def _push_undo_folder(self, path: str, label: str) -> None:
        self._undo_stack.append({"kind": "folder", "path": path, "label": label})
        self._cap_undo_stack()

    def _push_undo_move(self, src_path: str, dest_path: str, src_label: str, dest_label: str) -> None:
        self._undo_stack.append(
            {
                "kind": "move", "src_path": src_path, "dest_path": dest_path,
                "src_label": src_label, "dest_label": dest_label,
            }
        )
        self._cap_undo_stack()

    def _add_message(self, message: Message) -> MessageWidget:
        log = self.query_one("#log", VerticalScroll)
        widget = MessageWidget(message, model_color(self.model))
        log.mount(widget)
        log.scroll_end(animate=False)
        if message.role in ("user", "assistant"):
            self._message_log.append(message)
        if message.role != "thinking":
            self._transcript.append(message)
        return widget

    COMMANDS = {
        "help": "show this help",
        "model": "open a menu to switch models, /model <name> directly, or /model reset to clear unavailable-model restrictions",
        "edit": "propose an edit: /edit <path> <instructions> (on-device only)",
        "task": "run a multi-step edit loop, writing as it goes: /task <description>",
        "ask": "research a question via cloud/core subagents: /ask <question>",
        "subagents": "show or set the planning/building roles: /subagents [planning|building] <model>",
        "compare": "ask every model the same question: /compare <question>",
        "save": "save the conversation: /save <name>",
        "export": "export the transcript: /export [file.md|file.txt|file.json|copy]",
        "run": "run a shell command in this directory and show its output: /run <command>",
        "license": "read and agree to Apple's on-device model terms (one-time setup)",
        "verify": "turn /task's automatic checks (tests, syntax) on or off: /verify [on|off]",
        "resume": "resume a saved conversation, or list saved ones: /resume [name]",
        "undo": "revert the last file write made by /edit or /task",
        "push": "commit and push the current changes to git",
        "pull": "pull the latest changes from git",
        "branch": "create or switch git branches: /branch create <name> | /branch switch <name>",
        "update": "check for a newer version of fm-pcc",
        "apply": "write the pending proposed edit",
        "discard": "discard the pending proposed edit",
        "clear": "start a new conversation",
        "quit": "exit fm-pcc",
    }
    COMMAND_ALIASES = {"?": "help", "reset": "clear", "exit": "quit", "q": "quit"}

    def _handle_command(self, raw: str) -> None:
        parts = raw[1:].strip().split(maxsplit=1)
        name = self.COMMAND_ALIASES.get(parts[0].lower(), parts[0].lower()) if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name == "help":
            commands = "\n".join(f"  /{cmd:<7} {desc}" for cmd, desc in self.COMMANDS.items())
            shortcuts = (
                "  ctrl+t          cycle on-device / cloud / cloud pro / ollama\n"
                "  ctrl+r          start a new conversation\n"
                "  enter           send your message\n"
                "  ↑ / ↓           step through what you've sent\n"
                "  esc esc         stop the current response/edit/task\n"
                "  ctrl+c ctrl+c   quit"
            )
            note = (
                "note: fm-pcc's chat is text-only, but the underlying `fm` CLI "
                "can also analyze images directly -- `fm respond --image "
                "photo.jpg --tool ocr \"...\"` for reliable text extraction, or "
                "`--tool barcode` to actually decode a barcode/QR code (without "
                "the tool, the model just guesses at unreadable pixel patterns "
                "like that). Not wired into fm-pcc's chat yet."
            )
            self._add_message(
                Message(
                    "system",
                    f"commands:\n{commands}\n\nshortcuts:\n{shortcuts}\n\n{note}",
                )
            )
        elif name == "model":
            if not arg:
                self._open_model_picker()
            elif arg == "reset":
                self._reset_icloud_plus_unavailable()
            elif arg in MODEL_LABELS or arg.startswith("ollama:"):
                self._select_model(arg)
            else:
                choices = ", ".join(MODEL_ORDER)
                self._add_message(
                    Message(
                        "system",
                        f"unknown model '{arg}' — try one of: {choices}, "
                        f"or ollama:<name> for a specific local model",
                    )
                )
        elif name == "ask":
            if not arg:
                self._add_message(Message("system", "usage: /ask <question>"))
                return
            self.query_one(Input).disabled = True
            self._run_ask(arg)
        elif name == "compare":
            if not arg:
                self._add_message(Message("system", "usage: /compare <question>"))
                return
            self.query_one(Input).disabled = True
            self._run_compare(arg)
        elif name == "save":
            self._handle_save(arg)
        elif name == "export":
            self._handle_export(arg)
        elif name == "license":
            self._handle_license()
        elif name == "run":
            if not arg:
                self._add_message(Message("system", "usage: /run <command>"))
                return
            self.query_one(Input).disabled = True
            self._run_shell(arg)
        elif name == "verify":
            if arg.strip().lower() in ("on", "off"):
                self._verify_enabled = arg.strip().lower() == "on"
            state = "on" if self._verify_enabled else "off"
            self._add_message(Message(
                "system",
                f"/task's automatic checks are {state}"
                + (" -- after changing code it runs the project's tests and syntax checks, and fixes failures"
                   if self._verify_enabled else " -- /task won't run tests or checks after changing code"),
            ))
        elif name == "resume":
            self._handle_resume(arg)
        elif name == "undo":
            self._handle_undo()
        elif name == "subagents":
            self._handle_subagents(arg)
        elif name == "edit":
            edit_parts = arg.split(maxsplit=1)
            if len(edit_parts) < 2:
                self._add_message(Message("system", "usage: /edit <path> <instructions>"))
                return
            path, instructions = edit_parts
            self.query_one(Input).disabled = True
            self._thinking = self._add_message(Message("thinking", ""))
            self._propose_edit(path, instructions)
        elif name == "task":
            if not arg:
                self._add_message(Message("system", "usage: /task <description>"))
                return
            self._last_task_description = arg
            self.query_one(Input).disabled = True
            self._run_task(arg)
        elif name == "push":
            self._handle_push()
        elif name == "pull":
            self._handle_pull()
        elif name == "branch":
            self._handle_branch(arg)
        elif name == "update":
            self.query_one(Input).disabled = True
            self._run_manual_update_check()
        elif name == "apply":
            self._apply_pending_edit()
        elif name == "discard":
            if self._pending_edit:
                self._pending_edit = None
                self._add_message(Message("system", "edit discarded"))
            else:
                self._add_message(Message("system", "no pending edit"))
        elif name == "clear":
            self.action_reset()
        elif name == "quit":
            self.exit()
        else:
            self._add_message(Message("system", f"unknown command '/{name}' — try /help"))

    @work(thread=True)
    def _propose_edit(self, path: str, instructions: str) -> None:
        try:
            cwd = os.getcwd()
            full = os.path.join(cwd, os.path.expanduser(path))
            line_range = None
            if os.path.isfile(full):  # else propose_edit reports "no such file"
                with open(full, "r", errors="replace") as f:
                    content = f.read()
                line_range = choose_edit_range(
                    content, path, instructions, self.backend, self.subagent_roles["planning"]
                )
            proposal = propose_edit(path, instructions, cwd, line_range=line_range)
        except GenerationCancelled:
            self.call_from_thread(self._finish_cancelled)
            return
        except EditError as e:
            self.call_from_thread(self._finish_turn, None, str(e))
            return
        except Exception as e:
            self.call_from_thread(self._finish_turn, None, f"edit failed: {e}")
            return
        diff = diff_preview(proposal["original"], proposal["updated"])
        self.call_from_thread(self._show_edit_proposal, proposal, diff)

    @work(thread=True)
    def _run_task(self, task: str) -> None:
        """Plan the whole task up front (plan_task), show the plan, then
        execute it step by step, writing as it goes -- no per-step /apply.
        Stops at the first step that needs the user's confirmation (push,
        pull, branch changes), at an error, or on Esc-Esc.
        """
        cwd = os.getcwd()
        self._loop_running = True
        self._loop_cancel_requested = False
        summaries: list[str] = []
        skipped: list[str] = []
        self._task_request = task
        self._task_changed: list[str] = []   # files changed in this run
        self._task_created: list[str] = []   # files created in this run
        self._task_originals: dict[str, str] = {}  # contents before this run first changed them
        self._task_baseline: set[str] | None = None  # tests failing before this run
        self._task_unverified = False        # code changed since the last check
        start_time = time.monotonic()
        try:
            files, folders = gather_task_tree(cwd)
            self.call_from_thread(self._log_progress, "planning…")
            steps, model_used = plan_task(
                task, files, folders, self.backend, self.subagent_roles["planning"], cwd=cwd
            )
            if model_used:
                self._note_planning_fallback(model_used)
            if not steps:
                self.call_from_thread(self._log_progress, "nothing to do -- task already satisfied.")
                return
            self.call_from_thread(
                self._log_progress,
                "plan:\n" + "\n".join(f"{i}. {describe_step(s)}" for i, s in enumerate(steps, 1)),
            )
            self._task_baseline = self._baseline_failures(cwd, task, steps)

            for number, s in enumerate(steps, 1):
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    return
                action = s["action"]
                if action == "UNSUPPORTED":
                    self.call_from_thread(
                        self._log_progress,
                        f"stopped at step {number}: /task can't {s['details']}"
                        + (f" -- steps 1-{number - 1} were done." if number > 1 else "."),
                    )
                    return
                if action in TASK_CONFIRM_GATED or action == "COMMIT":
                    # Nothing gets committed or handed off before the code
                    # changed so far has passed the project's own checks.
                    if not self._verify_changes(cwd):
                        return
                if action in TASK_CONFIRM_GATED:
                    suggestion = {
                        "PUSH": "/push",
                        "PULL": "/pull",
                        "BRANCH_CREATE": f"/branch create {s['path']}",
                        "BRANCH_SWITCH": f"/branch switch {s['path']}",
                        "RUN": f"/run {s['details']}",
                    }[action]
                    self.call_from_thread(
                        self._log_progress,
                        f"task wants to run {suggestion} -- that needs your explicit "
                        f"confirmation, so /task is stopping here. Run {suggestion} "
                        f"yourself, then re-run /task for anything after it.",
                    )
                    return
                self.call_from_thread(self._log_progress, f"step {number}: {describe_step(s)}")
                try:
                    summary = self._execute_task_step(s, cwd, summaries)
                except EditError as e:
                    if not s.get("optional"):
                        raise
                    # A file the planner chose on its own (the request
                    # named none) that turned out not to need this change.
                    skipped.append(s["path"])
                    reason = str(e) if len(str(e)) < 80 else "no suitable change found in it"
                    self.call_from_thread(self._log_progress, f"skipped {s['path']}: {reason}")
                    continue
                summaries.append(summary)
                self.call_from_thread(self._log_progress, summary)

            done = len(steps) - len(skipped)
            if not done:
                self.call_from_thread(
                    self._log_progress, "stopped: none of the planned changes could be made."
                )
                return
            if not self._verify_changes(cwd):
                return
            note = f" (skipped {', '.join(skipped)})" if skipped else ""
            self.call_from_thread(self._log_progress, f"done after {done} step(s){note}.")
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        except EditError as e:
            self.call_from_thread(self._log_progress, f"error: {e}")
        except Exception as e:
            self.call_from_thread(self._log_progress, f"error: task failed: {e}")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)
            if time.monotonic() - start_time >= NOTIFY_MIN_SECONDS:
                notify("fm-pcc", f"/task finished: {task}")

    def _execute_task_step(self, s: dict, cwd: str, summaries: list[str]) -> str:
        """Carry out one planned, non-gated step. Returns a one-line summary
        of what happened; raises EditError if it can't be done."""
        action, path, destination, details = s["action"], s["path"], s["destination"], s["details"]
        task = getattr(self, "_task_request", details)

        if action == "STAGE":
            git_add_all(cwd)
            return "staged all changes"

        if action == "FIX_CHECKS":
            self._task_unverified = True
            if not codework.detect_checks(cwd, [], task):
                raise EditError("couldn't find tests or other checks to run in this project")
            if not self._verify_changes(cwd):
                raise EditError("the checks still fail")
            return "the checks pass"

        if action == "TEST":
            checks = [c for c in codework.detect_checks(cwd, [], task) if c[0] == "tests"]
            if not checks:
                raise EditError("couldn't find a test suite to run in this project")
            for label, argv in checks:
                ok, out = codework.run_check(argv, cwd)
                if not ok:
                    raise EditError(f"tests failed ({' '.join(argv)}):\n{out[-1500:]}")
            return f"tests passed ({' '.join(checks[0][1])})"

        if action == "RENAME_SYMBOL":
            old, new = destination, details
            changes = codework.rename_symbol(old, new, cwd, only=[path] if path else None)
            if not changes:
                raise EditError(f"{old} doesn't appear in {path or 'any file'}")
            for rel, (before, after) in changes.items():
                self._write_task_change(cwd, rel, before, after, f"renamed {old} to {new}")
            return f"renamed {old} to {new} in {', '.join(changes)}"

        if action == "MOVE_CODE":
            names = [n.strip() for n in details.split(",") if n.strip()]
            try:
                changes = codework.move_functions(names, path, destination, cwd)
            except ValueError as e:
                raise EditError(str(e))
            for rel, (before, after) in changes.items():
                self._write_task_change(cwd, rel, before, after, f"moved {details} into {destination}")
            return f"moved {details} from {path} into {destination}"

        if action == "COMMIT":
            if not git_status_porcelain(cwd).strip():
                return "nothing to commit -- the working tree is already clean"
            message = details or default_commit_message(summaries, cwd)
            git_add_all(cwd)
            git_commit(message, cwd)
            return f"committed: {message}"

        if action == "CREATE_FOLDER":
            result = create_folder(path, cwd)
            self._push_undo_folder(result["path"], result["label"])
            return f"created folder {result['label']}"

        if action in ("RENAME", "MOVE"):
            result = move_or_rename(path, destination, cwd)
            self._push_undo_move(
                result["src_path"], result["dest_path"], result["src_label"], result["dest_label"],
            )
            verb = "renamed" if action == "RENAME" else "moved"
            return f"{verb} {result['src_label']} to {result['dest_label']}"

        # What else the model sees: files this run already changed (a test
        # being written for a module just created, say) and whatever the
        # request's words point at.
        context = codework.build_context(
            cwd, f"{details} {task}", CONTEXT_BUDGET_ON_DEVICE, exclude=(path,),
            prefer=tuple(s.get("context_files", [])) + tuple(getattr(self, "_task_changed", [])),
        )
        if s.get("note"):
            context = f"Planner's note for {path}: {s['note']}\n\n{context}"
        shared = bool(s.get("shared"))
        if action == "CREATE_FILE":
            proposal = propose_new_file(path, details, cwd, context=context, task=task, expectations=not shared)
        elif action == "EDIT":
            full = resolve_safe_path(path, cwd)
            with open(full, "r", errors="replace") as f:
                content = f.read()
            line_range = choose_edit_range(
                content, path, details, self.backend, self.subagent_roles["planning"]
            )
            proposal = propose_edit(
                path, details, cwd, line_range=line_range, speculative=bool(s.get("optional")),
                context=context, task=task, expectations=not shared,
            )
        else:
            raise EditError(f"unknown step: {action}")

        self._write_task_change(
            cwd, proposal["label"], proposal["original"], proposal["updated"], proposal["summary"],
            existed_before=action == "EDIT",
        )
        return proposal["summary"]

    def _write_task_change(
        self, cwd: str, rel: str, before: str, after: str, summary: str, existed_before: bool | None = None
    ) -> None:
        """Write one file change made by /task: undo entry, diff shown, and
        noted for the verify step."""
        full = resolve_safe_path(rel, cwd)
        if existed_before is None:
            existed_before = os.path.exists(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(after)
        self._push_undo(full, rel, before, existed_before=existed_before)
        self.call_from_thread(
            self._task_step_applied, {"label": rel, "summary": summary}, diff_preview(before, after)
        )
        originals = getattr(self, "_task_originals", None)
        if originals is not None and rel not in originals:
            originals[rel] = before if existed_before else ""
        changed = getattr(self, "_task_changed", None)
        if changed is not None and rel not in changed:
            changed.append(rel)
        created = getattr(self, "_task_created", None)
        if created is not None and not existed_before and rel not in created:
            created.append(rel)
        if not _is_prose(rel):
            self._task_unverified = True

    def _baseline_failures(self, cwd: str, task: str, steps: list[dict]) -> set[str] | None:
        """Tests already failing before this run changes anything, so a
        change isn't blamed (and "repaired") for them -- seen for real: a
        pre-existing failure sent the repair loop after unrelated code.
        None when there's nothing to record (no code changes planned, no
        test suite, or a request to fix the failing tests themselves)."""
        if not self._verify_enabled or any(s["action"] == "FIX_CHECKS" for s in steps):
            return None
        # A fix request's failing tests are most likely the bug being fixed:
        # they have to pass afterwards, not be excused.
        if re.search(r"\b(?:fix|bug|broken|wrong|incorrect|fails?|failing|crash(?:es)?|errors?|off[\s-]by[\s-]one)\b",
                     task, re.IGNORECASE):
            return None
        if not any(s["action"] in ("EDIT", "CREATE_FILE", "RENAME_SYMBOL", "MOVE_CODE") for s in steps):
            return None
        tests = [argv for label, argv in codework.detect_checks(cwd, [], task) if label == "tests"]
        if not tests:
            return None
        failing: set[str] = set()
        for argv in tests:
            ok, out = codework.run_check(argv, cwd)
            if not ok and not codework.no_tests_ran(out):
                failing |= codework.failing_tests(out) or {"<unparsed failure>"}
        if failing:
            self.call_from_thread(
                self._log_progress,
                f"note: {len(failing)} test(s) already fail before any change -- only new failures will count",
            )
        return failing

    def _check_behavior(self, cwd: str) -> tuple[str, list[str], str, str] | None:
        """Behavior checks that need no model, run on a copy of the project:
        documented examples of functions this run changed ("'45m' -> 45" in
        a docstring, or doctests), and new standalone scripts run as-is
        (seen for real: a new script importing pandas, which wasn't
        installed). Returns ("behavior", argv, output, file) for the first
        failure, else None."""
        for rel in self._task_changed:
            if not rel.endswith(".py") or not os.path.isfile(os.path.join(cwd, rel)):
                continue
            after = codework.read_text(cwd, rel)
            names = codework.changed_functions(rel, self._task_originals.get(rel, ""), after)
            script = codework.docstring_examples_script(rel, after, names) if names else None
            if script:
                self.call_from_thread(self._log_progress, f"checking: {rel}'s documented examples…")
                ok, out, _origin = codework.run_smoke(cwd, script, "python")
                if not ok or (out and "docstring says" in out):
                    return ("behavior", [f"the documented examples in {rel}"], out, rel)
            if rel in self._task_created and codework.runnable_script(rel, after):
                self.call_from_thread(self._log_progress, f"checking: running {rel}…")
                runner = f"import runpy\nrunpy.run_path({rel!r}, run_name='__main__')\n"
                ok, out, origin = codework.run_smoke(cwd, runner, "python")
                if not ok and origin == rel:
                    return ("behavior", [f"python {rel}"], out, rel)
        return None

    def _write_smoke_script(self, cwd: str, targets: list[str], task: str) -> tuple[str, str] | None:
        """A short script that imports the changed modules and calls what
        the request changed, for run_smoke. None if the model can't write
        one."""
        lang = codework.SMOKE_LANGS[os.path.splitext(targets[0])[1]]
        targets = [t for t in targets if codework.SMOKE_LANGS[os.path.splitext(t)[1]] == lang]
        changed = sorted({
            n for t in targets
            for n in codework.changed_functions(t, self._task_originals.get(t, ""), codework.read_text(cwd, t))
        })
        self._smoke_functions = set(changed)
        changed_list = ("these functions (changed by this request): " + ", ".join(changed)) if changed \
            else "the functions or classes this request changed or added"
        sources = "\n\n".join(
            f"--- {t} ---\n{codework.excerpt_for(cwd, t, task + ' ' + ' '.join(changed))}" for t in targets
        )
        how = (
            "import them (by module name, e.g. `import stats` for stats.py)"
            if lang == "python" else "require them (e.g. `require('./stats.js')`)"
        )
        prompt = (
            f"These files were just changed for this request: {task}\n\n{sources}\n\n"
            f"Write a short {lang} script that {how} and calls only {changed_list}, with arguments "
            f"of the right types, printing the results. Use any specific inputs the request itself "
            f"mentions, plus a few typical ones. Don't use assert, don't read input, and don't "
            f"create, change, or delete any files."
        )
        try:
            return lang, fm_code(prompt)
        except EditError:
            return None

    def _verify_changes(self, cwd: str) -> bool:
        """Run the project's checks over code /task changed; on a failure,
        show the model the output and have it fix the file it points at,
        up to VERIFY_ROUNDS times. False (after saying why) if it still
        fails -- the caller then stops before committing anything."""
        if not getattr(self, "_task_unverified", False) or not self._verify_enabled:
            return True
        task = self._task_request
        about_tests = bool(_ABOUT_TESTS_RE.search(task))
        smoke_script: tuple[str, str] | None = None
        for round_number in range(VERIFY_ROUNDS + 1):
            smoke_origin: str | None = None
            checks = codework.detect_checks(cwd, self._task_changed, task)
            if not checks:
                break
            failure = None
            for label, argv in checks:
                self.call_from_thread(self._log_progress, f"checking: {label}…")
                ok, out = codework.run_check(argv, cwd)
                if not ok and label == "tests" and codework.no_tests_ran(out):
                    ok = True  # a test command that found nothing hasn't failed
                baseline = getattr(self, "_task_baseline", None)
                if not ok and label == "tests" and baseline:
                    now = codework.failing_tests(out)
                    if now and now <= baseline:
                        ok = True  # only the failures that were already there
                if not ok:
                    failure = (label, argv, out)
                    break
            if failure is None:
                failure = self._check_behavior(cwd)
                if failure is not None and failure[0] == "behavior":
                    smoke_origin = failure[3]
                    failure = failure[:3]
            # No test suite to catch a logic error: exercise the changed
            # code with a short model-written script (on a copy of the
            # project) -- seen for real: syntactically valid code that
            # crashed with an AttributeError the moment it was called.
            targets = codework.smoke_targets(self._task_changed, cwd)
            if failure is None and targets and not codework.has_test_suite(checks):
                if smoke_script is None:
                    smoke_script = self._write_smoke_script(cwd, targets, task)
                if smoke_script is not None:
                    lang, script = smoke_script
                    self.call_from_thread(self._log_progress, "checking: a quick run of the changed code…")
                    ok, out, origin = codework.run_smoke(cwd, script, lang)
                    if origin == "timeout" and len(targets) == 1:
                        origin = targets[0]
                    elif origin in targets and getattr(self, "_smoke_functions", None):
                        # A crash inside a function this run didn't touch
                        # means the script called it wrongly (seen for
                        # real: an int passed where a dict belongs).
                        if codework.crash_function(out) not in self._smoke_functions:
                            origin = None
                    if not ok and origin in targets:
                        failure = ("smoke run", [f"a quick run of {', '.join(targets)}"], out)
                        smoke_origin = origin
                    else:
                        # A crash anywhere but a changed file is the
                        # throwaway script's problem, not the change's.
                        checks = checks + [("smoke run", [])]
            if failure is None:
                self.call_from_thread(
                    self._log_progress, "checks passed: " + ", ".join(label for label, _ in checks)
                )
                break
            label, argv, out = failure
            tail = out[-1500:]
            if round_number == VERIFY_ROUNDS:
                self.call_from_thread(
                    self._log_progress,
                    f"error: {label} still failing after {VERIFY_ROUNDS} fix attempts -- stopping "
                    f"before anything else (nothing committed). Output:\n{tail}",
                )
                return False
            all_files = codework.project_files(cwd)
            candidates = [f for f in self._task_changed if os.path.isfile(os.path.join(cwd, f))]
            candidates += [f for f in codework.files_in_output(out, all_files, cwd) if f not in candidates]
            if not about_tests:
                code = [f for f in candidates if not codework.is_test_file(f)]
                if not code:
                    # A plain assertion failure only shows the test file:
                    # the bug is in the code that test imports.
                    for t in [f for f in candidates if codework.is_test_file(f)]:
                        code += [m for m in codework.local_imports(cwd, t, all_files) if m not in code]
                candidates = code or candidates
            pointed = codework.files_in_output(out, candidates, cwd)
            ordered = pointed + [c for c in candidates if c not in pointed]
            if smoke_origin:
                # A smoke-run crash: fix exactly the file it came from.
                ordered = [smoke_origin]
            # A test written in this same run can itself be wrong (seen for
            # real: expecting 15°C to be 61°F). If the code under test comes
            # back unchanged -- the model standing by it -- try the new test.
            ordered += [
                f for f in codework.files_in_output(out, self._task_created, cwd)
                if codework.is_test_file(f) and f not in ordered
            ]
            if not ordered:
                self.call_from_thread(self._log_progress, f"error: {label} failed:\n{tail}")
                return False
            fixed = False
            for target in ordered[:3]:
                self.call_from_thread(
                    self._log_progress, f"{label} failed -- fixing {target} (attempt {round_number + 1}):\n{tail[-600:]}"
                )
                context = codework.build_context(
                    cwd, f"{task}\n{out}", CONTEXT_BUDGET_ON_DEVICE, exclude=(target,),
                    prefer=tuple(f for f in self._task_changed if f != target),
                )
                try:
                    proposal = propose_edit(
                        target, task, cwd, context=context, task=task, expectations=False,
                        feedback=(
                        f"After the change, running `{' '.join(os.path.basename(a) for a in argv)}` "
                        f"fails with:\n{tail}\nFix {target} so it passes."
                    ) if label != "smoke run" else (
                        f"After the change, calling the code crashes:\n{tail}\nFix {target} so it works."
                    ),
                    )
                except EditError as e:
                    self.call_from_thread(self._log_progress, f"couldn't fix {target}: {e}")
                    continue
                self._write_task_change(cwd, target, proposal["original"], proposal["updated"], f"fixed {target}")
                fixed = True
                break
            if not fixed:
                continue
        self._task_unverified = False
        return True

    def _task_step_applied(self, proposal: dict, diff: str) -> None:
        self._add_message(
            Message("system", f"wrote {proposal['label']}: {proposal['summary']}\n\n{diff}")
        )

    def _log_progress(self, text: str) -> None:
        self._add_message(Message("system", text))

    def _note_planning_fallback(self, model_used: str) -> None:
        """If a /task or /ask planning call actually used a different
        model than the configured "planning" role (Backend fell back
        internally), keep that role pointed at what's actually working for
        the rest of this run instead of re-attempting a known-dead tier
        every single step, and say so once. Safe to call from a worker
        thread -- plain dict mutation, same as the rest of /task's state.
        """
        if model_used != self.subagent_roles["planning"]:
            previous = self.subagent_roles["planning"]
            self.subagent_roles["planning"] = model_used
            self.call_from_thread(
                self._log_progress,
                f"{model_label(previous)} unavailable — planning role switched to "
                f"{model_label(model_used)}",
            )

    @staticmethod
    def _sanitize_session_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", name)

    def _handle_save(self, arg: str) -> None:
        name = arg.strip()
        if not name:
            self._add_message(Message("system", "usage: /save <name>"))
            return

        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = os.path.join(SESSIONS_DIR, f"{self._sanitize_session_name(name)}.json")

        on_device_transcript = None
        transcript_path = self.backend._transcript_path
        if transcript_path and os.path.isfile(transcript_path):
            try:
                with open(transcript_path, "r") as f:
                    on_device_transcript = json.load(f)
            except (OSError, json.JSONDecodeError):
                on_device_transcript = None

        snapshot = {
            "model": self.model,
            "turn": self.turn,
            "messages": [{"role": m.role, "text": m.text} for m in self._message_log],
            "on_device_transcript": on_device_transcript,
            "cloud_history": self.backend._cloud_history,
            "ollama_history": self.backend._ollama_history,
        }
        try:
            with open(path, "w") as f:
                json.dump(snapshot, f, indent=2)
        except OSError as e:
            self._add_message(Message("system", f"couldn't save session '{name}': {e}"))
            return
        self._add_message(Message("system", f"saved session '{name}'"))

    def _handle_license(self) -> None:
        """Hand the terminal to `fm license` so the user can read Apple's
        terms and answer its prompt themselves, then re-check."""
        try:
            with self.suspend():
                subprocess.run(["fm", "license"])
        except FileNotFoundError:
            self._add_message(Message("system", "Apple's `fm` command wasn't found -- fm-pcc needs macOS 27."))
            return
        except Exception as e:
            self._add_message(Message(
                "system", f"couldn't open the license prompt here ({e}) -- run `fm license` in a terminal instead."
            ))
            return
        problem = on_device_problem()
        self._add_message(Message("system", problem or "the on-device model is ready."))

    @work(thread=True)
    def _run_shell(self, command: str) -> None:
        """/run: the user's own command, run in the current directory --
        the explicit, confirmed way to run something /task won't run on
        its own."""
        try:
            result = subprocess.run(
                command, shell=True, cwd=os.getcwd(), capture_output=True, text=True,
                timeout=600, stdin=subprocess.DEVNULL,
            )
            out = (result.stdout + result.stderr).strip()
            text = f"$ {command}  (exit {result.returncode})" + (f"\n{out[-6000:]}" if out else "")
        except subprocess.TimeoutExpired:
            text = f"$ {command}  (timed out after 10 minutes)"
        except OSError as e:
            text = f"$ {command}  (couldn't run: {e})"
        self.call_from_thread(self._log_progress, text)
        self.call_from_thread(self._enable_input)

    def _handle_export(self, arg: str) -> None:
        """Write everything on screen to a file (Markdown by default; .txt
        and .json by extension) or, with "copy", to the clipboard. Unlike
        /save, this is for reading or sharing, not for /resume."""
        # Leave out /export's own bookkeeping: the echoed commands and
        # their status lines, from this export and any earlier ones.
        messages = [
            msg for msg in self._transcript
            if not (msg.role == "user" and msg.text.startswith("/export"))
            and not (msg.role == "system" and msg.text.startswith(_EXPORT_STATUS_PREFIXES))
        ]
        if not messages:
            self._add_message(Message("system", "nothing to export yet"))
            return
        target = arg.strip()
        cwd = os.getcwd()
        header = {
            "exported": time.strftime("%Y-%m-%d %H:%M"),
            "directory": cwd,
            "model": model_label(self.model),
            "version": __version__,
        }

        if target.lower() in ("copy", "clipboard"):
            text = render_transcript(messages, "md", header)
            try:
                subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                self._add_message(Message("system", f"couldn't copy to the clipboard: {e}"))
                return
            self._add_message(Message("system", f"copied the transcript ({len(messages)} messages) to the clipboard"))
            return

        if not target:
            target = f"fm-pcc-transcript-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path = os.path.abspath(os.path.join(cwd, os.path.expanduser(target)))
        ext = os.path.splitext(path)[1].lower()
        fmt = {".txt": "txt", ".json": "json"}.get(ext, "md")
        if not ext:
            path += ".md"
        if os.path.exists(path):
            self._add_message(Message("system", f"couldn't export: {path} already exists -- pick another name"))
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(render_transcript(messages, fmt, header))
        except OSError as e:
            self._add_message(Message("system", f"couldn't export: {e}"))
            return
        self._add_message(Message("system", f"exported {len(messages)} messages to {path}"))

    def _handle_resume(self, arg: str) -> None:
        name = arg.strip()
        if not name:
            try:
                saved = sorted(
                    f[:-5] for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")
                )
            except OSError:
                saved = []
            if not saved:
                self._add_message(Message("system", "no saved sessions — try /save <name> first"))
            else:
                self._add_message(
                    Message("system", "saved sessions:\n" + "\n".join(f"  {s}" for s in saved))
                )
            return

        path = os.path.join(SESSIONS_DIR, f"{self._sanitize_session_name(name)}.json")
        if not os.path.isfile(path):
            self._add_message(Message("system", f"no saved session named '{name}'"))
            return
        try:
            with open(path, "r") as f:
                snapshot = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._add_message(Message("system", f"couldn't read session '{name}': {e}"))
            return

        self.backend._cloud_history = snapshot.get(
            "cloud_history", {m: [] for m in CLOUD_SHORTCUTS}
        )
        self.backend._ollama_history = snapshot.get("ollama_history", {})

        transcript = snapshot.get("on_device_transcript")
        if transcript is not None:
            transcript_path = os.path.join(tempfile.gettempdir(), f"fm-pcc-{uuid.uuid4().hex}.json")
            with open(transcript_path, "w") as f:
                json.dump(transcript, f)
            self.backend._transcript_path = transcript_path
        else:
            self.backend._transcript_path = None

        self.query_one("#log", VerticalScroll).remove_children()
        self._message_log = []
        self._transcript = []
        for m in snapshot.get("messages", []):
            self._add_message(Message(m["role"], m["text"]))

        self.turn = snapshot.get("turn", 0)
        self.model = snapshot.get("model", self.model)
        self._update_chrome()
        self._add_message(Message("system", f"resumed session '{name}'"))

    def _handle_undo(self) -> None:
        if not self._undo_stack:
            self._add_message(Message("system", "nothing to undo"))
            return
        entry = self._undo_stack.pop()
        kind = entry.get("kind", "write")
        try:
            if kind == "write":
                if entry.get("existed_before", True):
                    with open(entry["path"], "w") as f:
                        f.write(entry["original"])
                    self._add_message(Message("system", f"reverted {entry['label']}"))
                else:
                    os.remove(entry["path"])
                    self._add_message(Message("system", f"removed {entry['label']} (undid its creation)"))
            elif kind == "folder":
                os.rmdir(entry["path"])
                self._add_message(Message("system", f"removed folder {entry['label']} (undid its creation)"))
            elif kind == "move":
                shutil.move(entry["dest_path"], entry["src_path"])
                self._add_message(
                    Message("system", f"moved {entry['dest_label']} back to {entry['src_label']}")
                )
        except OSError as e:
            label = entry.get("label") or entry.get("dest_label", "?")
            self._add_message(Message("system", f"couldn't undo the change to {label}: {e}"))

    def _handle_push(self) -> None:
        cwd = os.getcwd()
        try:
            status = git_status_porcelain(cwd)
        except EditError as e:
            self._add_message(Message("system", str(e)))
            return
        except (OSError, subprocess.TimeoutExpired) as e:
            self._add_message(Message("system", f"couldn't check git status: {e}"))
            return
        if not status.strip() and not git_has_unpushed_commits(cwd):
            self._add_message(
                Message("system", "nothing to push -- working tree is clean and nothing is ahead of the remote")
            )
            return

        self.query_one(Input).disabled = True
        self._add_message(Message("system", "pushing…"))
        self._run_push()

    @work(thread=True)
    def _run_push(self) -> None:
        cwd = os.getcwd()
        message = self._last_task_description or "automated changes"
        try:
            # Only stage/commit if there's actually something dirty -- /task
            # may have already committed everything itself (via GIT_ADD/
            # GIT_COMMIT), leaving nothing to add and a `git commit` with
            # nothing staged would fail before ever reaching the push.
            if git_status_porcelain(cwd).strip():
                git_add_all(cwd)
                git_commit(f"fm-pcc: {message}", cwd)
            git_push(cwd)
        except (EditError, OSError, subprocess.TimeoutExpired) as e:
            self.call_from_thread(self._push_finished, False, str(e))
            return

        self.call_from_thread(self._push_finished, True, "")

    def _push_finished(self, success: bool, detail: str) -> None:
        self._enable_input()
        if success:
            self._add_message(Message("system", "pushed."))
        else:
            self._add_message(Message("system", f"push failed: {detail[:300]}"))

    def _handle_pull(self) -> None:
        self.query_one(Input).disabled = True
        self._add_message(Message("system", "pulling…"))
        self._run_pull()

    @work(thread=True)
    def _run_pull(self) -> None:
        try:
            output = git_pull(os.getcwd())
        except (EditError, OSError, subprocess.TimeoutExpired) as e:
            self.call_from_thread(self._pull_finished, False, str(e))
            return
        self.call_from_thread(self._pull_finished, True, output)

    def _pull_finished(self, success: bool, detail: str) -> None:
        self._enable_input()
        if success:
            self._add_message(Message("system", f"pulled.\n{detail}" if detail else "pulled."))
        else:
            self._add_message(Message("system", f"pull failed: {detail[:300]}"))

    def _handle_branch(self, arg: str) -> None:
        parts = arg.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in ("create", "switch"):
            self._add_message(Message("system", "usage: /branch create <name>, or /branch switch <name>"))
            return
        action, name = parts
        self.query_one(Input).disabled = True
        self._add_message(Message("system", f"{'creating' if action == 'create' else 'switching to'} branch {name}…"))
        self._run_branch(action, name)

    @work(thread=True)
    def _run_branch(self, action: str, name: str) -> None:
        cwd = os.getcwd()
        try:
            if action == "create":
                git_create_branch(name, cwd)
            else:
                git_switch_branch(name, cwd)
        except (EditError, OSError, subprocess.TimeoutExpired) as e:
            self.call_from_thread(self._branch_finished, False, name, str(e))
            return
        self.call_from_thread(self._branch_finished, True, name, "")

    def _branch_finished(self, success: bool, name: str, detail: str) -> None:
        self._enable_input()
        if success:
            self._add_message(Message("system", f"now on branch {name}"))
        else:
            self._add_message(Message("system", f"branch operation failed: {detail[:300]}"))

    def _handle_subagents(self, arg: str) -> None:
        if not arg:
            lines = "\n".join(
                f"  {role} → {model_label(model)}" for role, model in self.subagent_roles.items()
            )
            self._add_message(
                Message(
                    "system",
                    f"subagent roles (used by /ask and /task):\n{lines}\n\n"
                    f"set with: /subagents [planning|building] <model>",
                )
            )
            return

        parts = arg.split(maxsplit=1)
        valid_model = len(parts) == 2 and (parts[1] in MODEL_ORDER or parts[1].startswith("ollama:"))
        if len(parts) != 2 or parts[0] not in self.subagent_roles or not valid_model:
            choices = ", ".join(MODEL_ORDER)
            self._add_message(
                Message(
                    "system",
                    f"usage: /subagents [planning|building] <model> — model must be "
                    f"one of: {choices}, or ollama:<name>",
                )
            )
            return

        role, model = parts
        self.subagent_roles[role] = model
        self._add_message(Message("system", f"{role} → {model_label(model)}"))

    @work(thread=True)
    def _run_ask(self, question: str) -> None:
        """Orchestrator/worker for Q&A: the "planning" role either answers
        directly or splits the question into sub-questions, each dispatched
        to the "building" role, then "planning" synthesizes a final answer
        from that research. Read-only -- no files are touched.
        """
        self._loop_running = True
        self._loop_cancel_requested = False
        planning_model = self.subagent_roles["planning"]
        building_model = self.subagent_roles["building"]
        start_time = time.monotonic()
        try:
            # The files the question is about -- without them, measured,
            # /ask confidently named the wrong file and described a filter
            # function as computing a factorial.
            cwd = os.getcwd()
            budget = CONTEXT_BUDGET_ON_DEVICE + 2000 if planning_model == "on-device" else CONTEXT_BUDGET_CLOUD
            context = codework.build_context(cwd, question, budget)
            if context and planning_model == "on-device":
                # One call with the code in view beats splitting the
                # question on a 4096-token model: each sub-question would
                # need the same files again.
                self.call_from_thread(self._log_progress, "on-device is reading the project…")
                definitions = codework.find_definitions(cwd, question)
                found = ("Definitions matching the question:\n" + "\n".join(definitions) + "\n\n") if definitions else ""
                answer = self.backend.classify(
                    f"The project's files:\n\n{context}\n\n{found}Question: {question}\n\n"
                    "Answer the question in plain words first, then back it up with the specifics "
                    "from these files: the exact names and values, and which file (and function) "
                    "they're in.",
                    "on-device",
                )
                self.call_from_thread(self._ask_answered, answer.strip())
                return
            self.call_from_thread(
                self._log_progress, f"{model_label(planning_model)} is thinking this through…"
            )
            plan = decompose_question(question, self.backend, planning_model, context)
            self._note_planning_fallback(plan["model_used"])
            planning_model = self.subagent_roles["planning"]

            if "answer" in plan:
                self.call_from_thread(self._ask_answered, plan["answer"])
                return

            subquestions = plan["subquestions"]
            self.call_from_thread(
                self._log_progress,
                f"breaking this into {len(subquestions)} sub-question(s) for "
                f"{model_label(building_model)}…",
            )

            subanswers = []
            for i, subq in enumerate(subquestions, start=1):
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    return
                self.call_from_thread(self._log_progress, f"  {i}. {subq}")
                sub_budget = CONTEXT_BUDGET_ON_DEVICE if building_model == "on-device" else CONTEXT_BUDGET_CLOUD
                sub_context = codework.build_context(cwd, f"{question} {subq}", sub_budget) if context else ""
                answer = self.backend.classify(
                    (f"The project's files:\n\n{sub_context}\n\n" if sub_context else "") + subq, building_model
                )
                subanswers.append((subq, answer))

            self.call_from_thread(
                self._log_progress, f"{model_label(planning_model)} is synthesizing an answer…"
            )
            final = synthesize_answer(question, subanswers, self.backend, planning_model, context)
            self.call_from_thread(self._ask_answered, final)
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        except Exception as e:
            self.call_from_thread(self._log_progress, f"error: ask failed: {e}")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)
            if time.monotonic() - start_time >= NOTIFY_MIN_SECONDS:
                notify("fm-pcc", f"/ask finished: {question}")

    def _ask_answered(self, answer: str) -> None:
        self.turn += 1
        self._add_message(Message("assistant", answer))
        self._update_chrome()

    @work(thread=True)
    def _run_compare(self, question: str) -> None:
        """Ask every model the same question, one at a time, showing each
        answer as it comes back. Uses classify() (history-free) for all of
        them so this never pollutes any model's real conversation.
        """
        self._loop_running = True
        self._loop_cancel_requested = False
        ollama_available = self._ollama_available()
        try:
            for m in MODEL_ORDER:
                if self._loop_cancel_requested:
                    self.call_from_thread(self._log_progress, "stopped")
                    return
                if m in self.backend._icloud_plus_unavailable:
                    self.call_from_thread(
                        self._log_progress, f"skipping {model_label(m)} (requires iCloud+)"
                    )
                    continue
                if m in self.backend._session_unavailable:
                    self.call_from_thread(
                        self._log_progress,
                        f"skipping {model_label(m)} ({self.backend._session_unavailable[m]})",
                    )
                    continue
                if m == "ollama" and not ollama_available:
                    self.call_from_thread(
                        self._log_progress, f"skipping {model_label(m)} (not running)"
                    )
                    continue
                self.call_from_thread(self._log_progress, f"asking {model_label(m)}…")
                try:
                    answer = self.backend.classify(question, m)
                except GenerationCancelled:
                    raise
                except Exception as e:
                    answer = f"(error: {e})"
                self.call_from_thread(self._compare_result, m, answer)
        except GenerationCancelled:
            self.call_from_thread(self._log_progress, "stopped")
        finally:
            self._loop_running = False
            self.call_from_thread(self._enable_input)

    def _compare_result(self, model: str, answer: str) -> None:
        self._add_message(
            Message("assistant", f"{model_label(model)}:\n{answer}")
        )

    def _show_edit_proposal(self, proposal: dict, diff: str) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        self._pending_edit = proposal
        self._add_message(
            Message(
                "system",
                f"{proposal['summary']}\n\n{diff}\n"
                f"/apply to write this change to {proposal['label']}, /discard to cancel",
            )
        )
        self._enable_input()

    def _apply_pending_edit(self) -> None:
        if not self._pending_edit:
            self._add_message(Message("system", "no pending edit — try /edit <path> <instructions>"))
            return
        proposal = self._pending_edit
        try:
            with open(proposal["path"], "w") as f:
                f.write(proposal["updated"])
            self._push_undo(proposal["path"], proposal["label"], proposal["original"])
            self._add_message(Message("system", f"wrote {proposal['label']}"))
        except OSError as e:
            self._add_message(Message("system", f"failed to write {proposal['label']}: {e}"))
        self._pending_edit = None

    def _history_up(self) -> None:
        if not self._history:
            return
        input_widget = self.query_one(Input)
        if self._history_index is None:
            self._history_draft = input_widget.value
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._recall_history(input_widget)

    def _history_down(self) -> None:
        if self._history_index is None:
            return
        input_widget = self.query_one(Input)
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
        else:
            self._history_index = None
        self._recall_history(input_widget)

    def _recall_history(self, input_widget: Input) -> None:
        value = (
            self._history[self._history_index]
            if self._history_index is not None
            else self._history_draft
        )
        self._suppress_palette_once = True
        input_widget.value = value
        input_widget.cursor_position = len(value)
        self.query_one("#palette", OptionList).display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        self._update_palette(event.value)

    def _update_palette(self, value: str) -> None:
        if self._model_picker_active:
            # A deferred Input.Changed (e.g. from clearing the input after
            # submit) can arrive after the picker has already opened -- don't
            # let ordinary slash-filter logic clobber it.
            return

        command_name = value[1:].split(" ", 1)[0].lower() if value.startswith("/") else ""
        is_complete = command_name in self.COMMANDS or command_name in self.COMMAND_ALIASES
        self.query_one(Input).styles.color = (
            "#ffff00" if is_complete else self.NORMAL_INPUT_COLOR
        )

        palette = self.query_one("#palette", OptionList)
        if self._suppress_palette_once:
            # Programmatic recall (history up/down) fires a deferred
            # Input.Changed just like typing does -- don't let it reopen the
            # filter dropdown on top of a history recall.
            self._suppress_palette_once = False
            palette.display = False
            return

        if value.startswith("/") and " " not in value:
            query = value[1:].lower()
            matches = [name for name in self.COMMANDS if name.startswith(query)]
            if matches:
                palette.clear_options()
                for name in matches:
                    palette.add_option(Option(f"/{name}  {self.COMMANDS[name]}", id=name))
                palette.highlighted = 0
                palette.display = True
                return
        palette.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if not self._model_picker_active:
            return
        self._select_model(event.option.id)
        self._close_model_picker()

    def on_key(self, event: events.Key) -> None:
        palette = self.query_one("#palette", OptionList)

        if self._model_picker_active:
            # Up/down/enter are OptionList's own bindings (it has real focus
            # while the picker is open) -- selection comes through
            # on_option_list_option_selected. Only handle what it doesn't.
            if event.key == "tab" and palette.highlighted is not None:
                option = palette.get_option_at_index(palette.highlighted)
                self._select_model(option.id)
                self._close_model_picker()
                event.prevent_default()
                event.stop()
            elif event.key == "escape":
                self._close_model_picker()
                event.prevent_default()
                event.stop()
            return

        if not palette.display:
            if event.key == "up":
                self._history_up()
                event.prevent_default()
                event.stop()
            elif event.key == "down":
                self._history_down()
                event.prevent_default()
                event.stop()
            elif event.key == "escape":
                self._handle_escape()
                event.prevent_default()
                event.stop()
            return
        if event.key == "down":
            palette.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            palette.action_cursor_up()
            event.prevent_default()
            event.stop()
        elif event.key == "tab":
            if palette.highlighted is not None:
                option = palette.get_option_at_index(palette.highlighted)
                input_widget = self.query_one(Input)
                input_widget.value = f"/{option.id} "
                input_widget.cursor_position = len(input_widget.value)
            palette.display = False
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            palette.display = False
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#palette", OptionList).display = False
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""

        if not self._history or self._history[-1] != prompt:
            self._history.append(prompt)
        self._history_index = None
        self._history_draft = ""

        # Always echo what was actually typed, slash command or not -- a
        # command's own response (e.g. /compare's "asking on-device…" log)
        # otherwise appears with no visible record of what was asked.
        self._add_message(Message("user", prompt))

        if prompt.startswith("/"):
            self._handle_command(prompt)
            return

        if self._route_chat_to_task(prompt):
            return

        event.input.disabled = True

        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            self._add_message(Message("system", f"attached: {', '.join(attachments)}"))

        if self.turn == 0:
            cwd = os.getcwd()
            expanded = f"{build_environment_header(cwd)}\n\n{expanded}"
            self._add_message(Message("system", f"context: {cwd} and its contents included"))

        self._thinking = self._add_message(Message("thinking", ""))
        self._respond(expanded)

    def _route_chat_to_task(self, prompt: str) -> bool:
        """Chat can't change files or run git -- only /task can -- and the
        chat model will just say "I can't do that". So a plain message
        /task fully understands ("can you please commit and push") runs as
        /task directly, and "yes, do that" right after a change request
        runs that request. Returns True if it was routed."""
        request = None
        if self._pending_action_request and taskplan.is_affirmation(prompt):
            request = self._pending_action_request
        else:
            files, folders = gather_task_tree(os.getcwd())
            if taskplan.is_direct_action(prompt, files, folders):
                request = prompt
        self._pending_action_request = None
        if request is None:
            if taskplan.looks_like_action(prompt):
                self._pending_action_request = prompt
            return False
        self._add_message(Message("system", f"running as /task: {request}"))
        self._last_task_description = request
        self.query_one(Input).disabled = True
        self._run_task(request)
        return True

    @work(thread=True)
    def _respond(self, prompt: str) -> None:
        model = self.model
        try:
            text, model_used = self.backend.respond(prompt, model)
            fell_back = (model, model_used) if model_used != model else None
            self.call_from_thread(self._finish_turn, text, None, fell_back)
        except GenerationCancelled:
            self.call_from_thread(self._finish_cancelled)
        except Exception as e:
            self.call_from_thread(self._finish_turn, None, str(e), None)

    def _unavailable_reason_text(self, model: str) -> str:
        if model in self.backend._icloud_plus_unavailable:
            return "requires iCloud+ on this account"
        return self.backend._session_unavailable.get(model, "is unavailable")

    def _finish_turn(
        self, text: str | None, error: str | None, fell_back: tuple[str, str] | None = None
    ) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        if fell_back is not None:
            requested, used = fell_back
            self.model = used
            self._add_message(
                Message(
                    "system",
                    f"{model_label(requested)} {self._unavailable_reason_text(requested)} "
                    f"— switched to {model_label(used)}",
                )
            )
        if error is not None:
            self._add_message(Message("system", f"error: {error}"))
        else:
            self.turn += 1
            self._add_message(Message("assistant", text))
            if self._pending_action_request:
                self._add_message(Message(
                    "system",
                    'to have fm-pcc make this change itself, reply "do it" (runs it as /task)',
                ))
            self._update_chrome()
        self._enable_input()

    def _enable_input(self) -> None:
        input_widget = self.query_one(Input)
        input_widget.disabled = False
        input_widget.focus()

    def _finish_cancelled(self) -> None:
        if self._thinking is not None:
            self._thinking.remove()
            self._thinking = None
        self._add_message(Message("system", "stopped"))
        self._enable_input()

    def _handle_escape(self) -> None:
        input_widget = self.query_one(Input)
        if not input_widget.disabled:
            return  # nothing running -- escape has nothing to do here

        now = time.monotonic()
        if self._last_escape_time and now - self._last_escape_time < 1.5:
            self._last_escape_time = 0.0
            if self._loop_running:
                self._loop_cancel_requested = True
            cancel_active_process()
        else:
            self._last_escape_time = now
            self.query_one("#status", Static).update("press esc again to stop…")
            self.set_timer(1.5, self._update_chrome)


def _add_shortcut_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--shortcut-cloud", metavar="NAME",
        help=f"Override the Shortcuts name for cloud (default: {CLOUD_SHORTCUTS['cloud']['name']})",
    )
    parser.add_argument(
        "--shortcut-cloud-pro", metavar="NAME",
        help=f"Override the Shortcuts name for cloud pro (default: {CLOUD_SHORTCUTS['cloud-pro']['name']})",
    )
    parser.add_argument(
        "--ollama-model", metavar="NAME",
        help="Ollama model to use (default: the first one 'ollama list' returns)",
    )
    parser.add_argument(
        "--ollama-host", metavar="URL", default=OLLAMA_HOST_DEFAULT,
        help=f"Ollama server URL (default: {OLLAMA_HOST_DEFAULT})",
    )


def _shortcut_overrides(args: argparse.Namespace) -> dict[str, str]:
    overrides = {}
    if args.shortcut_cloud:
        overrides["cloud"] = args.shortcut_cloud
    if args.shortcut_cloud_pro:
        overrides["cloud-pro"] = args.shortcut_cloud_pro
    return overrides


def _model_arg(value: str) -> str:
    if value in MODEL_ORDER or value.startswith("ollama:"):
        return value
    choices = ", ".join(MODEL_ORDER)
    raise argparse.ArgumentTypeError(
        f"invalid choice: {value!r} (choose from {choices}, or ollama:<name>)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fm-pcc",
        description="Chat with Apple's on-device, cloud, cloud pro, or local Ollama models.",
    )
    sub = parser.add_subparsers(dest="command")

    respond_p = sub.add_parser("respond", help="Non-interactive one-shot response")
    respond_p.add_argument("prompt", nargs="?", help="Prompt (reads stdin if omitted)")
    respond_p.add_argument("-m", "--model", default="on-device", type=_model_arg)
    _add_shortcut_args(respond_p)

    parser.add_argument(
        "-m", "--model", default="on-device", type=_model_arg,
        help="Starting model for the chat TUI (default: on-device)",
    )
    _add_shortcut_args(parser)

    args = parser.parse_args()

    if args.command == "respond":
        prompt = args.prompt or sys.stdin.read().strip()
        if not prompt:
            parser.error("no prompt given (pass as argument or pipe via stdin)")
        expanded, attachments = expand_file_references(prompt, os.getcwd())
        if attachments:
            print(f"[attached: {', '.join(attachments)}]", file=sys.stderr)
        backend = Backend(
            _shortcut_overrides(args), ollama_model=args.ollama_model, ollama_host=args.ollama_host
        )
        text, model_used = backend.respond(expanded, args.model)
        if model_used != args.model:
            print(
                f"[{model_label(args.model)} unavailable, used {model_label(model_used)} instead]",
                file=sys.stderr,
            )
        print(text)
        return

    ChatApp(
        initial_model=args.model,
        shortcut_overrides=_shortcut_overrides(args),
        ollama_model=args.ollama_model,
        ollama_host=args.ollama_host,
    ).run()


if __name__ == "__main__":
    main()
