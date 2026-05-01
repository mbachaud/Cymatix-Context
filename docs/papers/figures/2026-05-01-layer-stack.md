# Figure spec — Layer-stack diagram

**Used in:** [`docs/papers/2026-05-01-the-same-move-at-every-layer.md`](../2026-05-01-the-same-move-at-every-layer.md)
**Position in paper:** After §2 (after the (a)/(b) test is in the reader's hands; the figure embeds those labels, so the test should be in the reader's head before they see them). Single figure for the post.

---

## What the figure must show

A vertical stack of five horizontal layers. Reader's eye should travel top-to-bottom or bottom-to-top and see the *same two-part move* repeating at each layer. The convergence is the visual punchline.

### Layer order (top → bottom)

| # | Layer | Example systems labeled |
|---|---|---|
| 1 | Model internals | HOPE |
| 2 | KV cache | KVzip, KVPress |
| 3 | Retrieval index | SPLADE · RAPTOR · GraphRAG |
| 4 | Agent memory | MemGPT · Letta |
| 5 | Substrate | **Helix** *(highlighted — see below)* |

### What appears inside each layer band

Two motifs, present in every layer:

1. **Persistent state box** — labeled `(a) state that persists`. A small block on the left side of the band.
2. **Selective expression arrow** — labeled `(b) selects what's expressed`. An arrow on the right side of the band, pointing right (representing flow into the active context for that request).

The motif repeats five times to make the convergence visible. Do not vary the motif per layer — sameness IS the point.

### Helix highlighting convention

Helix gets a single visual marker — *ringed*, not *throned*. Examples that are acceptable:
- A thin ring or border around the substrate band's example label.
- A small caret or asterisk next to "Helix" with a footnote reading the caption.
- Dashed or doubled border around the substrate band itself.

NOT acceptable:
- Helix in a different color while everything else is monochrome (over-emphasis).
- Helix in a larger font or at a different scale than the other example labels.
- A spotlight, glow, or "winner" treatment.

### Caption (mandatory)

> *Helix is one instance at one layer. The shape is the field's, not Helix's.*

Place caption directly below the figure. Italicized.

### Layer-band labels (mandatory)

On the LEFT side of each band, two labels stacked:
- **Top label (bold):** the layer name (e.g., "Model internals").
- **Bottom label (smaller):** an architectural one-liner. Suggested wordings:

| Layer | Architectural one-liner |
|---|---|
| Model internals | "weights / optimizer state" |
| KV cache | "compressed cache as substrate" |
| Retrieval index | "static post-construction" |
| Agent memory | "evolves between requests" |
| Substrate | "cross-process · cross-agent" |

These one-liners are doing genuine work — they preview Findings 2 and 3 visually (static→evolving transition between layers 3 and 4; substrate-specificity at layer 5).

---

## Production options

### Option A — ASCII / Unicode box-drawing  *(recommended)*

Renders inline as a fenced code block on Substack. Zero deps, theme-stable, fastest to ship.

Sketch (final to be tightened):

```
┌─────────────────────────────────────────────────────────────┐
│ MODEL INTERNALS    [(a) weights]  ──(b) inner-loop──▶  HOPE │
│ weights / optimizer state                                   │
├─────────────────────────────────────────────────────────────┤
│ KV CACHE           [(a) cache]    ──(b) eviction──▶  KVzip  │
│ compressed cache as substrate                       KVPress │
├─────────────────────────────────────────────────────────────┤
│ RETRIEVAL INDEX    [(a) index]    ──(b) cos/LLM──▶  SPLADE  │
│ static post-construction                            RAPTOR  │
│                                                   GraphRAG  │
├─────────────────────────────────────────────────────────────┤
│ AGENT MEMORY       [(a) tiers]    ──(b) self-edit──▶ MemGPT │
│ evolves between requests                            Letta   │
├─────────────────────────────────────────────────────────────┤
│ SUBSTRATE          [(a) genome]   ──(b) ΣĒMA + density──▶   │
│ cross-process · cross-agent                       ⟦ Helix ⟧ │
└─────────────────────────────────────────────────────────────┘

Helix is one instance at one layer. The shape is the field's, not Helix's.
```

Strengths: ships today, renders identically across Substack themes, matches the paper's plain field-report voice.
Weaknesses: terminal-aesthetic; some readers find ASCII figures off-putting on a long-form essay.

### Option B — Mermaid diagram

Flowchart-style. Substack support is theme-dependent; needs a render check before committing. If render-stable, this gives slightly more visual hierarchy than ASCII while staying source-controlled.

Strengths: inline-source, version-controllable, more polished than ASCII.
Weaknesses: not all Substack themes render Mermaid cleanly; a render fallback would be needed.

### Option C — Commissioned / hand-drawn image

Final art exported as PNG/SVG and uploaded with the post.

Strengths: highest quality; lets the figure carry real visual weight as the paper's only image.
Weaknesses: real time cost; depends on availability of an illustrator or design tool.

---

## Recommendation

**Option A (ASCII).** Reasons:

1. The paper's voice is field-report, not design-portfolio. ASCII matches.
2. The §7 reviewer flagged that bold-sub-heading prominence varies across Substack themes — Mermaid carries the same render risk at higher stakes.
3. Helix isn't v1.0; investing in commissioned art for a paper that explicitly defers a benchmark fight to a future post would be a tone mismatch.
4. The figure can always be upgraded later if the paper finds a wider audience and a v2 redo is warranted.

The user (paper author) makes the final call.

---

## Out of scope for this spec

- Producing the actual ASCII final art (will follow once Option chosen).
- Inserting the figure into the paper draft (happens after production).
- Color palette decisions (irrelevant for ASCII; deferred to Option B/C if chosen).

## Open questions

1. Does the user want the layer-band one-liners exactly as written above, or should any be sharpened?
2. If Option A, should the figure go directly under §1, or below §2 (which is where the move is *defined* in prose)?
3. Should the caption explicitly include the word "convergence," or is the current wording (*"the shape is the field's, not Helix's"*) preferred?
