#!/usr/bin/env python3
"""
Google 2MB HTML Limit Checker — Paramount Digital
--------------------------------------------------
Google truncates fetched HTML at 2,097,152 bytes (2 MiB). This tool measures
where that cut-off lands in a page's HTML, reports which top-level elements are
fully retained / partially cut / completely lost, and (optionally) asks an AI
model which retained sections look like bloat that could be trimmed.

Run:
    python app.py

Then open http://localhost:5731 and pick an AI provider in the app's Settings
panel to enable the analysis. OpenRouter, OpenAI, or any OpenAI-compatible
endpoint works. Credentials are written to a local .env file (gitignored) and
never leave this machine except in calls to the provider you chose.
"""

import os
import re
import io
import json
import base64
import html as html_lib
from pathlib import Path
from urllib.parse import urlparse

import requests
from datetime import datetime
from flask import Flask, Response, request, jsonify, render_template, stream_with_context

# ---- Config -----------------------------------------------------------------

GOOGLE_LIMIT_BYTES = 2 * 1024 * 1024  # 2,097,152 — Google's documented cap
PORT = 5731                            # uncommon local port

# Every provider here speaks the OpenAI chat-completions dialect, so one client
# covers all of them — only the base URL, key and model name change.
PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o",
        "keys_url": "https://openrouter.ai/keys",
        "hint": "One key, hundreds of models from every major lab.",
        # OpenRouter's /models is public, so it can't prove a key is valid.
        "auth_path": "/key",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "keys_url": "https://platform.openai.com/api-keys",
        "hint": "Direct to OpenAI.",
    },
    "custom": {
        "label": "Other (OpenAI-compatible)",
        "base_url": "",
        "default_model": "",
        "keys_url": "",
        "hint": "Anything speaking the OpenAI API — Groq, Together, a local Ollama, etc.",
    },
}
DEFAULT_PROVIDER = "openrouter"

ENV_FILE = Path(__file__).resolve().parent / ".env"
ENV_PROVIDER, ENV_KEY, ENV_MODEL, ENV_BASE = (
    "AI_PROVIDER", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL"
)

app = Flask(__name__)


# ---- Credential storage (.env) ----------------------------------------------

