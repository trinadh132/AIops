"""
Web search for the confidence-gated fallback node, using OpenRouter's
`web` plugin. This works with any model on OpenRouter (native search for
Anthropic/OpenAI/Perplexity/xAI, Exa for everything else) and returns
citations in a standard annotation schema — no separate search API key.

Note: OpenRouter's docs currently mark the `web` plugin as deprecated in
favor of a newer `openrouter:web_search` server tool that lets the model
decide when to search. We deliberately don't use that here — our
LangGraph confidence_gate already decides *when* to search, so the
simpler always-search-once-per-call plugin is the better fit. Re-check
openrouter.ai/docs/features/web-search if you revisit this later.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Any model works — the web plugin is model-agnostic. A cheap model is
# fine here since we only use the citations, not the model's prose.
SEARCH_MODEL = os.environ.get("SEARCH_MODEL", "minimax/minimax-m3:free")


def search(query: str, max_results: int = 5) -> list[dict]:
    """Returns a list of WebSearchResult-shaped dicts: query, title, url, snippet."""
    response = requests.post(
        OPENROUTER_CHAT_URL,
        headers={
            "Authorization": f"Bearer {os.environ["OPENROUTER_API"]}",
            "Content-Type": "application/json",
        },
        json={
            "model": SEARCH_MODEL,
            "messages": [{"role": "user", "content": query}],
            "plugins": [{"id": "web", "max_results": max_results}],
        },
        timeout=30,
    )
    response.raise_for_status()

    message = response.json()["choices"][0]["message"]
    annotations = message.get("annotations") or []

    return [
        {
            "query": query,
            "title": a["url_citation"].get("title", ""),
            "url": a["url_citation"]["url"],
            "snippet": a["url_citation"].get("content", ""),
        }
        for a in annotations
        if a.get("type") == "url_citation"
    ]