"""
Monitor wiadomości giełdowych dla Synektik S.A.

Sprawdza wiarygodne źródła (raporty spółki, komunikaty ESPI, wiadomości giełdowe)
i wysyła NOWE informacje dotyczące wyłącznie Synektik S.A. na kanał Discord.

Stan wysłanych już artykułów jest przechowywany w pliku `synektik_state.json`
w tym repozytorium, ale — inaczej niż wcześniej — NIE jest zapisywany przez
`git commit` + `git push`, tylko przez GitHub Contents API z mechanizmem
optymistycznej blokady (porównanie SHA pliku) i automatycznym retry.

Dlaczego zmiana sposobu zapisu stanu:
    Stary workflow robił `git pull --rebase` + `git push` na końcu joba.
    To działa tylko wtedy, gdy nic nie zmieni gałęzi `main` pomiędzy tymi
    dwoma krokami. W GitHub Actions to założenie łatwo złamać (kolejny
    przebieg workflow, commit z innego źródła, opóźnienie w sieci) i wtedy
    push kończy się błędem `! [rejected] (fetch first)`, a stan po prostu
    nie zostaje zapisany.

    GitHub Contents API (`PUT /repos/{owner}/{repo}/contents/{path}`)
    pozwala zaktualizować pojedynczy plik atomowo, pod warunkiem podania
    aktualnego `sha` pliku. Jeśli ktoś zmienił plik w międzyczasie, API
    zwraca błąd 409/422 zamiast po cichu nadpisywać albo się gubić – wtedy
    skrypt po prostu pobiera świeży stan, dokleja do niego swoje nowe ID
    i próbuje ponownie. Nie trzeba w ogóle robić `git commit`/`git push`
    w workflow, więc znika cała klasa błędów związanych z rebase/race
    condition.
"""

import base64
import json
import os
import re
import time

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK = os.environ["SYNEKTIK_WEBHOOK"]

# Automatyczny token GITHUB_TOKEN z workflow (permissions: contents: write)
GH_TOKEN = os.environ["GH_TOKEN"]
# GITHUB_REPOSITORY i GITHUB_REF_NAME są ustawiane automatycznie przez Actions
GH_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GH_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_PATH = "synektik_state.json"
GH_API_URL = f"https://api.github.com/repos/{GH_REPOSITORY}/contents/{STATE_PATH}"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "market-radar-synektik-bot",
}

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SynektikMonitor/1.0)"}

SYNEKTIK_URL = "https://synektik.com.pl/centrum-inwestora/raporty-biezace/"
BANKIER_GIELDA_RSS = "https://www.bankier.pl/rss/gielda.xml"
BANKIER_ESPI_RSS = "https://www.bankier.pl/rss/espi.xml"

MAX_IDS_PER_SOURCE = 300
MAX_SAVE_RETRIES = 5

# Dopasowanie nazwy spółki z granicami słów (żeby "Synektik" nie złapał się
# przypadkiem jako fragment innego słowa) oraz tickera SNT, rozpoznawanego
# tylko jako osobny, wielkoliterowy token (żeby nie łapać przypadkowych
# trzyliterowych skrótów w tekście).
NAME_PATTERN = re.compile(r"\bsynektik\b", re.IGNORECASE)
TICKER_PATTERN = re.compile(r"\bSNT\b")


def is_about_synektik(title, description=""):
    text = f"{title} {description}"
    return bool(NAME_PATTERN.search(text)) or bool(TICKER_PATTERN.search(text))


# ---------------------------------------------------------------------------
# Stan – odczyt / zapis przez GitHub Contents API
# ---------------------------------------------------------------------------

def load_remote_state():
    """Zwraca (state_dict, sha). sha=None jeśli plik jeszcze nie istnieje."""
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
    """Zwraca requests.Response. Wywołujący sprawdza status i decyduje o retry."""
    body = {
        "message": "Update Synektik news state [skip ci]",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GH_BRANCH,
    }

    if sha:
        body["sha"] = sha

    return requests.put(GH_API_URL, headers=GH_HEADERS, json=body, timeout=20)


def merge_sent_ids(state, sent_ids_by_source):
    """Dokleja nowo wysłane ID do danego stanu (in place) i przycina listy."""
    for source_name, ids in sent_ids_by_source.items():
        bucket = state.setdefault(source_name, [])

        for article_id in ids:
            if article_id not in bucket:
                bucket.append(article_id)

        if len(bucket) > MAX_IDS_PER_SOURCE:
            del bucket[: len(bucket) - MAX_IDS_PER_SOURCE]

    return state


def persist_sent_ids(sent_ids_by_source):
    """
    Zapisuje nowo wysłane ID w pliku stanu w repo, z retry przy konflikcie.

    Ponieważ wiadomości do Discorda zostały już wysłane w tym momencie,
    ta funkcja MUSI w końcu zapisać ID (inaczej przy kolejnym uruchomieniu
    te same artykuły trafią na Discord ponownie) – stąd pętla retry zamiast
    pojedynczej próby.
    """
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


