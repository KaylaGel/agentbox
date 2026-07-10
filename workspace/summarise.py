"""Summarise the latest articles from the allowed site.

Runs inside the agentbox container: fetches the RSS feed through the
per-agent egress proxy (urllib honors HTTP(S)_PROXY/NO_PROXY), sends the
articles to the LLM through the LiteLLM proxy, and writes the summary to
/workspace/brief.md. Stdlib only — the container has no route to PyPI.
"""

import json
import os
import urllib.request
from xml.etree import ElementTree

FEED = "https://www.bleepingcomputer.com/feed/"
MAX_ARTICLES = 10


def fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "Mozilla/5.0 (agentbox)",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


articles = []
for item in ElementTree.fromstring(fetch(FEED)).iter("item"):
    articles.append({
        "title": item.findtext("title"),
        "summary": item.findtext("description") or "",
        "link": item.findtext("link"),
        "published": item.findtext("pubDate"),
    })
    if len(articles) >= MAX_ARTICLES:
        break
print(f"fetched {len(articles)} articles from {FEED}")

task = os.environ.get("AGENT_TASK") or "Summarise the latest articles in bullet points."
body = {
    "model": os.environ["AGENT_MODEL"],
    "messages": [
        {"role": "system", "content": os.environ.get("AGENT_ROLE", "You are a research analyst.")},
        {"role": "user", "content": f"{task}\n\nArticles (JSON):\n{json.dumps(articles, indent=2)}"},
    ],
}
resp = json.loads(fetch(
    os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
))
summary = resp["choices"][0]["message"]["content"]

with open("/workspace/brief.md", "w") as f:
    f.write(summary + "\n")
print("\n" + summary + "\n\n(written to workspace/brief.md)")
