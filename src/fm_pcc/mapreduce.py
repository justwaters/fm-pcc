"""Map-reduce over the on-device model's 4096-token window.

Every `fm respond` call is its own fresh session with its own full window,
so material too big for one session is split into pieces, each piece is
handled in its own session (several at once -- measured, three on-device
sessions in parallel take about half the time of three in a row), and the
results are combined. If the combined results still don't fit one window,
they're grouped and combined again: as many tiers as it takes, until a
single session can see everything that's left.

Two things keep answers honest across tiers:

- Map sessions *extract* rather than summarize where they can: they copy
  the lines that matter word for word, and verify_quotes() drops anything
  that isn't really in the source. A summary of a summary drifts; a
  checked quote can't.
- Every piece keeps its source label (file, page, line range), so a final
  answer can say where each fact came from.

This module only splits, schedules, and checks; the model calls are
passed in, so it's testable offline.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

# Measured with `fm count-tokens`: ~3.5 characters per token for both code
# and prose. Budgeting at 3.2 leaves a margin; `fm count-tokens` itself
# takes ~2.5 s a call, too slow to use per piece.
CHARS_PER_TOKEN = 3.2
CONTEXT_TOKENS = 4096
# Room kept for the instructions around a piece and for the reply.
RESERVED_TOKENS = 1400
PIECE_CHARS = int((CONTEXT_TOKENS - RESERVED_TOKENS) * CHARS_PER_TOKEN)  # ~8600
PARALLEL_SESSIONS = 3
MAX_TIERS = 8


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def fits(text: str, budget_chars: int = PIECE_CHARS) -> bool:
    return len(text) <= budget_chars


@dataclass
class Piece:
    source: str          # "app.py", "app.py lines 120-260", "https://…"
    text: str
    start_line: int = 1  # 1-based line of text's first line in the source


def split_text(text: str, source: str, budget_chars: int = PIECE_CHARS, overlap_lines: int = 3) -> list[Piece]:
    """Split `text` into pieces under `budget_chars`, breaking at blank
    lines where possible (paragraphs, between functions) and never
    mid-line, with a few lines of overlap so nothing that straddles a
    boundary is lost. One piece if it already fits."""
    if len(text) <= budget_chars:
        return [Piece(source, text, 1)]
    lines = text.splitlines()
    pieces: list[Piece] = []
    start = 0
    while start < len(lines):
        size, end, last_blank = 0, start, None
        while end < len(lines) and size + len(lines[end]) + 1 <= budget_chars:
            size += len(lines[end]) + 1
            if not lines[end].strip():
                last_blank = end
            end += 1
        if end == start:  # a single line longer than the budget: hard-split it
            line = lines[start]
            for i in range(0, len(line), budget_chars):
                pieces.append(Piece(f"{source} line {start + 1}", line[i:i + budget_chars], start + 1))
            start += 1
            continue
        if end < len(lines) and last_blank is not None and last_blank > start + (end - start) // 2:
            end = last_blank + 1  # break at a paragraph/function boundary
        pieces.append(Piece(f"{source} lines {start + 1}-{end}", "\n".join(lines[start:end]), start + 1))
        if end >= len(lines):
            break
        start = max(end - overlap_lines, start + 1)
    return pieces


def split_documents(docs: list[tuple[str, str]], budget_chars: int = PIECE_CHARS) -> list[Piece]:
    """Pieces for several (source, text) documents: small ones packed
    together into one piece, big ones split."""
    pieces: list[Piece] = []
    batch: list[tuple[str, str]] = []
    batch_len = 0

    def flush():
        nonlocal batch, batch_len
        if batch:
            text = "\n\n".join(f"--- {src} ---\n{t}" for src, t in batch)
            pieces.append(Piece(", ".join(src for src, _ in batch), text, 1))
            batch, batch_len = [], 0

    for source, text in docs:
        block = len(text) + len(source) + 10
        if block > budget_chars:
            flush()
            pieces.extend(
                Piece(p.source, f"--- {p.source} ---\n{p.text}", p.start_line)
                # (room for the "--- source lines N-M ---" header each piece gets)
                for p in split_text(text, source, budget_chars - len(source) - 40)
            )
            continue
        if batch_len + block > budget_chars:
            flush()
        batch.append((source, text))
        batch_len += block
    flush()
    return pieces


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().strip("`").strip()


def verify_quotes(quotes: list[str], source_text: str, min_chars: int = 8) -> list[str]:
    """Only the quotes that really appear in `source_text` (whitespace-
    insensitive) -- a model's "quote" that isn't in the source is dropped."""
    haystack = _norm(source_text)
    out = []
    for q in quotes:
        n = _norm(q).strip('"“”')
        if len(n) >= min_chars and n in haystack and n not in out:
            out.append(n)
    return out


def parse_quote_lines(reply: str) -> list[str]:
    """Quote lines from a map session's reply ("> line" per quote, or
    NONE)."""
    if re.match(r"\s*NONE\b", reply, re.I):
        return []
    quotes = [m.group(1) for m in re.finditer(r"^\s*>\s?(.+)$", reply, re.MULTILINE)]
    if not quotes:
        # Some replies skip the ">" -- take non-empty lines that aren't
        # obviously commentary.
        quotes = [l for l in reply.splitlines() if l.strip() and not re.match(r"\s*(?:here|these|the following|none)\b", l, re.I)]
    return quotes


class MapReduce:
    """Runs map sessions in parallel and reduces their results in as many
    tiers as it takes to fit one window."""

    def __init__(
        self,
        parallel: int = PARALLEL_SESSIONS,
        budget_chars: int = PIECE_CHARS,
        progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ):
        self.parallel = parallel
        self.budget_chars = budget_chars
        self.progress = progress or (lambda _msg: None)
        self.cancelled = cancelled
        self.sessions = 0
        self.tiers = 0
        self.failures = 0
        self._lock = threading.Lock()

    def _count(self, n: int = 1) -> None:
        with self._lock:
            self.sessions += n

    def map(self, items: list, work: Callable, tolerate: bool = True) -> list:
        """work(item) for every item, `parallel` at a time, results in order.
        With `tolerate`, a session that fails (a runaway generation that
        times out, say) gives None and is reported, instead of throwing
        away every other session's work -- seen for real: one timed-out
        piece out of ten failed a whole /ask."""
        if not items:
            return []
        if self.cancelled():
            raise InterruptedError("cancelled")

        def run(item):
            if self.cancelled():
                raise InterruptedError("cancelled")
            self._count()
            try:
                return work(item)
            except InterruptedError:
                raise
            except Exception as e:
                if not tolerate or type(e).__name__ == "GenerationCancelled":
                    raise
                with self._lock:
                    self.failures += 1
                self.progress(f"one part couldn't be read ({str(e)[:80]}) -- continuing without it")
                return None

        if len(items) == 1 or self.parallel <= 1:
            return [run(i) for i in items]
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            return list(pool.map(run, items))

    def reduce(
        self,
        results: list[str],
        combine: Callable[[str, bool], str],
        separator: str = "\n\n",
    ) -> str:
        """Combine `results` into one: while they don't fit a single window
        together, group them into window-sized batches and combine each
        batch (combine(text, final=False)) -- one tier per pass -- then
        combine what's left once more with final=True."""
        results = [r for r in results if r and r.strip()]
        tier = 0
        while True:
            joined = separator.join(results)
            if fits(joined, self.budget_chars) or len(results) <= 1:
                break
            tier += 1
            if tier > MAX_TIERS:
                raise RuntimeError("too much material to combine")
            batches = self._batch(results, separator)
            self.progress(f"combining {len(results)} results in {len(batches)} group(s) (tier {tier + 1})…")
            new = [r for r in self.map(batches, lambda text: combine(text, False)) if r]
            if not new:
                raise RuntimeError("couldn't combine the results")
            if len(new) >= len(results):
                # Combining didn't shrink anything; stop looping and let
                # the final session take the most it can.
                results = new
                break
            results = new
        self.tiers = tier + 1
        joined = separator.join(results)
        if not fits(joined, self.budget_chars):
            joined = joined[: self.budget_chars]
        self._count()
        return combine(joined, True)

    def _batch(self, results: list[str], separator: str) -> list[str]:
        batches, current, size = [], [], 0
        for r in results:
            r = r if len(r) <= self.budget_chars else r[: self.budget_chars]
            if current and size + len(r) + len(separator) > self.budget_chars:
                batches.append(separator.join(current))
                current, size = [], 0
            current.append(r)
            size += len(r) + len(separator)
        if current:
            batches.append(separator.join(current))
        return batches


