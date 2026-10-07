## Task

Using only the instructions and facts above, write the analysis and the slide text for this audience.
Return ONE JSON object and nothing else: no markdown fences, no text before or after.

Required JSON shape (all keys required; use [] when a list is empty):

{
  "commentary": "plain prose, within the word range on the audience card",
  "analysis": {
    "headline": "one sentence stating the main result",
    "key_variances": ["up to 4 short statements, largest material variance first"],
    "drivers": ["each explanation must come from a Driver note, or say: Driver not yet provided for <line>"],
    "unexplained": ["material lines with no Driver note, or []"],
    "questions": ["questions for owners if the audience card allows the questions layout, otherwise []"]
  },
  "slides": [
    {"layout": "headline", "title": "takeaway under 70 characters", "bullets": ["at most 4 bullets, each under 160 characters"]},
    {"layout": "variance", "title": "...", "bullets": ["..."]}
  ]
}

Rules: use only layouts allowed for this audience; at most 6 slides; every figure must be copied exactly as formatted in the Facts; do not add any other key.
