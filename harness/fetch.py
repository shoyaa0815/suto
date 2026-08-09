import re
from html.parser import HTMLParser

import aiohttp

FETCH_MAX_CHARS = 4000

# PROMPT is this tool's slice of the AI's system prompt: when to call
# fetch_url and how to use its result. See the comment in datetime_tool.py
# for why this lives next to the tool instead of in ai.py.
#
# The link-verification rule below is written here, not in search.py, even
# though it's triggered by search_web results — the rule governs an action
# fetch_url performs (verify a link before recommending it), so it belongs
# with the tool doing the acting, not the tool that produced the link.
PROMPT = """- If the user shares a URL, use fetch_url to read it instead of guessing
  what it contains.
- If fetch_url comes back saying it's unavailable, tell the user that and
  answer with what you already know. Do not retry it.
- Before recommending a specific link for the user to buy something or view
  a listing (e.g. comparing prices across stores), use fetch_url on that
  link first to confirm the page still shows that listing. Drop any link
  that's dead, redirected, or no longer shows the item instead of including
  it. Skip this check for general information searches where you're not
  pointing the user to a specific listing."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch the text content of a specific web page URL, e.g. one the user shared.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch."},
            },
            "required": ["url"],
        },
    },
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.chunks.append(text)


async def fetch_url(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                html = await response.text()
    except Exception as e:
        print(f"[fetch_url] {type(e).__name__}: {e!r}")
        return "couldn't fetch that url"

    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
    return text[:FETCH_MAX_CHARS] if text else "page has no readable text content"
