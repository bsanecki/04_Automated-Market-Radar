import base64
import json
import os
import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup


DISCORD_WEBHOOK = os.environ["SYN2BIO_WEBHOOK"]

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "syn2bio_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-syn2bio-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Syn2bioMonitor/1.0)"}

SYN2BIO_RAPORTY_URL = "https://syn2bio.pl/relacje-inwestorskie/raporty-biezace/"
SYN2BIO_AKTUALNOSCI_URL = "https://syn2bio.pl/aktualnosci/"
SYN2BIO_BASE = "https://syn2bio.pl"
BANKIER_SYN2BIO_URL = "https://www.bankier.pl/tag/syn2bio"
BANKIER_BASE = "https://www.bankier.pl"

MAX_IDS_PER_SOURCE = 300
MAX_SAVE_RETRIES = 5

# Pokazujemy tylko wiadomości/raporty od 1 czerwca 2026 włącznie (nie całą
# historię spółki od początku roku).
CUTOFF_DATE = date(2026, 6, 1)

DATE_PATTERN = re.compile(r"\d{2}\.\d{2}\.\d{4},?\s*\d{2}:\d{2}")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}")
DATE_ONLY_PATTERN = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s*(.*)$")
BANKIER_ARTICLE_PATTERN = re.compile(r"/wiadomosc/.+-\d+\.html", re.IGNORECASE)


def parse_date_value(date_str):
    """Próbuje sparsować datę w formatach spotykanych na obu stronach."""
    date_str = (date_str or "").strip()

    for fmt in (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y, %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    return None


def is_recent_enough(article):
    parsed = article.get("parsed_date")
    # Brak możliwości sparsowania daty -> wolimy pokazać niż zgubić wpis.
    return parsed is None or parsed >= CUTOFF_DATE


def load_remote_state():
    response = requests.get(
        GH_API_URL,
        headers=GH_HEADERS,
        params={"ref": GH_BRANCH},
        timeout=20,
    )

    if response.status_code == 404:
        return {}, None

    response.raise_for_status()
    payload = response.json()

    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content), payload["sha"]