# ---------------------------------------------------------------------------
# Źródła danych
#
# Sprawdzone i użyte (stabilne):
#   - Synektik – Centrum Inwestora    -> scraping tabeli raportów (oficjalne)
#   - Bankier.pl – RSS "Giełda"       -> https://www.bankier.pl/rss/gielda.xml
#   - Bankier.pl – RSS "ESPI"         -> https://www.bankier.pl/rss/espi.xml
#     (to jest ten sam system komunikatów ESPI, który obsługuje GPW, więc
#     ten jeden feed pokrywa zarówno "GPW/ESPI", jak i "Bankier.pl – ESPI"
#     z listy źródeł – nie ma sensu duplikować tego samego strumienia)
#
# Świadomie pominięte (patrz wiadomość z wyjaśnieniem):
#   - PAP Biznes  – brak publicznego RSS/API, to usługa
#     subskrypcyjna/terminalowa; PAP MediaRoom to inny serwis (dystrybucja
#     PR), nie chciałem podszywać się nim pod "depesze PAP Biznes"
#   - Parkiet     – artykuły są za paywallem, nie ma już działającego RSS
#   - ISBnews     – oficjalny RSS (isbnews.pl/rss.php) istnieje, ale zwraca
#     pusty kanał; strona z listą depesz nie ma w kodzie prostych,
#     przewidywalnych linków <a href> do pojedynczych depesz, więc bez
#     dostępu do realnego źródła HTML nie chciałem zgadywać selektorów
# ---------------------------------------------------------------------------

def get_synektik_reports():
    response = requests.get(SYNEKTIK_URL, timeout=20, headers=HTTP_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")

    if not table:
        raise RuntimeError("Nie znaleziono tabeli raportów Synektik.")

    reports = []

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")

        if len(cells) < 4:
            continue

        title = cells[1].get_text(" ", strip=True)
        date = cells[2].get_text(" ", strip=True)

        link_tag = cells[3].find("a")

        if not link_tag:
            continue

        link = link_tag.get("href")

        if link and not link.startswith("http"):
            link = "https://synektik.com.pl" + link

        reports.append(
            {
                "id": link,
                "title": title,
                "date": date,
                "source": "Synektik S.A. – Centrum Inwestora",
                "description": "Oficjalny raport / komunikat spółki.",
                "link": link,
                "is_official": True,
            }
        )

    return reports


def _parse_bankier_rss(feed_url, source_label, is_official):
    feed = feedparser.parse(feed_url)
    articles = []

    for entry in feed.entries:
        title = entry.get("title", "")
        raw_description = entry.get("summary", "")
        link = entry.get("link", "")

        description = BeautifulSoup(raw_description, "html.parser").get_text(
            " ", strip=True
        )

        if not is_about_synektik(title, description):
            continue

        date = entry.get("published", "") or entry.get("updated", "") or "Brak daty"

        articles.append(
            {
                "id": link,
                "title": title,
                "date": date,
                "source": source_label,
                "description": description,
                "link": link,
                "is_official": is_official,
            }
        )

    return articles


def get_bankier_gielda_articles():
    return _parse_bankier_rss(
        BANKIER_GIELDA_RSS, "Bankier.pl – Giełda", is_official=False
    )


def get_bankier_espi_articles():
    return _parse_bankier_rss(
        BANKIER_ESPI_RSS,
        "Bankier.pl / ESPI – raport bieżący spółki",
        is_official=True,
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Główna logika
# ---------------------------------------------------------------------------

def process_source(articles, source_name, known_state, sent_ids_by_source):
    already_sent = set(known_state.get(source_name, []))
    new_ids = []

    for article in reversed(articles):  # od najstarszego do najnowszego
        article_id = article["id"]

        if not article_id or article_id in already_sent:
            continue

        send_to_discord(article)
        new_ids.append(article_id)
        time.sleep(1)  # uprzejmy odstęp między wiadomościami na Discord

    sent_ids_by_source[source_name] = new_ids
    return len(new_ids)


def main():
    print("Wczytuję aktualny stan z repozytorium (GitHub Contents API)...")
    known_state, _ = load_remote_state()

    sent_ids_by_source = {}
    total_new = 0

    sources = [
        ("synektik", "Sprawdzam Synektik – Centrum Inwestora...", get_synektik_reports),
        ("bankier_gielda", "Sprawdzam Bankier.pl (Giełda)...", get_bankier_gielda_articles),
        ("bankier_espi", "Sprawdzam Bankier.pl (ESPI)...", get_bankier_espi_articles),
    ]

    for source_name, log_message, fetch_fn in sources:
        print(log_message)

        try:
            articles = fetch_fn()
            print(f"  Znaleziono pozycji dot. Synektika: {len(articles)}")
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