def load_env_file() -> None:
    """
    Read .env into os.environ. Settings saved through the app override
    shell-set ones, so what the user last saved is what runs.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ[name] = value


def write_env_values(values: dict[str, str | None]) -> None:
    """Persist (or clear) settings in .env, preserving any unrelated entries."""
    lines = []
    if ENV_FILE.exists():
        lines = [
            ln for ln in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if not any(ln.strip().startswith(f"{n}=") for n in values)
        ]
    for name, value in values.items():
        if value:
            lines.append(f'{name}="{value}"')
        os.environ.pop(name, None)
        if value:
            os.environ[name] = value
    ENV_FILE.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def get_ai_config() -> dict:
    """Resolve the active provider settings from the environment."""
    provider = os.environ.get(ENV_PROVIDER, "").strip().lower()
    key = os.environ.get(ENV_KEY, "").strip()

    # A plain OPENAI_API_KEY in the shell is still honoured as an OpenAI setup.
    if not key and os.environ.get("OPENAI_API_KEY", "").strip():
        provider, key = "openai", os.environ["OPENAI_API_KEY"].strip()

    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    preset = PROVIDERS[provider]
    base_url = (os.environ.get(ENV_BASE, "").strip() or preset["base_url"]).rstrip("/")
    model = os.environ.get(ENV_MODEL, "").strip() or preset["default_model"]
    return {"provider": provider, "key": key, "base_url": base_url, "model": model}


def mask_key(key: str) -> str:
    return f"{key[:3]}...{key[-4:]}" if len(key) > 11 else "*" * len(key)


def provider_label(provider: str) -> str:
    return PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])["label"]


def auth_headers(cfg: dict) -> dict:
    headers = {"authorization": f"Bearer {cfg['key']}"}
    if cfg["provider"] == "openrouter":
        # OpenRouter uses these to attribute traffic to the calling app.
        headers["HTTP-Referer"] = "https://paramountdigital.co.uk"
        headers["X-Title"] = "GMC 2MB Limit Checker"
    return headers


def provider_get(cfg: dict, path: str) -> tuple[bool, dict, str]:
    label = provider_label(cfg["provider"])
    try:
        resp = requests.get(
            f"{cfg['base_url']}{path}", headers=auth_headers(cfg), timeout=20
        )
    except requests.RequestException as e:
        return False, {}, f"Could not reach {label}: {e}"
    if resp.status_code in (401, 403):
        return False, {}, f"{label} rejected that key. Check you copied it in full."
    if not resp.ok:
        return False, {}, f"{label} returned {resp.status_code}. Try again shortly."
    try:
        return True, resp.json(), ""
    except ValueError:
        return False, {}, f"{label} returned an unexpected response — check the base URL."


def verify_key(cfg: dict) -> tuple[bool, str]:
    """
    Confirm the key actually authenticates. Providers whose model list is
    public need a dedicated authenticated endpoint to prove anything.
    """
    path = PROVIDERS.get(cfg["provider"], {}).get("auth_path", "/models")
    ok, _, error = provider_get(cfg, path)
    return ok, error


def fetch_models(cfg: dict) -> tuple[bool, list[str], str]:
    """List the models this key can use, so the UI can offer a picker."""
    ok, body, error = provider_get(cfg, "/models")
    if not ok:
        return False, [], error
    entries = body.get("data")
    if not isinstance(entries, list):
        return False, [], "That endpoint did not return a model list."
    return True, sorted(str(m.get("id", "")) for m in entries if m.get("id")), ""


def local_request_only():
    """
    Block cross-origin writes. This app binds to localhost, but a page in the
    user's browser could still POST here, so key-changing routes require a
    same-origin request.
    """
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != f"{request.scheme}://{request.host}":
        return jsonify({"ok": False, "error": "Cross-origin request refused."}), 403
    return None


load_env_file()


# ---- HTML fetching ----------------------------------------------------------

def fetch_html(source: str) -> tuple[str, bytes]:
    """Return (label, raw_bytes) from a URL or raw pasted HTML."""
    src = source.strip()
    if src.lower().startswith(("http://", "https://")):
        headers = {
            # Mimic Googlebot so we get the same markup Google would fetch.
            "User-Agent": (
                "Mozilla/5.0 (compatible; Googlebot/2.1; "
                "+http://www.google.com/bot.html)"
            )
        }
        resp = requests.get(src, headers=headers, timeout=30)
        resp.raise_for_status()
        return urlparse(src).netloc or src, resp.content
    # Treat as raw HTML paste.
    return "pasted HTML", src.encode("utf-8", errors="replace")


# ---- Element mapping --------------------------------------------------------

# We locate every top-level structural element and record its byte span so we
# can classify it against the cut point. Byte offsets matter (not char offsets)
# because Google's limit is measured in bytes and multibyte chars shift things.

BLOCK_TAGS = (
    "div", "section", "article", "header", "footer", "main", "nav",
    "aside", "table", "ul", "ol", "form", "script", "style", "svg",
)


def find_blocks(raw: bytes, max_depth: int = 3):
    """
    Walk the byte stream and return every block element up to `max_depth` levels
    of nesting, each with its byte span, depth, ancestor path, id, classes,
    other useful attrs, and a short text preview of what it contains.

    Tracking nested elements (not just depth-0) is what lets the dashboard show
    *which* section the cut actually lands in — on flat pages the whole body is
    a run of depth-0 siblings and the interesting detail is one level down.
    """
    text = raw.decode("utf-8", errors="replace")

    # Byte offset for each character index (Google's limit is measured in bytes).
    byte_at = [0] * (len(text) + 1)
    acc = 0
    for i, ch in enumerate(text):
        acc += len(ch.encode("utf-8"))
        byte_at[i + 1] = acc

    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")
    void = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}

    blocks = []
    stack = []  # each: dict with tag, char_start, attrs, depth

    for m in tag_re.finditer(text):
        closing, tag, attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag not in BLOCK_TAGS:
            continue
        if closing:
            for j in range(len(stack) - 1, -1, -1):
                if stack[j]["tag"] == tag:
                    node = stack.pop(j)
                    if node["depth"] <= max_depth:
                        _record(blocks, node, byte_at[node["char_start"]],
                                byte_at[m.end()], text, byte_at, stack)
                    break
        elif selfclose or tag in void:
            depth = len(stack)
            if depth <= max_depth:
                node = {"tag": tag, "char_start": m.start(), "attrs": attrs, "depth": depth}
                _record(blocks, node, byte_at[m.start()], byte_at[m.end()],
                        text, byte_at, stack)
        else:
            stack.append({"tag": tag, "char_start": m.start(), "attrs": attrs, "depth": len(stack)})

    blocks.sort(key=lambda b: (b["byte_start"], -b["byte_end"]))
    return blocks, text, byte_at


def _record(blocks, node, bstart, bend, text, byte_at, stack):
    attrs = node["attrs"]
    # char span for preview
    cstart = _char_for_byte(byte_at, bstart)
    cend = _char_for_byte(byte_at, bend)
    path = [f"{a['tag']}" + (f"#{_attr(a['attrs'],'id')}" if _attr(a['attrs'], 'id') else "")
            for a in stack]
    blocks.append({
        "tag": node["tag"],
        "depth": node["depth"],
        "id": _attr(attrs, "id"),
        "cls": _attr(attrs, "class"),
        "role": _attr(attrs, "role"),
        "data_section": _attr(attrs, "data-section") or _attr(attrs, "data-testid"),
        "src": _attr(attrs, "src"),
        "path": " › ".join(path),
        "byte_start": bstart,
        "byte_end": bend,
        "preview": _preview(text[cstart:cend]),
    })


def _char_for_byte(byte_at, target):
    lo, hi = 0, len(byte_at) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if byte_at[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _preview(chunk: str, limit: int = 140) -> str:
    """Collapse whitespace and strip tags to a short human-readable gist."""
    inner = re.sub(r"<[^>]+>", " ", chunk)
    inner = re.sub(r"\s+", " ", inner).strip()
    if len(inner) > limit:
        inner = inner[:limit].rstrip() + "…"
    return inner


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', attrs) or \
        re.search(rf"{name}\s*=\s*'([^']*)'", attrs)
    return m.group(1).strip() if m else ""


def label_for(b: dict) -> str:
    label = b["tag"]
    if b["id"]:
        label += f"#{b['id']}"
    elif b["cls"]:
        classes = b["cls"].split()
        if classes:
            label += "." + ".".join(classes[:2])  # up to 2 classes for context
    return label


def snippet_around(text: str, byte_at, cut_byte: int) -> str:
    """Return ~200 chars of HTML straddling the cut point for context."""
    # find char index closest to cut_byte
    lo, hi = 0, len(byte_at) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if byte_at[mid] < cut_byte:
            lo = mid + 1
        else:
            hi = mid
    ci = lo
    start = max(0, ci - 120)
    end = min(len(text), ci + 120)
    before = text[start:ci]
    after = text[ci:end]
    return before, after


# ---- Analysis ---------------------------------------------------------------

# Elements that actually matter to Google for indexing/ranking. If any of these
# fall below the 2MB cut they're invisible to Googlebot — far more serious than
# losing, say, cart or footer markup. Ordered roughly by severity.
SEO_CRITICAL = [
    ("JSON-LD structured data", re.compile(r'<script[^>]*type=["\']application/ld\+json["\']', re.I), "high"),
    ("Canonical link", re.compile(r'<link[^>]*rel=["\']canonical["\']', re.I), "high"),
    ("Meta robots", re.compile(r'<meta[^>]*name=["\']robots["\']', re.I), "high"),
    ("Hreflang alternate", re.compile(r'<link[^>]*hreflang=', re.I), "high"),
    ("Title tag", re.compile(r'<title[\s>]', re.I), "high"),
    ("Meta description", re.compile(r'<meta[^>]*name=["\']description["\']', re.I), "medium"),
    ("H1 heading", re.compile(r'<h1[\s>]', re.I), "medium"),
    ("Open Graph tags", re.compile(r'<meta[^>]*property=["\']og:', re.I), "medium"),
    ("Twitter Card tags", re.compile(r'<meta[^>]*name=["\']twitter:', re.I), "low"),
    ("Pagination rel", re.compile(r'<link[^>]*rel=["\'](?:next|prev)["\']', re.I), "medium"),
    ("Heading (h2–h3)", re.compile(r'<h[23][\s>]', re.I), "low"),
    ("Image alt text", re.compile(r'<img[^>]*\balt=', re.I), "low"),
    ("Internal/anchor links", re.compile(r'<a[^>]*\bhref=', re.I), "low"),
]


def _scan_seo_critical(region: str):
    """Return a list of ranking-critical elements found in the lost HTML region."""
    hits = []
    for name, rx, severity in SEO_CRITICAL:
        matches = rx.findall(region)
        if matches:
            hits.append({"what": name, "count": len(matches), "severity": severity})
    return hits


# Class/id tokens that typically mark a repeating product / listing item. If the
# cut lands inside one of these, the page is being truncated mid-product-list.
LISTING_TOKENS = re.compile(
    r'(product|item|card|grid|listing|collection|tile|result|article|post|'
    r'faq|accordion|review|feed|catalog|sku)',
    re.I,
)


def _strip_to_text(html: str) -> str:
    """Remove script/style/noscript and tags, collapse whitespace → visible text."""
    t = re.sub(r'<(script|style|noscript|template)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.S)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'&nbsp;', ' ', t)
    t = re.sub(r'&[a-z]+;', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _detect_lost_text(lost_region: str):
    """
    Detect visible, human-readable text being lost below the cut — product names,
    descriptions, FAQ answers, body copy — with or without a heading. Returns a
    flag dict, or None if no meaningful text is lost.
    """
    region = lost_region
    # The cut can land INSIDE a <script>/<style>; the lost region then begins with
    # that tag's leftover code and its closing tag. Drop that opening fragment so
    # raw JS/CSS isn't mistaken for page text.
    m = re.match(r'^(.*?)</(script|style|noscript|template)\s*>', region, flags=re.S | re.I)
    if m:
        region = region[m.end():]

    text = _strip_to_text(region)
    # Need real words (letters), not just a run of one repeated char or symbols.
    words = re.findall(r'[A-Za-z]{2,}', text)
    if len(text) < 40 or len(words) < 6:
        return None

    # Pull out the specific things that are disappearing so we can name them.
    headings = re.findall(r'<h[1-6][^>]*>(.*?)</h[1-6]>', region, flags=re.S | re.I)
    headings = [_strip_to_text(h) for h in headings]
    headings = [h for h in headings if h][:8]

    # FAQ questions (common patterns: <summary>, itemprop="name" in FAQ blocks).
    faq_qs = re.findall(r'<summary[^>]*>(.*?)</summary>', region, flags=re.S | re.I)
    faq_qs = [_strip_to_text(q) for q in faq_qs if _strip_to_text(q)][:8]

    # Rough count of paragraphs / list items lost.
    para_count = len(re.findall(r'<(?:p|li|dd|dt)\b', region, flags=re.I))

    # A readable sample of the lost copy.
    sample = text[:280] + ("…" if len(text) > 280 else "")

    return {
        "char_count": len(text),
        "heading_count": len(headings),
        "headings": headings,
        "faq_questions": faq_qs,
        "para_count": para_count,
        "sample": sample,
    }


def _el_label(tag: str, attrs: str) -> str:
    """Build a readable label like div#reviews or div.product-card for an element."""
    idv = _attr(attrs, "id")
    cls = _attr(attrs, "class")
    if idv:
        return f"{tag}#{idv}"
    if cls:
        return f"{tag}." + ".".join(cls.split()[:2])
    return tag


