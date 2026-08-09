from datetime import datetime

# PROMPT is this tool's slice of the AI's system prompt: it tells the AI
# when/why to call get_current_datetime. harness/__init__.py collects every
# tool's PROMPT into one block that ai.py appends to its base system prompt.
# Keep it here (not in ai.py) so the usage rule always travels with the tool
# it governs — delete this file and its rule disappears with it.
PROMPT = """- You do not know the current date or time on your own — your training data
  has no awareness of "now". Any question involving today's date, the current
  time, day of the week, or how long ago/until something is, always call
  get_current_datetime first. Never guess it.
- get_current_datetime returns labelled fields. Copy the values out of it
  exactly as they are. Do not recompute, re-derive, or adjust any of them —
  including converting the year yourself, which the buddhist_year field
  already gives you."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": "Get the current local date and time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


# The return value is deliberately one labelled field per line rather than a
# formatted sentence like "Sunday 09 August 2026, 21:26:42". A 9B model reading
# that sentence kept shifting the day number by one while rewriting it into
# another language.
#
# Anything the model would otherwise have to derive for itself is precomputed
# and named here instead — buddhist_year rather than a year to add 543 to —
# because that arithmetic is exactly where a model this size goes wrong, and
# none of it needs a model to do it.
#
# Ready-made Thai month and weekday names were here too, for a while. They were
# added when Thai answers were inventing month names that are not words
# ("สหระคม", "สหคัศน์มกราคม") about half the time, and they did fix it. They are
# gone because date_iso and the digit rules added afterwards fixed the same
# thing more generally: with those in place, dropping the Thai fields changed
# nothing measurable (8/9 correct either way over nine Thai answers), and the
# invented month names did not come back. If they ever do, this is where the
# fix went last time.
#
# date_iso looks like it only restates day_of_month, month and year, and it was
# dropped once on that reasoning. Removing it broke English answers immediately:
# five of six replies moved the date into September while month still read
# August, one of them arguing with itself mid-sentence about which was right.
# It is not a duplicate — it is the one field that states the month as a number,
# so the model never has to match a month name to a position in the year. Keep
# it, and prefer adding this kind of redundancy over trimming it.
#
# There is, however, no utc_offset field. It was here briefly, and
# the model treated it as an invitation to shift the clock itself: it started
# answering 18:44 for a 21:44 local time and aging the year by one along the
# way, in questions that never mentioned UTC. These are local times; anything
# that has to be converted from them is better added as its own field.
def get_current_datetime() -> str:
    now = datetime.now().astimezone()
    return "\n".join(
        [
            f"day_of_week: {now.strftime('%A')}",
            f"day_of_month: {now.day}",
            f"month: {now.strftime('%B')}",
            f"year: {now.year}",
            f"buddhist_year: {now.year + 543}",
            f"date_iso: {now.strftime('%Y-%m-%d')}",
            f"time_24h: {now.strftime('%H:%M')}",
        ]
    )
