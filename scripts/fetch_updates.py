"""
Daily changelog fetcher for foundry-change-log-web.
Scrapes each source, extracts entries newer than what's already in updates.json,
and appends them (sorted newest-first).

Sources:
  - Azure AI Foundry (learn.microsoft.com)
  - Azure OpenAI   (learn.microsoft.com)
  - Azure ML       (learn.microsoft.com)
  - GitHub Copilot CLI (raw.githubusercontent.com)
  - Anthropic Claude release notes (docs.anthropic.com)

Run locally:  python scripts/fetch_updates.py
In CI:        called by .github/workflows/update-changelog.yml
"""

import json, re, sys, time
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT         = Path(__file__).resolve().parent.parent
UPDATES_FILE = ROOT / "updates.json"
SOURCES_FILE = ROOT / "sources.json"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; changelog-bot/1.0; "
        "+https://github.com/mark-quickel/foundry-change-log-web)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})


# ─── Helpers ────────────────────────────────────────────────────────────────

def load_updates() -> list:
    return json.loads(UPDATES_FILE.read_text(encoding="utf-8"))


def save_updates(updates: list):
    UPDATES_FILE.write_text(
        json.dumps(updates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def latest_date(updates: list, category: str) -> str:
    """Return the most recent date already recorded for a category."""
    dates = [u["date"] for u in updates if u.get("category") == category and u.get("date")]
    return max(dates) if dates else "2026-02-09"


def compute_week(date_str: str, anchor: date) -> int:
    """Return 1-based week number relative to the anchor (earliest existing entry)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (d - anchor).days // 7 + 1
    except ValueError:
        return 1


def anchor_date(updates: list) -> date:
    """Return the earliest date in the existing updates list as the week-1 anchor."""
    dates = [u["date"] for u in updates if u.get("date")]
    if dates:
        return datetime.strptime(min(dates), "%Y-%m-%d").date()
    return date.today()


def fetch_html(url: str):
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        print(f"  ⚠  Failed to fetch {url}: {exc}")
        return None


def parse_date(text: str) -> str | None:
    """Try to parse a date string like 'April 30, 2026', 'Apr 2026', or '2026-04-30'."""
    text = text.strip()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-01") if "Y" not in fmt[:2] else dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def is_duplicate(updates: list, category: str, title: str) -> bool:
    title_lo = title.lower().strip()
    return any(
        u.get("category") == category and u.get("title", "").lower().strip() == title_lo
        for u in updates
    )


def make_entry(category, date_str, title, summary, impact, url, link_label="Read more", *, week_anchor: date) -> dict:
    return {
        "week": compute_week(date_str, week_anchor),
        "category": category,
        "date": date_str,
        "title": title,
        "summary": summary,
        "impact": impact,
        "links": [{"label": link_label, "url": url}],
    }


# ─── Microsoft Learn parser (Foundry, OpenAI, Azure ML) ────────────────────

def _extract_learn_link(elem, base="https://learn.microsoft.com") -> str:
    """Walk siblings until next heading, return first absolute link found."""
    for sib in elem.find_next_siblings():
        if sib.name in ("h1", "h2", "h3", "h4", "h5"):
            break
        a = sib.find("a", href=True)
        if a:
            href = a["href"]
            if href.startswith("/"):
                return base + href
            if href.startswith("http"):
                return href
    return ""


def _extract_sibling_text(elem, max_chars=350) -> str:
    """Collect text from following siblings until the next heading."""
    parts = []
    for sib in elem.find_next_siblings():
        if sib.name in ("h1", "h2", "h3", "h4", "h5"):
            break
        t = sib.get_text(" ", strip=True)
        if t:
            parts.append(t)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return " ".join(parts)[:max_chars].strip()


def fetch_microsoft_learn(url: str, category: str, since: str, updates: list, week_anchor: date) -> list:
    """
    Generic parser for Microsoft Learn 'What's New' pages.
    Looks for date headings (h2/h3) and feature headings beneath them.
    """
    soup = fetch_html(url)
    if not soup:
        return []

    article = soup.find("article") or soup.find("main") or soup
    new_entries = []
    current_date = None

    CATEGORY_LABELS = {
        "foundry": "Azure AI Foundry",
        "openai":  "Azure OpenAI",
        "azureml": "Azure Machine Learning",
    }
    cat_label = CATEGORY_LABELS.get(category, category)

    for elem in article.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = elem.get_text(" ", strip=True)
        if not text:
            continue

        parsed = parse_date(text)
        if parsed:
            current_date = parsed
            continue

        if not current_date or current_date <= since:
            continue

        # It's a feature heading under a known, new date
        title = text
        if len(title) < 4 or is_duplicate(updates, category, title):
            continue

        summary = _extract_sibling_text(elem)
        if not summary:
            summary = f"New {cat_label} update: {title}"

        link_url = _extract_learn_link(elem) or url

        impact = (
            f"Review this {cat_label} change to determine if it affects your "
            "deployments, APIs, or dependent workflows."
        )

        new_entries.append(make_entry(
            category=category,
            date_str=current_date,
            title=title,
            summary=summary,
            impact=impact,
            url=link_url,
            link_label="View on Microsoft Learn",
            week_anchor=week_anchor,
        ))

    return new_entries


# ─── GitHub Copilot CLI (raw markdown) ─────────────────────────────────────

def fetch_github_changelog(updates: list, week_anchor: date) -> list:
    url   = "https://raw.githubusercontent.com/github/copilot-cli/main/changelog.md"
    since = latest_date(updates, "github")

    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        content = r.text
    except Exception as exc:
        print(f"  ⚠  Failed to fetch GitHub changelog: {exc}")
        return []

    new_entries = []
    # Sections start with "## " — split on that
    sections = re.split(r"(?m)^##\s+", content)

    for section in sections:
        if not section.strip():
            continue
        lines  = section.split("\n")
        header = lines[0].strip()

        date_m   = re.search(r"(\d{4}-\d{2}-\d{2})", header)
        ver_m    = re.search(r"\[([^\]]+)\]", header)
        if not date_m:
            continue

        date_str = date_m.group(1)
        version  = ver_m.group(1) if ver_m else re.split(r"\s*-\s*", header)[0].strip()

        if date_str <= since:
            continue

        title = f"GitHub Copilot CLI {version}"
        if is_duplicate(updates, "github", title):
            continue

        body     = "\n".join(lines[1:]).strip()
        features = re.findall(r"^[-*]\s+(.+)$", body, re.MULTILINE)
        summary  = "; ".join(features[:4]) if features else (body[:300] or f"Copilot CLI {version} released")
        summary  = summary[:350]

        new_entries.append(make_entry(
            category="github",
            date_str=date_str,
            title=title,
            summary=summary,
            impact="Update your Copilot CLI to get the latest features and bug fixes.",
            url="https://github.com/github/copilot-cli/blob/main/changelog.md",
            link_label="Changelog",
            week_anchor=week_anchor,
        ))

    return new_entries


# ─── Anthropic Claude release notes ────────────────────────────────────────

def fetch_anthropic_notes(updates: list, week_anchor: date) -> list:
    url   = "https://docs.anthropic.com/en/release-notes/overview"
    since = latest_date(updates, "claude")

    soup = fetch_html(url)
    if not soup:
        return []

    new_entries = []
    content = soup.find("main") or soup.find("article") or soup
    current_date = None

    for elem in content.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = elem.get_text(" ", strip=True)
        if not text:
            continue

        parsed = parse_date(text)
        if parsed:
            current_date = parsed
            continue

        if not current_date or current_date <= since or len(text) < 4:
            continue

        title = text
        if is_duplicate(updates, "claude", title):
            continue

        summary = _extract_sibling_text(elem)
        if not summary:
            summary = f"Anthropic update: {title}"

        a = elem.find("a", href=True)
        link_url = url
        if a:
            href = a["href"]
            link_url = ("https://docs.anthropic.com" + href) if href.startswith("/") else href

        new_entries.append(make_entry(
            category="claude",
            date_str=current_date,
            title=title,
            summary=summary,
            impact="Evaluate how this Claude API change affects your model integrations and prompting strategies.",
            url=link_url,
            link_label="Anthropic Release Notes",
            week_anchor=week_anchor,
        ))

    return new_entries


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("📰  Fetching changelog updates…")
    updates = load_updates()
    print(f"    Loaded {len(updates)} existing entries\n")

    week_anchor = anchor_date(updates)
    print(f"    Week anchor (week 1 = {week_anchor})\n")

    all_new = []

    sources = [
        ("foundry",  "🔷 Azure AI Foundry",
         "https://learn.microsoft.com/en-us/azure/ai-foundry/whats-new-foundry"),
        ("openai",   "🔵 Azure OpenAI",
         "https://learn.microsoft.com/en-us/azure/ai-services/openai/whats-new"),
        ("azureml",  "🔴 Azure ML",
         "https://learn.microsoft.com/en-us/azure/machine-learning/whats-new"),
    ]

    for category, label, url in sources:
        print(f"{label}…")
        since = latest_date(updates, category)
        print(f"    Since: {since}")
        new = fetch_microsoft_learn(url, category, since, updates, week_anchor)
        print(f"    Found {len(new)} new entries")
        all_new.extend(new)
        time.sleep(1)   # be polite between requests

    print("\n🟣 GitHub Copilot CLI…")
    new = fetch_github_changelog(updates, week_anchor)
    print(f"    Found {len(new)} new entries")
    all_new.extend(new)

    time.sleep(1)

    print("\n🟠 Anthropic Claude…")
    new = fetch_anthropic_notes(updates, week_anchor)
    print(f"    Found {len(new)} new entries")
    all_new.extend(new)

    if all_new:
        combined = all_new + updates
        combined.sort(key=lambda u: u.get("date") or "", reverse=True)
        save_updates(combined)
        print(f"\n✅  Added {len(all_new)} new entries. Total: {len(combined)}")
    else:
        print("\n✅  No new entries found — updates.json unchanged.")

    # Stamp lastFetched in sources.json
    sources_data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    today = date.today().isoformat()
    for s in sources_data:
        s["lastFetched"] = today
    SOURCES_FILE.write_text(
        json.dumps(sources_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"    Updated sources.json lastFetched → {today}")


if __name__ == "__main__":
    main()