def _stack_at_cut(text: str, cut_char: int):
    """
    Return the list of element labels that are still OPEN at the cut point, outermost
    first — i.e. the containers the cut is sitting inside. This lets us attribute the
    first lost items to the divs they belong to even though those divs opened above
    the cut.
    """
    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")
    void = {"br", "hr", "img", "input", "meta", "link", "source", "wbr",
            "area", "base", "col", "embed", "param", "track"}
    stack = []
    for m in tag_re.finditer(text, 0, cut_char):
        closing, tag, attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag in void or selfclose:
            continue
        if closing:
            for j in range(len(stack) - 1, -1, -1):
                if stack[j][0] == tag:
                    del stack[j:]
                    break
        else:
            stack.append((tag, _el_label(tag, attrs)))
    # Only structural containers are useful for grouping.
    keep = {"div", "section", "article", "main", "aside", "ul", "ol", "table",
            "nav", "header", "footer", "form", "details"}
    return [lbl for (t, lbl) in stack if t in keep]


def build_lost_inventory(text: str, cut_char: int, max_items: int = 800):
    """
    Walk the lost region and produce a full, itemised inventory of every piece of
    human-readable content being cut, grouped by the nearest enclosing container
    element. Answers: exactly what is missing, and from which div.

    Returns a list of groups: {container, path, items:[{type, text}], counts{…}}.
    """
    region = text[cut_char:]

    # Elements whose text content we care about, with a friendly type label.
    CONTENT = [
        ("h1", "Heading (H1)"), ("h2", "Heading (H2)"), ("h3", "Heading (H3)"),
        ("h4", "Heading (H4)"), ("h5", "Heading"), ("h6", "Heading"),
        ("summary", "FAQ question"), ("figcaption", "Caption"),
        ("p", "Paragraph"), ("li", "List item"), ("dt", "Term"), ("dd", "Definition"),
        ("td", "Table cell"), ("th", "Table header"), ("blockquote", "Quote"),
        ("a", "Link"), ("button", "Button"),
    ]
    content_tags = {t for t, _ in CONTENT}
    type_of = {t: lbl for t, lbl in CONTENT}

    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")
    void = {"br", "hr", "img", "input", "meta", "link", "source", "wbr",
            "area", "base", "col", "embed", "param", "track"}
    container_tags = {"div", "section", "article", "main", "aside", "ul", "ol",
                      "table", "nav", "header", "footer", "form", "details"}

    # Seed the container stack with whatever was open at the cut.
    stack = [(None, lbl) for lbl in _stack_at_cut(text, cut_char)]

    groups = {}           # key -> group dict, preserves insertion order (py3.7+)
    order = []
    truncated = False

    def container_key():
        for _, lbl in reversed(stack):
            return lbl
        return "(top level, no container)"

    def container_path():
        return " › ".join(lbl for _, lbl in stack) or "(root)"

    def add_item(itype, itext):
        nonlocal truncated
        itext = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", itext)).strip()
        itext = re.sub(r"&nbsp;", " ", itext)
        itext = re.sub(r"&[a-z]+;", " ", itext).strip()
        if not itext or len(itext) < 2:
            return
        key = container_key()
        if key not in groups:
            g = {"container": key, "path": container_path(), "items": []}
            groups[key] = g
            order.append(key)
        total_items = sum(len(groups[k]["items"]) for k in groups)
        if total_items >= max_items:
            truncated = True
            return
        groups[key]["items"].append({"type": itype, "text": itext[:200]})

    pos = 0
    # We iterate tags; when we open a content element, grab its inner text up to the
    # matching close (best-effort, non-nested) and record it under the current group.
    i = 0
    n = len(region)
    while i < n:
        m = tag_re.search(region, i)
        if not m:
            break
        closing, tag, attrs, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)

        if tag in container_tags and not selfclose:
            if closing:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][1].split("#")[0].split(".")[0] == tag:
                        del stack[j:]
                        break
            else:
                stack.append((tag, _el_label(tag, attrs)))
            i = m.end()
            continue

        # image alt text is content too
        if tag == "img" and not closing:
            alt = _attr(attrs, "alt")
            if alt:
                add_item("Image alt", alt)
            i = m.end()
            continue

        if tag in content_tags and not closing and not selfclose:
            close_re = re.compile(rf"</{tag}\s*>", re.I)
            cm = close_re.search(region, m.end())
            inner_end = cm.start() if cm else min(n, m.end() + 400)
            inner = region[m.end():inner_end]
            add_item(type_of[tag], inner)
            i = (cm.end() if cm else inner_end)
            continue

        i = m.end()

    result = []
    for key in order:
        g = groups[key]
        if not g["items"]:
            continue
        # summarise item type counts for the group header
        counts = {}
        for it in g["items"]:
            counts[it["type"]] = counts.get(it["type"], 0) + 1
        g["counts"] = counts
        g["item_count"] = len(g["items"])
        result.append(g)
    return {"groups": result, "truncated": truncated,
            "total_items": sum(g["item_count"] for g in result)}


