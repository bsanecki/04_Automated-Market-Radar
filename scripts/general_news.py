import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup


DISCORD_WEBHOOK = os.environ["GENERAL_NEWS_WEBHOOK"]

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "general_news_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-general-news-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GeneralNewsMonitor/1.0)"}

AP_HOME_URL = "https://apnews.com/"
ARTICLE_LINK_PATTERN = re.compile(r"^https://apnews\.com/article/[a-z0-9\-]+", re.I)

MAX_SEEN_IDS = 1000
MAX_SAVE_RETRIES = 5

# Ile godzin wstecz liczymy jako "świeży" artykuł.
FRESHNESS_HOURS = 24

# Minimalna liczba punktów, żeby artykuł w ogóle się kwalifikował.
SCORE_THRESHOLD = 5

# Maksymalna liczba artykułów wysyłanych w jednym uruchomieniu (dobowym).
MAX_ARTICLES_PER_RUN = 3

# --- Artykuły odrzucane automatycznie, niezależnie od punktacji poniżej ---
# Uwaga: to dopasowanie słów kluczowych, nie prawdziwe rozumienie tekstu —
# to najlepsze przybliżenie bez wywoływania osobnego modelu językowego.
REJECT_PATTERNS = [
    r"\bstock(s)? (rise|rises|rising|fall|falls|falling|jump|jumps|drop|drops|surge|surges|slide|slides|climb|climbs|sink|sinks)\b",
    r"\bshares (rise|rises|fall|falls|jump|jumps|drop|drops|surge|surges|slide|slides)\b",
    r"\bearnings\b",
    r"\bquarterly (results|report|earnings)\b",
    r"\b(q1|q2|q3|q4) (results|earnings)\b",
    r"\banalysts? (say|says|expect|expects|predict|predicts|forecast|forecasts)\b",
    r"\bprice target\b",
    r"\bthings to know\b",
    r"\bthings you need to know\b",
    r"\b\d+ things\b",
    r"\bbox office\b",
    r"\bmovie review\b",
    r"\balbum review\b",
    r"\b(actor|actress|singer|celebrity|celebrities)\b",
    r"\b(nba|nfl|nhl|mlb|olympics|world cup|super bowl)\b",
    r"\b(football|basketball|baseball|soccer|tennis) (game|match|team|player)\b",
]

# --- Punktacja tematów istotnych dla rynku ---
# Format: (regex, punkty)
TIER_5_PATTERNS = [
    (r"\bfed(eral reserve)? (cuts|cut|raises|raised|hikes|hiked)\b", 5),
    (r"\becb (cuts|cut|raises|raised|hikes|hiked)\b", 5),
    (r"\binterest rate decision\b", 5),
    (r"\brate (cut|hike)\b", 5),
    (r"\bunexpected rate\b", 5),
    (r"\bsurprise rate\b", 5),
    (r"\binflation report\b", 5),
    (r"\bcpi report\b", 5),
    (r"\bconsumer price index\b", 5),
    (r"\bceasefire\b", 5),
    (r"\binvasion\b", 5),
    (r"\bdeclares? war\b", 5),
    (r"\bmajor sanctions\b", 5),
    (r"\bnew sanctions\b", 5),
    (r"\bfinancial crisis\b", 5),
    (r"\bbank collapse\b", 5),
    (r"\bbanking crisis\b", 5),
    (r"\bsovereign default\b", 5),
    (r"\bdebt default\b", 5),
]

TIER_3_PATTERNS = [
    (r"\bgdp (growth|data|report|contracts|contraction)\b", 3),
    (r"\bunemployment rate\b", 3),
    (r"\bjobs report\b", 3),
    (r"\bemployment report\b", 3),
    (r"\btrade deal\b", 3),
    (r"\btrade war\b", 3),
    (r"\btariffs?\b", 3),
    (r"\beconomic policy\b", 3),
    (r"\bstimulus package\b", 3),
    (r"\bdebt ceiling\b", 3),
    (r"\bmonetary policy\b", 3),
    (r"\bcentral bank\b", 3),
    (r"\brecession\b", 3),
    (r"\bus-china\b", 3),
    (r"\brussia-ukraine\b", 3),
    (r"\btaiwan strait\b", 3),
]

TIER_1_PATTERNS = [
    (r"\boil prices?\b", 1),
    (r"\benergy prices?\b", 1),
    (r"\bopec\b", 1),
    (r"\bcrude oil\b", 1),
    (r"\bnatural gas prices?\b", 1),
    (r"\bchina('s)? economy\b", 1),
    (r"\beurozone economy\b", 1),
    (r"\bglobal economy\b", 1),
    (r"\bemerging markets\b", 1),
    (r"\bmiddle east\b", 1),
]

ALL_TIER_PATTERNS = TIER_5_PATTERNS + TIER_3_PATTERNS + TIER_1_PATTERNS


def load_remote_state():
    response = requests.get(
        GH_API_URL,
        headers=GH_HEADERS,
        params={"ref": GH_BRANCH},
        timeout=20,
    )

    if response.status_code == 404:
        return {"seen_ids": []}, None

    response.raise_for_status()
    payload = response.json()

    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content), payload["sha"]