# ---------------------------------------------------------------------------
# High-level operations (model calls passed in)
# ---------------------------------------------------------------------------

_SUMMARY_RE = re.compile(
    r"\b(?:summari[sz]e|summary|overview|tl;?dr|gist|outline|recap|describe|explain\s+(?:what|how)\s+this)\b", re.I
)


def wants_summary(question: str) -> bool:
    return bool(_SUMMARY_RE.search(question))


def _extract_prompt(question: str, piece: Piece) -> str:
    return (
        f"Question: {question}\n\n"
        f"Text (from {piece.source}):\n{piece.text}\n\n"
        "Copy, word for word, the lines from this text that help answer the question -- at most "
        "8 short lines, one per line, each starting with \"> \". Copy exactly; don't reword or explain. "
        "If nothing in it is relevant, reply NONE."
    )


def _summary_prompt(question: str, text: str, source: str) -> str:
    return (
        f"Request: {question}\n\n"
        f"Part of the material (from {source}):\n{text}\n\n"
        "Summarize this part for that request in at most 120 words. Keep names, numbers, "
        "code identifiers, and decisions exactly as written."
    )


def _answer_prompt(question: str, evidence: str) -> str:
    return (
        f"Question: {question}\n\n"
        f"Evidence gathered from the material, with where each part came from:\n{evidence}\n\n"
        "Answer the question using only this evidence. Name the source (in square brackets) "
        "for each fact you use. If the evidence doesn't answer it, say what's missing."
    )


