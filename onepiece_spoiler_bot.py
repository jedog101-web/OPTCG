#!/usr/bin/env python3
"""
One Piece TCG Spoiler Bot – onepiecetopdecks.com
Posts new card leaks to a Discord webhook.

Usage:
    python onepiece_spoiler_bot.py

Schedule with cron (every 30 minutes):
    */30 * * * * /usr/bin/python3 /path/to/onepiece_spoiler_bot.py
"""

import json
import os
import re
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
SPOILERS_URL    = "https://onepiecetopdecks.com/leaks-op-16-the-time-of-battle/"
WEBHOOK_URL     = "https://discord.com/api/webhooks/1506418453970686043/LfHePqeUkDqcrRXQ6C744UsfsI6s_9V71-7jDaXuxR9U22gpphiP9i4QSnDKqiQTOuHn"
SEEN_CARDS_FILE = "seen_cards.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Colour map by card colour keyword in the card text
COLOUR_MAP = {
    "Red":    0xE74C3C,
    "Blue":   0x3498DB,
    "Green":  0x2ECC71,
    "Purple": 0x9B59B6,
    "Yellow": 0xF1C40F,
    "Black":  0x2C3E50,
}
DEFAULT_COLOUR = 0xE8B343  # gold fallback


# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_CARDS_FILE):
        with open(SEEN_CARDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_CARDS_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)


def card_uid(card: dict) -> str:
    raw = f"{card['card_number']}|{card['image_url']}"
    return hashlib.md5(raw.encode()).hexdigest()


# ── Scraper ───────────────────────────────────────────────────────────────────

def fetch_cards() -> list[dict]:
    """
    Parse onepiecetopdecks.com spoiler page.

    The page structure:
      <strong>Card Name (OP16-XXX)</strong>
      ... text describing cost/power/types/effects ...
      <img src="https://onepiecetopdecks.com/wp-content/uploads/...">

    We walk every <img> whose src points to wp-content/uploads and look
    backward in the DOM for the nearest <strong> block to get the card name.
    """
    try:
        resp = requests.get(SPOILERS_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Grab the main content area so we skip header/nav images
    content = soup.find("div", class_=re.compile(r"entry-content|post-content|elementor"))
    if not content:
        content = soup.body

    cards = []
    all_tags = list(content.descendants)

    for i, tag in enumerate(all_tags):
        # Find real card images (wp-content uploads, skip base64 gif placeholders)
        if getattr(tag, "name", None) != "img":
            continue
        src = tag.get("src") or tag.get("data-src") or ""
        if "wp-content/uploads" not in src or src.startswith("data:"):
            continue
        if not any(ext in src for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            continue

        # Skip section-header / parallel-art images (no card number in nearby text)
        # Walk backwards up to 15 tags to find a <strong> with card info
        name_text = ""
        card_number = ""
        card_details = ""

        for j in range(i - 1, max(i - 40, -1), -1):
            prev = all_tags[j]
            if getattr(prev, "name", None) == "strong":
                candidate = prev.get_text(strip=True)
                # Must contain an OP/P card number pattern OR be a bold title
                if re.search(r"OP\d{2}-\d{3}|P-\d{3}", candidate, re.IGNORECASE):
                    name_text = candidate
                    m = re.search(r"(OP\d{2}-\d{3}|P-\d{3})", candidate, re.IGNORECASE)
                    card_number = m.group(1).upper() if m else ""
                    break
                elif len(candidate) > 3:
                    name_text = candidate  # title without inline number

        if not name_text:
            continue  # skip images without an identifiable card name nearby

        # Grab the text block between the <strong> and this image for card details
        detail_parts = []
        collecting = False
        for j, t in enumerate(all_tags):
            if t is tag:
                break
            text = t.get_text(strip=True) if hasattr(t, "get_text") else str(t).strip()
            if name_text and name_text in text:
                collecting = True
                continue
            if collecting and text and len(text) > 2:
                detail_parts.append(text)
        card_details = " ".join(detail_parts[:8])  # first few text nodes after name

        # Strip the card number from the display name
        display_name = re.sub(r"\s*\(?(OP\d{2}-\d{3}|P-\d{3})\)?", "", name_text, flags=re.IGNORECASE).strip()
        display_name = display_name.strip(" –-")

        # Detect card colour from description text
        colour = DEFAULT_COLOUR
        for colour_name, colour_hex in COLOUR_MAP.items():
            if colour_name.lower() in card_details.lower() or colour_name.lower() in name_text.lower():
                colour = colour_hex
                break

        cards.append({
            "display_name": display_name or name_text,
            "card_number":  card_number,
            "image_url":    src,
            "details":      card_details[:300],
            "colour":       colour,
            "page_url":     SPOILERS_URL,
        })

    # Deduplicate by image URL (same card can appear multiple times)
    seen_urls: set[str] = set()
    unique = []
    for c in cards:
        if c["image_url"] not in seen_urls:
            seen_urls.add(c["image_url"])
            unique.append(c)

    print(f"[INFO] Parsed {len(unique)} unique cards from page.")
    return unique


# ── Discord ───────────────────────────────────────────────────────────────────

def post_card(card: dict) -> bool:
    title = card["display_name"]
    if card["card_number"]:
        title = f"{card['card_number']} – {title}"

    embed = {
        "title":     title,
        "url":       card["page_url"],
        "color":     card["colour"],
        "image":     {"url": card["image_url"]},
        "footer":    {"text": "One Piece Top Decks • onepiecetopdecks.com"},
        "timestamp": datetime.utcnow().isoformat(),
    }

    if card["details"]:
        embed["description"] = card["details"]

    payload = {
        "username":   "OP Spoiler Bot 🏴‍☠️",
        "avatar_url": "https://onepiecetopdecks.com/wp-content/uploads/2022/12/cropped-topdecks-270x270.jpg",
        "content":    "🆕 **New One Piece TCG Spoiler – OP-16 The Time of Battle!**",
        "embeds":     [embed],
    }

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 204:
            return True
        print(f"[WARN] Discord {r.status_code}: {r.text[:200]}")
        return False
    except requests.RequestException as e:
        print(f"[ERROR] Discord post failed: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Checking for new OP-16 spoilers…")

    seen  = load_seen()
    cards = fetch_cards()

    if not cards:
        print("[INFO] No cards parsed — check the URL or page structure.")
        return

    new_cards = [c for c in cards if card_uid(c) not in seen]
    print(f"[INFO] {len(new_cards)} new card(s) to post.")

    posted = 0
    for card in new_cards:
        uid     = card_uid(card)
        success = post_card(card)
        if success:
            seen.add(uid)
            posted += 1
            label = card["card_number"] or card["display_name"]
            print(f"  ✅  {label}")
            time.sleep(1.5)   # respect Discord rate limits
        else:
            print(f"  ❌  Failed: {card['display_name']}")

    save_seen(seen)
    print(f"[INFO] Done — posted {posted} new card(s).")


if __name__ == "__main__":
    main()