def save_remote_state(state, sha):
    body = {
        "message": "Update General News monitor state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def persist_seen_ids(new_seen_ids):
    if not new_seen_ids:
        print("Brak nowych ID do zapisania – pomijam zapis stanu.")
        return

    last_error = None

    for attempt in range(1, MAX_SAVE_RETRIES + 1):
        try:
            state, sha = load_remote_state()
            bucket = state.setdefault("seen_ids", [])

            for article_id in new_seen_ids:
                if article_id not in bucket:
                    bucket.append(article_id)

            if len(bucket) > MAX_SEEN_IDS:
                del bucket[: len(bucket) - MAX_SEEN_IDS]

            response = save_remote_state(state, sha)

            if response.status_code in (200, 201):
                print(f"Stan zapisany (próba {attempt}).")
                return

            if response.status_code in (409, 422):
                print(
                    f"Konflikt zapisu stanu (próba {attempt}/{MAX_SAVE_RETRIES}), "
                    f"pobieram świeży stan i ponawiam..."
                )
                last_error = response.text
                time.sleep(1.5 * attempt)
                continue

            response.raise_for_status()

        except requests.RequestException as error:
            last_error = str(error)
            print(f"Błąd sieci przy zapisie stanu (próba {attempt}): {error}")
            time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"Nie udało się zapisać stanu po {MAX_SAVE_RETRIES} próbach. "
        f"Ostatni błąd: {last_error}"
    )


def get_candidate_links():
    """
    Pobiera stronę główną AP News i wyciąga unikalne linki do artykułów.
    Opieramy się na stabilnej strukturze URL (/article/...), a nie na
    klasach CSS kart, które mogą się zmieniać przy redesignach.
    """
    response = requests.get(AP_HOME_URL, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    links = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        if href.startswith("/"):
            href = "https://apnews.com" + href

        match = ARTICLE_LINK_PATTERN.match(href)

        if not match:
            continue

        clean_link = match.group(0)

        if clean_link in seen:
            continue

        seen.add(clean_link)
        links.append(clean_link)

    return links


def fetch_article_details(link):
    """
    Wchodzi na stronę artykułu i wyciąga tytuł, opis (meta description)
    oraz datę publikacji (meta article:published_time) — to standardowe
    tagi meta, dużo stabilniejsze niż layout strony.
    """
    response = requests.get(link, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title_tag = soup.find("meta", property="og:title") or soup.find("title")
    title = (
        title_tag.get("content") if title_tag and title_tag.has_attr("content")
        else (title_tag.get_text(strip=True) if title_tag else "")
    )

    desc_tag = soup.find("meta", property="og:description") or soup.find(
        "meta", attrs={"name": "description"}
    )
    description = desc_tag.get("content", "") if desc_tag else ""

    date_tag = soup.find("meta", property="article:published_time")
    published_raw = date_tag.get("content", "") if date_tag else ""
    published_dt = parse_iso_datetime(published_raw)

    return {
        "title": title or "",
        "description": description or "",
        "published_dt": published_dt,
    }


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt
    except ValueError:
        return None


def is_fresh_enough(published_dt):
    # Brak sparsowanej daty -> nie odrzucamy automatycznie (jak w innych
    # monitorach w tym repo), traktujemy jako potencjalnie świeże.
    if published_dt is None:
        return True

    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    return published_dt >= cutoff


def is_rejected(text):
    return any(re.search(pattern, text) for pattern in REJECT_PATTERNS)


def score_article(text):
    score = 0
    matched_labels = []

    for pattern, points in ALL_TIER_PATTERNS:
        if re.search(pattern, text):
            score += points
            matched_labels.append(pattern)

    return score, matched_labels


def send_to_discord(article, rank):
    embed = {
        "title": f"🌍 {article['title']}",
        "url": article["link"],
        "description": (
            f"⭐ **Ważność:** {article['score']} pkt (miejsce {rank}/{MAX_ARTICLES_PER_RUN})\n"
            f"🌐 **Źródło:** AP News\n\n"
            f"📝 {article['description']}\n\n"
            f"🔗 [Otwórz oryginał]({article['link']})"
        ),
        "color": 0x9B59B6,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()
    already_seen = set(known_state.get("seen_ids", []))

    print("Pobieram listę artykułów ze strony głównej AP News...")

    try:
        links = get_candidate_links()
    except Exception as error:
        print(f"Błąd pobierania strony głównej AP: {error}")
        return

    new_links = [link for link in links if link not in already_seen]
    print(f"Znaleziono {len(links)} linków, w tym {len(new_links)} nowych.")

    candidates = []
    newly_seen_ids = []

    for link in new_links:
        try:
            details = fetch_article_details(link)
        except Exception as error:
            print(f"  Błąd pobierania artykułu {link}: {error}")
            continue

        time.sleep(1)

        newly_seen_ids.append(link)

        if not is_fresh_enough(details["published_dt"]):
            print(f"  Pomijam (starszy niż {FRESHNESS_HOURS}h): {link}")
            continue

        combined_text = f"{details['title']} {details['description']}".lower()

        if is_rejected(combined_text):
            print(f"  Odrzucony (kategoria wykluczona): {details['title'][:80]}")
            continue

        score, _ = score_article(combined_text)

        if score < SCORE_THRESHOLD:
            print(f"  Zbyt niski wynik ({score} pkt): {details['title'][:80]}")
            continue

        print(f"  Kwalifikuje się ({score} pkt): {details['title'][:80]}")
        candidates.append(
            {
                "link": link,
                "title": details["title"],
                "description": details["description"],
                "score": score,
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    to_send = candidates[:MAX_ARTICLES_PER_RUN]

    print(f"Kwalifikujących się artykułów: {len(candidates)}, wysyłam: {len(to_send)}")

    for rank, article in enumerate(to_send, start=1):
        send_to_discord(article, rank)
        time.sleep(1)

    print("Zapisuję stan...")
    persist_seen_ids(newly_seen_ids)

    print(f"Wysłanych artykułów: {len(to_send)}")


if __name__ == "__main__":
    main()