def answer_over(
    question: str,
    docs: list[tuple[str, str]],
    ask: Callable[[str], str],
    mr: MapReduce,
) -> tuple[str, list[str]]:
    """Answer `question` from `docs` [(source, text)] of any size. Returns
    (answer, sources used). One plain call when everything fits a window;
    otherwise map sessions extract verified quotes (or summarize, for
    summary requests) and the results are reduced in tiers."""
    total = sum(len(t) + len(s) + 10 for s, t in docs)
    if total + len(question) <= mr.budget_chars:
        mr._count()
        material = "\n\n".join(f"--- {s} ---\n{t}" for s, t in docs)
        return ask(f"Question: {question}\n\nMaterial:\n{material}\n\n"
                   "Answer from this material; say which file or source each fact comes from."), [s for s, _ in docs]

    pieces = split_documents(docs, mr.budget_chars - len(question) - 400)
    summary = wants_summary(question)
    mr.progress(f"reading {len(pieces)} part(s) in separate sessions…")

    def work(piece: Piece) -> list[tuple[str, str]]:
        if summary:
            body = ask(_summary_prompt(question, piece.text, piece.source)).strip()
            return [(piece.source, body)] if body else []
        quotes = verify_quotes(parse_quote_lines(ask(_extract_prompt(question, piece))), piece.text)
        # Cite each verified quote by the exact file and line it's on.
        return [(locate(q, docs) or piece.source, f"> {q}") for q in quotes]

    found = [item for items in mr.map(pieces, work) if items for item in items]
    if not found:
        return ("I read all of it, but found nothing in it about that.", [])
    grouped: dict[str, list[str]] = {}
    for src, line in found:
        grouped.setdefault(src, []).append(line)
    sources = sorted({src.split(":")[0] for src in grouped})
    blocks = [f"[{src}]\n" + "\n".join(lines) for src, lines in grouped.items()]

    def combine(text: str, final: bool) -> str:
        if final:
            return ask(_answer_prompt(question, text))
        if summary:
            return ask(_summary_prompt(question, text, "several parts"))
        # Narrow the quotes to the most relevant ones, still verbatim and
        # still labeled -- and re-verified against what came in.
        reply = ask(
            f"Question: {question}\n\nQuoted excerpts, each under its [source]:\n{text}\n\n"
            "Keep only the excerpts that best help answer the question: copy each kept line "
            "exactly, keeping its [source] line above it. Drop the rest."
        )
        kept, current = [], None
        for line in reply.splitlines():
            if re.match(r"^\s*\[[^\]]+\]\s*$", line):
                current = line.strip()
                kept.append(current)
            elif line.strip().startswith(">") and verify_quotes([line.strip()[1:]], text):
                kept.append("> " + _norm(line.strip()[1:]))
        return "\n".join(kept) if any(k.startswith(">") for k in kept) else text[: mr.budget_chars // 2]

    return mr.reduce(blocks, combine), sources


def locate(quote: str, docs: list[tuple[str, str]]) -> str | None:
    """"file:line" of the first document line a (verified) quote starts on."""
    target = _norm(quote)
    head = target[:40]
    for source, text in docs:
        if target not in _norm(text):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if head and head.startswith(_norm(line)[:40]) and _norm(line):
                return f"{source}:{i}"
            if head and _norm(line).find(head[:20]) >= 0:
                return f"{source}:{i}"
        return source
    return None


def relevant_files(
    request: str,
    docs: list[tuple[str, str]],
    judge: Callable[[str, list[str]], list[dict]],
    mr: MapReduce,
) -> dict[str, list[str]]:
    """Which files matter for `request`, and which of their functions/
    classes: map sessions each judge a window's worth of files (judge
    returns [{"path", "relevant", "names"}] for the paths it was shown);
    results are merged in code, no reduce session needed."""
    pieces = split_documents(docs, mr.budget_chars - len(request) - 600)
    mr.progress(f"looking through {len(docs)} file(s) in {len(pieces)} session(s) for what's relevant…")

    def work(piece: Piece) -> list[dict]:
        paths = [s for s, _ in docs if s in piece.source.split(", ") or piece.source.startswith(s)]
        prompt = (
            f"Request: {request}\n\nFiles:\n{piece.text}\n\n"
            "For each file, say whether it's relevant to carrying out the request, and list the "
            "names of the functions or classes in it that are."
        )
        return judge(prompt, paths)

    out: dict[str, list[str]] = {}
    for verdicts in mr.map(pieces, work):
        for v in verdicts or []:
            if v.get("relevant") and v.get("path"):
                names = [n for n in v.get("names") or [] if n]
                out.setdefault(v["path"], [])
                out[v["path"]] += [n for n in names if n not in out[v["path"]]]
    return out


def condense(
    text: str,
    purpose: str,
    ask: Callable[[str], str],
    mr: MapReduce,
    source: str = "the material",
) -> str:
    """A compact version of `text` for `purpose` (a conversation's history,
    a diff), however long `text` is: one call if it fits, otherwise
    summarized piece by piece and combined in tiers."""
    prompt = lambda body, src: (
        f"{purpose}\n\n{body}\n\n"
        "Condense this in at most 150 words. Keep names, numbers, file names, code identifiers, "
        "and decisions exactly as written."
    )
    if fits(text, mr.budget_chars - len(purpose) - 300):
        mr._count()
        return ask(prompt(text, source)).strip()
    pieces = split_text(text, source, mr.budget_chars - len(purpose) - 300)
    mr.progress(f"condensing {len(pieces)} part(s) in separate sessions…")
    parts = [p for p in mr.map(pieces, lambda p: ask(prompt(p.text, p.source)).strip()) if p]
    return mr.reduce(parts, lambda body, final: ask(prompt(body, "several parts")).strip()).strip()


def find_edit_sites(
    request: str,
    blocks: list[tuple[str, int, int, str]],
    judge: Callable[[str], bool],
    mr: MapReduce,
) -> list[tuple[int, int]]:
    """Which (start, end) blocks of a large file need changing for
    `request`: one session per block (a function, or a chunk), each
    answering yes/no. blocks = [(label, start, end, text)]."""
    mr.progress(f"checking {len(blocks)} part(s) of the file for places to change…")

    def work(block):
        label, start, end, text = block
        return judge(
            f"Request: {request}\n\nThis part of the file ({label}):\n```\n{text}\n```\n\n"
            "Does this part need to change to carry out the request?"
        )

    verdicts = mr.map(blocks, work)
    return [(b[1], b[2]) for b, yes in zip(blocks, verdicts) if yes]


_STOP_WORDS = {"the", "and", "for", "all", "every", "each", "add", "call", "calls", "start", "end", "file",
               "function", "functions", "method", "methods", "make", "change", "with", "that", "this", "into",
               "log", "info", "line", "lines", "one", "them", "their", "its"}


def sites_by_name(request: str, blocks: list[tuple[str, int, int, str]]) -> list[tuple[int, int]] | None:
    """For "every X"/"all X" requests, the blocks whose *names* say they're
    the X ones, without asking a model: request words that match some
    block names but not all ("admin" in "every admin handler" picks the 3
    admin_* handlers; "handler" matches all 60, so it doesn't narrow
    anything). None if the request isn't an "every X" or no word narrows."""
    if not re.search(r"\b(?:every|each|all)\b", request, re.I):
        return None
    named = [b for b in blocks if re.fullmatch(r"[A-Za-z_$][\w$]*", b[0])]
    if not named:
        return None

    def tokens(name: str) -> set[str]:
        parts = re.split(r"_|(?<=[a-z])(?=[A-Z])", name)
        return {p.lower() for p in parts if p}

    words = {w.lower().rstrip("s") for w in re.findall(r"[A-Za-z]{3,}", request)} - _STOP_WORDS
    narrowing = [w for w in words
                 if 0 < sum(1 for b in named if w in {t.rstrip("s") for t in tokens(b[0])}) < len(named)]
    if not narrowing:
        return None
    hits = [b for b in named if all(w in {t.rstrip("s") for t in tokens(b[0])} for w in narrowing)]
    return [(b[1], b[2]) for b in hits] or None