def save_remote_state(state, sha):
    body = {
        "message": "Update Syn2bio news state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def merge_sent_ids(state, sent_ids_by_source):
    for source_name, ids in sent_ids_by_source.items():
        bucket = state.setdefault(source_name, [])

        for article_id in ids:
            if article_id not in bucket:
                bucket.append(article_id)

        if len(bucket) > MAX_IDS_PER_SOURCE:
            del bucket[: len(bucket) - MAX_IDS_PER_SOURCE]

    return state


def persist_sent_ids(sent_ids_by_source):
    if not any(sent_ids_by_source.values()):
        print("Brak nowych ID do zapisania – pomijam zapis stanu.")
        return

    last_error = None

    for attempt in range(1, MAX_SAVE_RETRIES + 1):
        try:
            state, sha = load_remote_state()
            merged = merge_sent_ids(state, sent_ids_by_source)
            response = save_remote_state(merged, sha)

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


def get_syn2bio_reports():
    """
    Tabela 'Raporty bieżące' na syn2bio.pl - oficjalne komunikaty ESPI
    spółki, już przefiltrowane do samego Syn2bio.
    """
    response = requests.get(SYN2BIO_RAPORTY_URL, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")

    if not table:
        raise RuntimeError("Nie znaleziono tabeli raportów Syn2bio.")

    reports = []

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")

        if len(cells) < 2:
            continue

        date_text = cells[0].get_text(" ", strip=True)
        label = cells[1].get_text(" ", strip=True)
        opis = cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""

        link_tag = row.find("a", href=True)

        if not link_tag:
            continue

        link = link_tag["href"]

        if link and not link.startswith("http"):
            link = SYN2BIO_BASE + link

        title = f"{label} – {opis}" if opis else label

        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "..."

        reports.append(
            {
                "id": link,
                "title": title,
                "date": date_text,
                "parsed_date": parse_date_value(date_text),
                "source": "Syn2bio – Raporty bieżące (ESPI)",
                "description": "Oficjalny raport bieżący spółki Syn2bio (ESPI).",
                "link": link,
                "is_official": True,
            }
        )

    return reports


def get_syn2bio_aktualnosci():
    """
    Lista 'Aktualności' na syn2bio.pl - każda pozycja zaczyna się w
    tekście linku od daty (DD.MM.YYYY), co odróżnia ją od linków
    nawigacyjnych bez daty.
    """
    response = requests.get(
        SYN2BIO_AKTUALNOSCI_URL, timeout=20, headers=HTTP_HEADERS
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    articles = []
    seen_links = set()

    for link_tag in soup.find_all("a", href=True):
        text = link_tag.get_text(" ", strip=True)
        match = DATE_ONLY_PATTERN.match(text)

        if not match:
            continue

        href = link_tag["href"]
        link = href if href.startswith("http") else SYN2BIO_BASE + href

        if link in seen_links:
            continue

        seen_links.add(link)

        date_text, rest = match.group(1), match.group(2).strip()
        # Reszta tekstu to zwykle "Tytuł - Kategoria - Kategoria2".
        title = rest.split(" - ")[0].strip() or rest

        articles.append(
            {
                "id": link,
                "title": title,
                "date": date_text,
                "parsed_date": parse_date_value(date_text),
                "source": "Syn2bio – Aktualności",
                "description": "Aktualność ze strony spółki Syn2bio.",
                "link": link,
                "is_official": False,
            }
        )

    return articles


def get_bankier_syn2bio_articles():
    """
    Tag 'syn2bio' na Bankier.pl - już przefiltrowany do samej spółki.
    Wymagamy obecności daty przy linku, żeby odciąć boczny panel
    'Najpopularniejsze' (niepowiązane newsy z całego portalu).
    """
    response = requests.get(BANKIER_SYN2BIO_URL, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    articles = []
    seen_links = set()

    for link_tag in soup.find_all("a", href=True):
        href = link_tag["href"]

        if not BANKIER_ARTICLE_PATTERN.search(href):
            continue

        link = href if href.startswith("http") else BANKIER_BASE + href

        if link in seen_links:
            continue

        seen_links.add(link)

        full_text = link_tag.get_text(" ", strip=True)
        date_match = ISO_DATE_PATTERN.search(full_text) or DATE_PATTERN.search(
            full_text
        )

        if date_match:
            date_text = date_match.group(0)
            title = full_text[len(date_text):].strip(" :-") or full_text
        else:
            container = link_tag.find_parent(["li", "div"]) or link_tag.parent
            container_text = container.get_text(" ", strip=True) if container else ""
            date_match = ISO_DATE_PATTERN.search(
                container_text
            ) or DATE_PATTERN.search(container_text)

            if not date_match:
                # Brak daty w pobliżu = boczny panel z niepowiązanymi newsami.
                continue

            date_text = date_match.group(0)
            title = full_text

        if not title:
            continue

        if len(title) > 180:
            title = title[:180].rsplit(" ", 1)[0] + "..."

        articles.append(
            {
                "id": link,
                "title": title,
                "date": date_text,
                "parsed_date": parse_date_value(date_text),
                "source": "Bankier.pl – Wiadomości (Syn2bio)",
                "description": "Wiadomość dotycząca spółki Syn2bio (Bankier.pl).",
                "link": link,
                "is_official": False,
            }
        )

    return articles


def send_to_discord(article):
    description = article.get("description", "")

    if len(description) > 500:
        description = description[:500].rsplit(" ", 1)[0] + "..."

    official_tag = "📌 **Oficjalny komunikat**\n" if article.get("is_official") else ""

    embed = {
        "title": f"📰 {article['title']}",
        "url": article["link"],
        "description": (
            f"{official_tag}"
            f"📅 **Data:** {article['date']}\n"
            f"🌐 **Źródło:** {article['source']}\n\n"
            f"📝 {description}\n\n"
            f"🔗 [Otwórz oryginał]({article['link']})"
        ),
        "color": 0x2ECC71 if article.get("is_official") else 0x3498DB,
    }

    response = requests.post(
        DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20
    )
    response.raise_for_status()


def process_source(articles, source_name, known_state, sent_ids_by_source):
    already_sent = set(known_state.get(source_name, []))
    new_ids = []

    for article in reversed(articles):
        article_id = article["id"]

        if not article_id or article_id in already_sent:
            continue

        if not is_recent_enough(article):
            continue

        send_to_discord(article)
        time.sleep(1)

        new_ids.append(article_id)

    sent_ids_by_source[source_name] = new_ids
    return len(new_ids)


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()

    sent_ids_by_source = {}
    total_new = 0

    sources = [
        ("syn2bio_raporty", "Sprawdzam Syn2bio – Raporty bieżące...", get_syn2bio_reports),
        ("syn2bio_aktualnosci", "Sprawdzam Syn2bio – Aktualności...", get_syn2bio_aktualnosci),
        ("bankier_syn2bio", "Sprawdzam Bankier.pl (tag: syn2bio)...", get_bankier_syn2bio_articles),
    ]

    for source_name, log_message, fetch_fn in sources:
        print(log_message)

        try:
            articles = fetch_fn()
            print(f"  Znaleziono pozycji dot. Syn2bio: {len(articles)}")
            total_new += process_source(
                articles, source_name, known_state, sent_ids_by_source
            )
        except Exception as error:
            print(f"  Błąd źródła '{source_name}': {error}")

    print("Zapisuję stan...")
    persist_sent_ids(sent_ids_by_source)

    print(f"Nowych informacji wysłanych: {total_new}")


if __name__ == "__main__":
    main()
