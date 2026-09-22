# Google 2MB HTML Limit Checker

Checks a page against Google's 2,097,152-byte (2 MiB) HTML fetch limit and shows
exactly where the cut lands — which top-level elements are **kept**, **partially
clipped**, or **completely lost** — plus an optional AI review of what's bloat.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
```

`playwright install chromium` downloads a headless browser (~150MB, one-off). It's
only used to screenshot the truncated render for reports — skip it and everything
else still works; report generation just notes that the capture failed.

Open **http://localhost:5731**

To enable the AI panel, click **Settings** in the top bar and pick a provider:

| Provider | Notes |
| --- | --- |
| **OpenRouter** (default) | One key, hundreds of models from every major lab |
| **OpenAI** | Direct to OpenAI |
| **Other** | Any OpenAI-compatible endpoint — Groq, Together, a local Ollama… |

Paste your key and the model dropdown fills with everything that key can reach,
so you can switch models without touching any code. The key is verified before
it's saved, then written to a local `.env` next to `app.py` (already gitignored)
and loaded automatically on future runs. It's only ever sent to the provider you
picked. A plain `OPENAI_API_KEY` in your shell still works as an OpenAI setup,
but anything saved through Settings takes precedence.

## Usage

1. Paste a URL (fetched as Googlebot) or raw HTML into the input.
2. **Run check** — see the size meter, cut point, element breakdown, and the
   **risk verdict** with deterministic content flags (below). No API key needed.
3. **Run analysis** — reads the *actual HTML lost below the cut* and rates ranking
   impact. It flags ranking-critical tags that fall past the cut (JSON-LD schema,
   canonical, meta robots, hreflang, title, headings), rates each lost section by
   how vital it is to Google (critical → irrelevant), and gives an overall
   ranking-impact verdict — e.g. lost structured data is serious, lost cart JS is not.
   It then recommends concrete **reductions** to trim the retained (above-the-cut)
   HTML back under 2MB, each with an estimated saving and a safe / test-first risk flag.
4. **Render Google's view** — a sandboxed visual approximation of the page rendered
   only up to the 2MB cut, so you can see what Googlebot sees with the below-cut
   content missing. A red bar marks where the cut falls. External CSS/images load
   from the live origin (so it's approximate, not pixel-perfect) and scripts are
   disabled for safety.
5. **Full vs Snipped HTML** — a side-by-side, searchable source view: the full
   fetched HTML (with everything below the 2MB cut dimmed and a red cut marker) next
   to the snipped HTML Google actually keeps. Sync-scroll, line-wrap, and a search
   box that highlights matches in both panes; very large pages are trimmed near the cut.
6. **Generate report** — runs the whole pipeline for a URL in one click: fetch,
   measure, AI analysis, screenshot, render. A modal reports each stage as it
   finishes, and the export buttons unlock only once the report is complete.
   The report embeds a **full-page screenshot of the truncated render** — the page
   drawn from the HTML above the cut only. It's captured with JavaScript disabled,
   because the 2MB cap applies to the raw HTML fetch: showing JS-injected content
   would show content Google never received in the HTML. On script-heavy sites the
   render will look sparse, and that gap is itself the finding. Very tall pages are
   capped at 14,000px with a note, since a 2MB page can render 700,000px tall.
   - **HTML** saves a standalone, self-contained file (styles inline, no assets to
     ship alongside it) — fine to email or drop in a shared drive.
   - **PDF** opens your browser's print dialog with print stylesheets applied;
     choose *Save as PDF*. This avoids a heavyweight PDF dependency, so there's
     nothing extra to install.

   The AI stage is skipped automatically when the page is within the 2MB limit
   (nothing is lost, so there's nothing to analyse) or when no provider is
   configured — the report still generates with the measured results.

## Content-loss flags (deterministic)

The risk verdict is not just "over/under". Two independent checks run on the lost
region and raise the risk to **HIGH** even when no `<head>` tags are affected:

- **Text content missing below the cut** — any visible, human-readable text (product
  names/descriptions, FAQ answers, body copy) that falls past the cut, *with or
  without a heading*. The flag names the specific lost headings and FAQ questions,
  or shows a sample of the lost copy.
- **Cut off mid product/FAQ list** — the cut landing inside a repeating
  product/listing/FAQ block, so the listing is truncated part-way through and later
  products won't be seen by Google.

Both flags are also fed into the AI verdict as ground truth.

## Full lost-content inventory

The **What's Being Lost** panel lists *every* piece of content cut below the 2MB
line, grouped into collapsible dropdowns by its containing element — so you can see
exactly what is missing and from which div (e.g. `div#faqs › details`). Each item is
tagged by type (Heading, Paragraph, FAQ question, List item, Link, Image alt, …).
There's a filter box and expand/collapse-all controls. This inventory walks the raw
lost HTML directly, so it isn't limited by the element-map's nesting depth. When a
page is over the limit, the Element Breakdown table also defaults to its **Lost**
filter so cut/clipped elements are visible immediately.

## Notes on accuracy

- Google applies the limit to **uncompressed** bytes and stops the fetch *exactly*
  at 2,097,152 bytes; everything after is ignored (not fetched, rendered, or indexed).
- Google's limit **also counts the HTTP request headers** it sends, so the true HTML
  budget is a little under 2 MB. Pages within ~5% of the limit get a **NEAR LIMIT**
  flag, since they can tip over the cut in practice.
- The tool fetches with a Googlebot user-agent, but a site may serve different markup
  to Googlebot than to this fetcher, and JS-rendered content isn't executed — so treat
  the byte count as a close estimate of the raw HTML Googlebot receives, not gospel.
- The limit is measured in **bytes**, not characters — multibyte content counts more.
- Element boundaries are best-effort (regex-based), so partial percentages carry
  some uncertainty on malformed or heavily-nested markup.
- The analysis sends a trimmed sample of the actual lost HTML (from the cut point,
  ~24k chars) plus a deterministic scan of ranking-critical tags — never the whole page.
- Change the port in `app.py` (`PORT = 5731`) if needed.