def _detect_midlist_cut(straddler, blocks):
    """
    Detect whether the 2MB cut falls inside (or between siblings of) a repeating
    product / listing / FAQ block — i.e. the page is truncated mid-list. Returns a
    flag dict or None.
    """
    # 1) The element the cut lands inside looks like a listing item.
    hit = None
    if straddler:
        label = (straddler.get("label") or "")
        path = (straddler.get("path") or "")
        if LISTING_TOKENS.search(label) or LISTING_TOKENS.search(path):
            hit = straddler

    # 2) Count how many repeating listing-type siblings were cut or clipped —
    #    strong signal the cut sits in the middle of a product grid.
    cut_items = [b for b in blocks
                 if b["status"] in ("cut", "partial")
                 and (LISTING_TOKENS.search(b.get("label") or "")
                      or LISTING_TOKENS.search(b.get("path") or ""))]

    if not hit and not cut_items:
        return None

    # Pick a representative label for the message.
    where = hit["label"] if hit else (cut_items[0]["label"] if cut_items else "a listing block")
    return {
        "where": where,
        "path": (hit or (cut_items[0] if cut_items else {})).get("path", ""),
        "cut_items": len(cut_items),
    }


def analyse(raw: bytes):
    total = len(raw)
    over = total > GOOGLE_LIMIT_BYTES
    cut = GOOGLE_LIMIT_BYTES if over else total

    blocks, text, byte_at = find_blocks(raw)

    results = []
    for b in blocks:
        bs, be = b["byte_start"], b["byte_end"]
        size = be - bs
        if not over or be <= cut:
            status = "kept"
            kept_pct = 100
        elif bs >= cut:
            status = "cut"
            kept_pct = 0
        else:
            status = "partial"
            raw_pct = (cut - bs) / size * 100 if size else 0
            kept_pct = max(1, min(99, round(raw_pct)))
        results.append({
            "label": label_for(b),
            "tag": b["tag"],
            "depth": b["depth"],
            "id": b["id"],
            "cls": b["cls"],
            "role": b["role"],
            "data_section": b["data_section"],
            "src": b["src"],
            "path": b["path"],
            "preview": b["preview"],
            "byte_start": bs,
            "byte_end": be,
            "size": size,
            "status": status,
            "kept_pct": kept_pct,
        })

    before, after = ("", "")
    if over:
        before, after = snippet_around(text, byte_at, cut)

    # The element the cut lands *inside* — the most useful single fact.
    straddler = next((r for r in results if r["status"] == "partial"), None)

    # What's lost below the cut, biggest first, for a focused "lost content" view.
    lost = [r for r in results if r["status"] in ("cut", "partial")]
    lost.sort(key=lambda r: r["byte_end"], reverse=True)

    # Extract the ACTUAL HTML in the lost region (from the cut byte to EOF) so the
    # analysis can judge what's really disappearing, and scan it for the tags that
    # actually matter to Google for ranking/indexing.
    lost_html = ""
    seo_hits = []
    lost_text_flag = None
    midlist_flag = None
    lost_inventory = None
    if over:
        cut_char = _char_for_byte(byte_at, cut)
        lost_region = text[cut_char:]
        lost_html = lost_region  # full lost tail; trimmed before sending to AI
        seo_hits = _scan_seo_critical(lost_region)
        lost_text_flag = _detect_lost_text(lost_region)
        midlist_flag = _detect_midlist_cut(straddler, results)
        lost_inventory = build_lost_inventory(text, cut_char)

    # Deterministic risk level — independent of the AI. Escalates on the things
    # the user cares about even when no ranking-critical <head> tags are lost:
    #   • any visible text lost below the cut  → at least "high" (content missing)
    #   • cut lands mid product/FAQ list       → at least "high"
    #   • ranking-critical tags lost (schema…)  → "critical"
    risk = "none"
    if over:
        risk = "low"  # something is cut, but so far only non-text markup
        if midlist_flag:
            risk = "high"
        if lost_text_flag:
            risk = "high"
        # a lot of lost copy, or many headings, pushes it higher
        if lost_text_flag and (lost_text_flag["char_count"] > 2000
                               or lost_text_flag["heading_count"] >= 3):
            risk = "high"
        if any(h["severity"] == "high" for h in seo_hits):
            risk = "critical"
    elif total >= GOOGLE_LIMIT_BYTES * 0.95:
        # Under the limit but within 5% — Google's cap also counts the HTTP
        # request headers it sends, so a page this close can tip over in practice.
        risk = "near"

    return {
        "total_bytes": total,
        "limit_bytes": GOOGLE_LIMIT_BYTES,
        "over_limit": over,
        "cut_byte": cut,
        "overage_bytes": max(0, total - GOOGLE_LIMIT_BYTES),
        "pct_of_limit": round(total / GOOGLE_LIMIT_BYTES * 100, 1),
        "blocks": results,
        "kept_count": sum(1 for r in results if r["status"] == "kept"),
        "partial_count": sum(1 for r in results if r["status"] == "partial"),
        "cut_count": sum(1 for r in results if r["status"] == "cut"),
        "lost_bytes": sum(r["size"] for r in results
                          if r["status"] == "cut" and r["depth"] == 0),
        "straddler": straddler,
        "cut_before": before,
        "cut_after": after,
        "seo_hits": seo_hits,
        "lost_text": lost_text_flag,
        "midlist_cut": midlist_flag,
        "lost_inventory": lost_inventory,
        "risk": risk,
        "lost_html": lost_html,
    }


# ---- AI ranking-impact analysis ---------------------------------------------

def extract_json(text: str) -> str:
    """
    Pull the JSON object out of a model reply. Models behind OpenRouter vary in
    how strictly they honour a JSON-only instruction — some wrap it in fences,
    some add a sentence of preamble — so fall back to the outermost braces.
    """
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text



def ai_bloat_review(analysis: dict, raw: bytes) -> dict:
    cfg = get_ai_config()
    if not cfg["key"]:
        return {"ok": False, "error": "No AI provider configured — set one up in Settings."}
    if not cfg["base_url"] or not cfg["model"]:
        return {"ok": False, "error": "Provider is missing a base URL or model — check Settings."}

    over = analysis.get("over_limit")

    # If nothing is cut there's nothing at risk — say so without spending a call.
    if not over:
        return {
            "ok": True,
            "sections": [],
            "seo_hits": [],
            "lost_summary": "The page is within the 2MB limit, so no HTML is lost "
                            "to Google's fetch cap. Nothing is at risk.",
            "verdict": "No ranking impact from truncation — the full document is fetched.",
            "impact": "none",
        }

    # Ground-truth: ranking-critical tags found in the lost region (deterministic).
    seo_hits = analysis.get("seo_hits", [])
    seo_txt = (
        "\n".join(f"- {h['count']}× {h['what']} (severity {h['severity']})" for h in seo_hits)
        if seo_hits else "- none detected"
    )

    # Deterministic content flags — the things the user cares about most.
    lt = analysis.get("lost_text")
    ml = analysis.get("midlist_cut")
    flags_lines = []
    if lt:
        names = ", ".join(lt.get("headings", [])[:5]) or "(no headings; body copy)"
        flags_lines.append(
            f"- VISIBLE TEXT LOST: ~{lt['char_count']:,} chars across ~{lt['para_count']} "
            f"paragraphs/list items are below the cut and will NOT be indexed. "
            f"Lost sections include: {names}."
        )
        if lt.get("faq_questions"):
            flags_lines.append("- LOST FAQ QUESTIONS: " + "; ".join(lt["faq_questions"][:5]))
    if ml:
        flags_lines.append(
            f"- CUT MID-LISTING: the cut falls inside a product/listing block "
            f"({ml['where']}); {ml['cut_items']} listing item(s) are on/below the cut, "
            f"so products/items after that point are invisible to Google."
        )
    flags_txt = "\n".join(flags_lines) if flags_lines else "- none"

    # A compact per-container tally of the itemised lost inventory for the AI.
    inv = analysis.get("lost_inventory")
    if inv and inv.get("groups"):
        inv_lines = []
        for g in inv["groups"][:15]:
            counts = ", ".join(f"{c} {t.lower()}" for t, c in g["counts"].items())
            inv_lines.append(f"- {g['container']} (in {g['path']}): {counts}")
        flags_txt += "\n\nITEMISED LOST CONTENT (by container):\n" + "\n".join(inv_lines)

    # The lost/clipped SECTIONS with their metadata.
    lost_sections = [b for b in analysis["blocks"] if b["status"] in ("cut", "partial")]
    lost_sections.sort(key=lambda b: b["size"], reverse=True)
    sec_lines = []
    for i, b in enumerate(lost_sections[:20]):
        ctx = b["path"] or "(root)"
        sec_lines.append(
            f"[{i}] {b['label']} · {b['size']:,}B · {b['status']} "
            f"({b['kept_pct']}% kept) · in: {ctx} · preview: {b['preview'] or '—'}"
        )
    sections_txt = "\n".join(sec_lines) or "(no discrete tracked sections in the lost region)"

    # Retained blocks ABOVE the cut, biggest first — these are the trimming targets.
    # Reducing these is what pulls the lost content back under the 2MB limit.
    kept = [b for b in analysis["blocks"] if b["status"] == "kept"]
    kept.sort(key=lambda b: b["size"], reverse=True)
    kept_lines = []
    for i, b in enumerate(kept[:20]):
        ctx = b["path"] or "(root)"
        kept_lines.append(
            f"[{i}] {b['label']} · {b['size']:,}B · in: {ctx} · preview: {b['preview'] or '—'}"
        )
    kept_txt = "\n".join(kept_lines) or "(no large retained blocks tracked)"

    # The ACTUAL HTML being lost — this is the key change. Trim to a safe budget
    # (~24k chars ≈ well within context) taken from the start of the lost region,
    # since that's the part nearest the cut and most likely to be salvageable.
    lost_html = analysis.get("lost_html", "")
    LOST_BUDGET = 24000
    lost_sample = lost_html[:LOST_BUDGET]
    truncated_note = (
        f"\n\n[…lost region continues for {len(lost_html) - LOST_BUDGET:,} more chars…]"
        if len(lost_html) > LOST_BUDGET else ""
    )

    prompt = (
        "You are a senior technical-SEO auditor. Google truncates fetched HTML at "
        f"{analysis['limit_bytes']:,} bytes. This page is {analysis['total_bytes']:,} "
        f"bytes — OVER by {analysis['overage_bytes']:,} bytes, so everything past the "
        "2MB cut is INVISIBLE to Googlebot.\n\n"
        "Your job is to judge how badly this truncation hurts the page's SEO, based on "
        "WHAT is actually being lost. Weight by ranking importance, not byte size: "
        "losing structured data (JSON-LD), canonical, meta robots, hreflang, title, "
        "headings or primary body content is serious; losing cart/checkout scripts, "
        "third-party widgets, analytics, cookie banners, chat widgets or footer "
        "boilerplate is largely irrelevant to Google because it isn't used for ranking. "
        "IMPORTANT: any visible page TEXT (product names/descriptions, FAQ answers, body "
        "copy) being lost, or the cut landing mid product/FAQ listing, is a serious "
        "problem even if no <head> tags are lost — that content simply won't be indexed.\n\n"
        "DETERMINISTIC CONTENT FLAGS (treat as ground truth; reflect these in your impact "
        "rating and verdict):\n"
        f"{flags_txt}\n\n"
        "DETERMINISTIC SCAN — ranking-critical tags detected in the lost region "
        "(treat as ground truth, always account for these):\n"
        f"{seo_txt}\n\n"
        "LOST / CLIPPED SECTIONS (metadata):\n"
        f"{sections_txt}\n\n"
        "LARGEST RETAINED BLOCKS ABOVE THE CUT (candidates to trim so the lost "
        "content fits back under 2MB):\n"
        f"{kept_txt}\n\n"
        "ACTUAL HTML FROM THE LOST REGION (starts at the 2MB cut):\n"
        f"```html\n{lost_sample}{truncated_note}\n```\n\n"
        "Analyse the actual lost HTML above. For each meaningful lost/clipped section "
        "give:\n"
        "- label: a short identifier for it (tag/id/class or what it is)\n"
        "- purpose: what it actually is, in plain English\n"
        "- vitality: how vital it is to GOOGLE for indexing/ranking — one of "
        "'critical' | 'important' | 'minor' | 'irrelevant'\n"
        "- reason: why, referencing what you saw in the HTML\n"
        "- fix: how to protect it (e.g. move above the cut, move to <head>, reduce "
        "earlier bloat so it fits) — or 'no action needed' if irrelevant to ranking\n\n"
        "Then, using the RETAINED BLOCKS list, recommend concrete reductions to get the "
        f"page under 2MB (it needs to shed at least {analysis['overage_bytes']:,} bytes). "
        "For each 'reductions' item give:\n"
        "- target: the block/label to trim\n"
        "- action: the specific tactic (e.g. 'externalise inline CSS to a cached "
        "stylesheet', 'minify inline JSON', 'lazy-load the reviews carousel', 'remove "
        "duplicated inline SVG icons'), noting anything safe to remove entirely\n"
        "- saving: rough byte saving (e.g. '~120KB')\n"
        "- risk: 'safe' | 'test-first' — whether it could affect rendering/behaviour\n"
        "Order reductions by biggest saving first, and only include ones that are "
        "genuinely bloat or safely externalisable — never recommend cutting the "
        "ranking-critical content itself.\n\n"
        "Then give:\n"
        "- lost_summary: plain-English summary of what's disappearing and whether it "
        "matters for SEO\n"
        "- impact: overall ranking-impact severity — one of 'critical' | 'high' | "
        "'moderate' | 'low' | 'none'\n"
        "- verdict: a direct verdict on how badly this could affect the page's "
        "performance in Google, and the single highest-priority fix. Be concrete about "
        "consequences (e.g. 'structured data won't be parsed, so rich results are lost' "
        "vs 'only cart JS is lost, which Google ignores — negligible ranking impact').\n\n"
        "Order sections most-vital first. Respond ONLY as JSON, no markdown fences:\n"
        '{"sections":[{"label":"...","purpose":"...","vitality":"critical",'
        '"reason":"...","fix":"..."}],'
        '"reductions":[{"target":"...","action":"...","saving":"~120KB","risk":"safe"}],'
        '"lost_summary":"...","impact":"high","verdict":"..."}'
    )

    text = ""
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"content-type": "application/json", **auth_headers(cfg)},
            json={
                "model": cfg["model"],
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if "choices" not in data:
            # OpenAI-compatible gateways report upstream failures in the body.
            detail = (data.get("error") or {}).get("message") or json.dumps(data)[:200]
            return {"ok": False, "error": f"{provider_label(cfg['provider'])}: {detail}"}
        text = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(extract_json(text))
        parsed["seo_hits"] = seo_hits  # attach ground-truth scan for the UI
        parsed["model_used"] = cfg["model"]
        return {"ok": True, **parsed}
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"{cfg['model']} did not return usable JSON. Try a stronger model in Settings.",
            "raw": text[:500],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---- Rendered preview (what Google sees up to the cut) -----------------------

def build_truncated_preview(raw: bytes, source_url: str = "") -> str:
    """
    Return an HTML document representing the page as fetched up to the 2MB cut:
    truncate at the byte limit, close any tags left open at the cut, and inject a
    <base> so external CSS/images resolve. This is what Googlebot would see — the
    content below the cut is simply absent. Scripts are neutralised because the
    iframe is sandboxed without allow-scripts, so this is a static structural view.
    """
    total = len(raw)
    cut = min(GOOGLE_LIMIT_BYTES, total)
    text = raw.decode("utf-8", errors="replace")

    # char index at the cut byte
    byte_at = [0] * (len(text) + 1)
    acc = 0
    for i, ch in enumerate(text):
        acc += len(ch.encode("utf-8"))
        byte_at[i + 1] = acc
    ci = _char_for_byte(byte_at, cut)
    truncated = text[:ci]

    # If the cut fell in the middle of a tag, drop the partial trailing '<...'
    last_lt = truncated.rfind("<")
    last_gt = truncated.rfind(">")
    if last_lt > last_gt:
        truncated = truncated[:last_lt]

    # Figure out which elements are still open at the cut, then close them.
    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")
    void = {"br", "hr", "img", "input", "meta", "link", "source", "wbr",
            "area", "base", "col", "embed", "param", "track"}
    stack = []
    for m in tag_re.finditer(truncated):
        closing, tag, _, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag in void or selfclose:
            continue
        if closing:
            for j in range(len(stack) - 1, -1, -1):
                if stack[j] == tag:
                    del stack[j:]  # close nested unclosed tags too
                    break
        else:
            stack.append(tag)

    # Close still-open tags in reverse order (skip html/head/body — handled below).
    closers = "".join(f"</{t}>" for t in reversed(stack) if t not in ("html", "head", "body"))
    truncated += closers

    # Inject <base> so relative URLs (CSS, images) resolve against the origin.
    base_tag = ""
    if source_url.startswith(("http://", "https://")):
        parts = urlparse(source_url)
        base_href = f"{parts.scheme}://{parts.netloc}/"
        base_tag = f'<base href="{base_href}">'

    # A small banner marking the cut, plus base tag, inserted right after <head>.
    inject = (
        base_tag +
        '<style>#__cutmark{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
        'background:#DA1A50;color:#fff;font:600 12px/1.4 system-ui,sans-serif;'
        'padding:6px 12px;text-align:center;letter-spacing:.02em;}</style>'
    )
    if re.search(r"<head[^>]*>", truncated, re.I):
        truncated = re.sub(r"(<head[^>]*>)", r"\1" + inject, truncated, count=1, flags=re.I)
    else:
        truncated = inject + truncated

    cutmark = ('<div id="__cutmark">\u25b2 2MB limit reached here \u2014 everything below '
               'this point is not fetched by Google</div>')
    if re.search(r"</body>", truncated, re.I):
        truncated = re.sub(r"</body>", cutmark + "</body>", truncated, count=1, flags=re.I)
    else:
        truncated += cutmark

    return truncated


# ---- Routes -----------------------------------------------------------------

def config_status() -> dict:
    """Public view of the AI settings — the key itself is never included."""
    cfg = get_ai_config()
    return {
        "ok": True,
        "configured": bool(cfg["key"]),
        "masked": mask_key(cfg["key"]) if cfg["key"] else "",
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
    }


@app.route("/")
def index():
    status = config_status()
    return render_template(
        "index.html",
        has_key=status["configured"],
        status=status,
        providers=PROVIDERS,
    )


@app.route("/api/ai-config", methods=["GET", "POST", "DELETE"])
def ai_config_route():
    """Read, save, or clear the AI provider settings. Never returns the key."""
    if request.method == "GET":
        return jsonify(config_status())

    refused = local_request_only()
    if refused:
        return refused

    if request.method == "DELETE":
        write_env_values({ENV_PROVIDER: None, ENV_KEY: None, ENV_MODEL: None, ENV_BASE: None})
        os.environ.pop("OPENAI_API_KEY", None)
        return jsonify(config_status())

    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        return jsonify({"ok": False, "error": "Unknown provider."}), 400

    current = get_ai_config()
    # Blank key means "keep the saved one" so users can change model alone.
    key = (payload.get("key") or "").strip() or (current["key"] if current["provider"] == provider else "")
    if not key:
        return jsonify({"ok": False, "error": "Paste an API key first."}), 400

    base_url = (payload.get("base_url") or "").strip().rstrip("/") or PROVIDERS[provider]["base_url"]
    if not base_url:
        return jsonify({"ok": False, "error": "Enter the API base URL for this provider."}), 400
    if not base_url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Base URL must start with http:// or https://."}), 400

    candidate = {"provider": provider, "key": key, "base_url": base_url, "model": ""}
    valid, error = verify_key(candidate)
    if not valid:
        return jsonify({"ok": False, "error": error}), 400
    listed, models, error = fetch_models(candidate)
    if not listed:
        return jsonify({"ok": False, "error": error}), 400

    model = (payload.get("model") or "").strip() or PROVIDERS[provider]["default_model"]
    if not model:
        return jsonify({"ok": False, "error": "Choose a model.", "models": models}), 400
    if models and model not in models:
        return jsonify({
            "ok": False,
            "error": f"'{model}' isn't available on this key.",
            "models": models,
        }), 400

    write_env_values({
        ENV_PROVIDER: provider,
        ENV_KEY: key,
        ENV_MODEL: model,
        # Only pin a base URL when it differs from the preset, so preset
        # changes aren't frozen in place by a stale .env entry.
        ENV_BASE: base_url if base_url != PROVIDERS[provider]["base_url"] else None,
    })
    # A shell OPENAI_API_KEY would otherwise shadow the choice just saved.
    os.environ.pop("OPENAI_API_KEY", None)
    return jsonify({**config_status(), "models": models})


@app.route("/api/ai-models", methods=["POST"])
def ai_models_route():
    """List models available to a key, for the Settings model picker."""
    refused = local_request_only()
    if refused:
        return refused

    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        return jsonify({"ok": False, "error": "Unknown provider."}), 400

    current = get_ai_config()
    key = (payload.get("key") or "").strip() or (current["key"] if current["provider"] == provider else "")
    base_url = (payload.get("base_url") or "").strip().rstrip("/") or PROVIDERS[provider]["base_url"]
    if not key or not base_url:
        return jsonify({"ok": False, "error": "Need a key and base URL to list models."}), 400

    candidate = {"provider": provider, "key": key, "base_url": base_url, "model": ""}
    valid, error = verify_key(candidate)
    if not valid:
        return jsonify({"ok": False, "error": error}), 400
    listed, models, error = fetch_models(candidate)
    if not listed:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "models": models, "default": PROVIDERS[provider]["default_model"]})


@app.route("/analyse", methods=["POST"])
def analyse_route():
    payload = request.get_json(force=True)
    source = (payload or {}).get("source", "")
    if not source.strip():
        return jsonify({"error": "Provide a URL or paste HTML."}), 400
    try:
        label, raw = fetch_html(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not fetch: {e}"}), 400
    result = analyse(raw)
    result["source_label"] = label
    return jsonify(result)


@app.route("/ai-review", methods=["POST"])
def ai_review_route():
    payload = request.get_json(force=True)
    source = (payload or {}).get("source", "")
    try:
        _, raw = fetch_html(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Could not fetch: {e}"}), 400
    analysis = analyse(raw)
    return jsonify(ai_bloat_review(analysis, raw))


CAPTURE_WIDTH = 1280        # viewport width the page is rendered at
CAPTURE_MAX_HEIGHT = 14000  # a 2MB page can render tens of thousands of px tall
REPORT_IMAGE_WIDTH = 1000   # downscaled before embedding to keep the report portable


def capture_render(raw: bytes, source_url: str) -> tuple[str, int, str]:
    """
    Screenshot the page as Googlebot sees it — truncated at the 2MB cut.

    Returns (data_uri, height_px, note). An empty data_uri means capture failed;
    the report is still worth producing without it, so callers carry on.

    JavaScript stays off: the 2MB cap applies to the raw HTML fetch, so a static
    render is the honest picture, and it avoids running third-party scripts here.
    """
    from playwright.sync_api import sync_playwright

    html = build_truncated_preview(raw, source_url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={"width": CAPTURE_WIDTH, "height": 900},
                java_script_enabled=False,
            )
            page = context.new_page()
            page.set_content(html, wait_until="load", timeout=45000)
            try:
                # Give late CSS/images from the live origin a chance to land.
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            full_height = page.evaluate(
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            ) or 900
            # Growing the viewport to the capped height bounds what Chromium has to
            # rasterise; full_page on a multi-page-tall document can balloon memory.
            height = max(1, min(full_height, CAPTURE_MAX_HEIGHT))
            page.set_viewport_size({"width": CAPTURE_WIDTH, "height": height})
            png = page.screenshot(full_page=False)
            browser.close()
    except Exception as e:  # noqa: BLE001
        return "", 0, f"Could not capture the render: {e}"

    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width > REPORT_IMAGE_WIDTH:
        ratio = REPORT_IMAGE_WIDTH / img.width
        img = img.resize((REPORT_IMAGE_WIDTH, round(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    data_uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    note = ""
    if full_height > CAPTURE_MAX_HEIGHT:
        note = (f"Render is {full_height:,}px tall; showing the top "
                f"{CAPTURE_MAX_HEIGHT:,}px.")
    return data_uri, full_height, note


def build_report_html(analysis: dict, ai: dict | None, source_label: str,
                      render: dict | None = None) -> str:
    """Render the standalone report — self-contained so it can be saved or printed."""
    # A truncated page can have tens of thousands of top-level elements, so the
    # table has to be capped. What's clipped or lost is the point of the report,
    # so lead with those and fall back to the largest survivors.
    order = {"partial": 0, "cut": 1, "kept": 2}
    blocks = sorted(
        analysis["blocks"],
        key=lambda b: (order.get(b["status"], 3), -b["size"]),
    )
    shown = blocks[:40]
    return render_template(
        "report.html",
        a=analysis,
        ai=ai,
        render=render,
        blocks=shown,
        blocks_truncated=len(blocks) > len(shown),
        source_label=source_label,
        generated_at=datetime.now().strftime("%d %b %Y, %H:%M"),
        kb=lambda n: f"{n / 1024:,.1f} KB",
        mb=lambda n: f"{n / 1024 / 1024:,.2f} MB",
    )


@app.route("/report", methods=["POST"])
def report_route():
    """
    Run the whole pipeline for one URL and stream progress as NDJSON, so the
    UI can report what's finished rather than showing an untimed spinner.
    """
    payload = request.get_json(force=True)
    source = (payload or {}).get("source", "").strip()
    if not source:
        return jsonify({"error": "Provide a URL or paste HTML."}), 400

    cfg = get_ai_config()

    def emit(step, status, detail=""):
        return json.dumps({"step": step, "status": status, "detail": detail}) + "\n"

    def run():
        yield emit("fetch", "active", "Fetching the page as Googlebot...")
        try:
            label, raw = fetch_html(source)
        except Exception as e:  # noqa: BLE001
            yield emit("fetch", "error", f"Could not fetch: {e}")
            return
        total = len(raw)
        yield emit("fetch", "done", f"{total / 1024 / 1024:.2f} MB fetched from {label}")

        yield emit("analyse", "active", "Mapping elements against the 2MB cut...")
        analysis = analyse(raw)
        analysis["source_label"] = label
        over = analysis["over_limit"]
        yield emit("analyse", "done",
                   f"{analysis['kept_count']} kept, {analysis['partial_count']} clipped, "
                   f"{analysis['cut_count']} lost"
                   + ("" if over else " — page is within the limit"))

        ai = None
        if not cfg["key"]:
            yield emit("ai", "skipped", "No AI provider configured — measured results only.")
        elif not over:
            yield emit("ai", "skipped", "Page is within the limit, so there is nothing to analyse.")
        else:
            yield emit("ai", "active", f"Asking {cfg['model']} what the loss costs...")
            ai = ai_bloat_review(analysis, raw)
            if ai.get("ok"):
                yield emit("ai", "done",
                           f"Impact rated {ai.get('impact', 'unknown')} "
                           f"across {len(ai.get('sections', []))} lost section(s)")
            else:
                yield emit("ai", "error", ai.get("error", "Analysis failed."))

        yield emit("capture", "active", "Screenshotting the page as Googlebot sees it...")
        src_url = source if source.lower().startswith(("http://", "https://")) else ""
        data_uri, height, note = capture_render(raw, src_url)
        render = {"image": data_uri, "height": height, "note": note} if data_uri else None
        if data_uri:
            kb = len(data_uri) * 3 // 4 // 1024
            yield emit("capture", "done",
                       f"Captured {height:,}px tall ({kb:,} KB)" + (f" — {note}" if note else ""))
        else:
            yield emit("capture", "error", note)

        yield emit("render", "active", "Building the report...")
        # The report identifies the exact page, not just the site — several pages
        # of one domain would otherwise be indistinguishable.
        html = build_report_html(analysis, ai, src_url or label, render)
        yield json.dumps({
            "step": "complete",
            "status": "done",
            "detail": "Report ready.",
            "html": html,
            "filename": report_filename(src_url or label),
        }) + "\n"

    return Response(stream_with_context(run()), mimetype="application/x-ndjson")


def report_filename(source: str) -> str:
    trimmed = re.sub(r"^https?://", "", source).rstrip("/")
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", trimmed).strip("-").lower() or "report"
    return f"2mb-report-{safe[:80]}-{datetime.now():%Y%m%d}"


@app.route("/preview", methods=["POST"])
def preview_route():
    """Return the page truncated at the 2MB cut, ready to render in an iframe."""
    payload = request.get_json(force=True)
    source = (payload or {}).get("source", "")
    if not source.strip():
        return jsonify({"error": "Provide a URL or paste HTML."}), 400
    try:
        _, raw = fetch_html(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not fetch: {e}"}), 400
    src_url = source.strip() if source.strip().lower().startswith(("http://", "https://")) else ""
    html = build_truncated_preview(raw, src_url)
    total = len(raw)
    return jsonify({
        "html": html,
        "over_limit": total > GOOGLE_LIMIT_BYTES,
        "total_bytes": total,
        "shown_bytes": min(GOOGLE_LIMIT_BYTES, total),
    })


@app.route("/source", methods=["POST"])
def source_route():
    """
    Return the raw HTML split at the 2MB cut so the UI can show full vs snipped
    side by side. The lost tail can be huge, so it's capped with a note; the kept
    portion (what Google keeps) is always returned in full.
    """
    payload = request.get_json(force=True)
    source = (payload or {}).get("source", "")
    if not source.strip():
        return jsonify({"error": "Provide a URL or paste HTML."}), 400
    try:
        _, raw = fetch_html(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not fetch: {e}"}), 400

    total = len(raw)
    over = total > GOOGLE_LIMIT_BYTES
    cut = GOOGLE_LIMIT_BYTES if over else total

    text = raw.decode("utf-8", errors="replace")
    byte_at = [0] * (len(text) + 1)
    acc = 0
    for i, ch in enumerate(text):
        acc += len(ch.encode("utf-8"))
        byte_at[i + 1] = acc
    ci = _char_for_byte(byte_at, cut)

    kept = text[:ci]
    lost = text[ci:]

    # Cap what we ship so a very large page doesn't produce a massive payload.
    KEEP_CAP = 600_000   # chars of kept HTML (well under the 2MB char count)
    LOST_CAP = 400_000   # chars of lost HTML
    kept_trunc = len(kept) > KEEP_CAP
    lost_trunc = len(lost) > LOST_CAP
    kept_out = kept[-KEEP_CAP:] if kept_trunc else kept  # tail of kept = nearest the cut
    lost_out = lost[:LOST_CAP] if lost_trunc else lost    # head of lost = nearest the cut

    return jsonify({
        "over_limit": over,
        "total_bytes": total,
        "cut_byte": cut,
        "kept_html": kept_out,
        "lost_html": lost_out,
        "kept_truncated": kept_trunc,     # we trimmed the *start* of kept
        "lost_truncated": lost_trunc,     # we trimmed the *end* of lost
        "kept_shown_chars": len(kept_out),
        "lost_total_chars": len(lost),
    })


if __name__ == "__main__":
    print(f"\n  Google 2MB Limit Checker -> http://localhost:{PORT}")
    _cfg = get_ai_config()
    _ai = f"{provider_label(_cfg['provider'])} / {_cfg['model']}" if _cfg["key"] else "set up a provider in the app under Settings"
    print(f"  AI review: {_ai}\n")
    app.run(port=PORT, debug=False)
