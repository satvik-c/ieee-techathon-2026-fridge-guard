"""
FridgeGuard — Analyzer module (Groq vision, single pass).

Injects current inventory into prompt so Groq uses exact stored
descriptions for removals, and includes item count so Groq doesn't
hallucinate duplicates.
"""

import base64
import io
import json
import re
import time

import numpy as np
from PIL import Image


SEND_WIDTH = 1024

BASE_PROMPT = """\
You are a visual change detector for a refrigerator.
Photo 1 is BEFORE. Photo 2 is AFTER.

CONTEXT:
- Blue LED lighting makes both photos look dark and blue-tinted. This is normal — do not treat it as a difference.
- There is a bright light at the back of the fridge. Objects near it may look 
  washed out but are still real solid objects. However, if an area that 
  previously had an object is now just empty shelf or wall, that object is REMOVED.
- The camera is wide-angle. Objects at the edges and corners of the frame are real — do not ignore them.
- If one object is partially behind another, they are TWO separate objects. If both disappear, that is TWO removals.
- BACKGROUND LIGHT does not move or change between photos — if something near it disappeared, it was a real object.
- Objects may shift slightly in position between photos — this is irrelevant. Only care about objects that completely disappear or newly appear.
- PARTIALLY OBSCURED OBJECTS: count every distinct object even if only partially visible.
- Photo 1 and Photo 2 may have different brightness levels due to door position 
  during capture. Brightness difference is NOT a reason to assume an item is 
  still present. If an item occupied a specific region in Photo 1 and that 
  region is clearly empty in Photo 2, report it as REMOVED even if Photo 2 
  is darker overall.
- A darker Photo 2 does not mean items are hidden. Empty space looks empty 
  regardless of brightness.

CRITICAL RULES FOR EDGE CASES (OBJECT PERMANENCE & STRICT MATH):
- CUTOFFS & OCCLUSIONS ARE EXPECTED: The internal space is extremely tight (roughly 28x40x38 cm). Because the camera is wide-angle and close to the items, objects WILL frequently be cut in half by the edge of the frame or tightly packed together.
- THE 80% SIMILARITY RULE: If you see an object that looks 80% similar to an item right next to it, but it is slightly cut off, distorted by the lens edge, or partially obscured, YOU MUST COUNT IT AS A SEPARATE, ADDITIONAL ITEM.
- THE HIDDEN ITEM FALLBACK: If a large item appears in Photo 2 and blocks the view, ASSUME the items previously behind it are still there.
- NO SUMMARIZATION: NEVER group identical items together. NEVER use numbers like "two" or "four" in your descriptions. 
- STRICT QUANTITY TRACKING (THE MATH RULE): You MUST count the exact number of identical items. If the inventory lists 4 identical items, but you only see 3 in Photo 2, you MUST report 1 as "removed". If you see 5, you MUST report 1 as "added". NEVER assume "at least one is still there, so none were removed."

YOUR TASK:
There are only two possible changes: ADDED or REMOVED. There is no "moved".

For every object in Photo 1: is it still present anywhere in Photo 2? If not → REMOVED.
For every object in Photo 2: was it present in Photo 1? If not → ADDED.

Scan every zone: top-left, top-center, top-right, bottom-left, bottom-center, bottom-right.

{inventory_section}

DESCRIBE NEW OBJECTS BY APPEARANCE ONLY:
- Shape + color + size. No brand names, no label text.
- Good: "small cylindrical can, blue and silver"
- Bad: "Monster", "energy drink"

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "before_contents": ["description 1", "description 2", ...],
  "after_contents":  ["description 1", "description 2", ...],
  "changes": [
    {{"item": "<description>", "action": "added" | "removed"}}
  ]
}}

List every individual object separately.
If nothing changed, return an empty changes array.
"""

INVENTORY_SECTION = """\
CURRENT FRIDGE INVENTORY — there are exactly {count} item(s) in the fridge right now:
{lines}

IMPORTANT:
- Do NOT report more removals than the total number of items listed above ({count} item(s)).
- If any of these items are missing in Photo 2, use the EXACT description string shown \
above in your changes array — do not rephrase or redescribe them.
- If an item appears to still be present but looks slightly different (different angle, \
lighting), do NOT report it as removed.
"""

EMPTY_INVENTORY_SECTION = """\
CURRENT FRIDGE INVENTORY: empty — no items have been logged yet.
Describe any removed items by appearance only.
"""


class Analyzer:
    def __init__(self, api_key: str,
                 model: str = "meta-llama/llama-4-scout-17b-16e-instruct"):
        self.api_key = api_key
        self.model   = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self.api_key)
            print(f"[Analyzer] Groq client ready (model: {self.model})")
        return self._client

    def _encode(self, frame: np.ndarray) -> str:
        img   = Image.fromarray(frame)
        w, h  = img.size
        new_h = int(h * SEND_WIDTH / w)
        img   = img.resize((SEND_WIDTH, new_h), Image.LANCZOS)
        buf   = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        kb    = len(buf.getvalue()) // 1024
        b64   = base64.b64encode(buf.getvalue()).decode("utf-8")
        print(f"[Analyzer] Encoded {w}x{h} → {SEND_WIDTH}x{new_h}, {kb}KB")
        return f"data:image/jpeg;base64,{b64}"

    def _build_prompt(self, inventory: list[dict]) -> str:
        if inventory:
            lines = "\n".join(
                f'  - "{item["item_name"]}" (owned by {item["owner"]})'
                for item in inventory
            )
            inv_section = INVENTORY_SECTION.format(
                count=len(inventory),
                lines=lines,
            )
        else:
            inv_section = EMPTY_INVENTORY_SECTION
        return BASE_PROMPT.format(inventory_section=inv_section)

    def _parse(self, text: str) -> dict:
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            text  = parts[1] if len(parts) >= 2 else parts[0]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = text[start:end + 1]
        else:
            clean = text[start:] if start != -1 else text

        # Try clean parse first
        try:
            result = json.loads(clean)
            result.setdefault("before_contents", [])
            result.setdefault("after_contents",  [])
            result.setdefault("changes", [])

            # Recompute changes from content lists — more reliable than
            # trusting the model's own changes array
            before = result["before_contents"]
            after  = result["after_contents"]
            if before or after:
                result["changes"] = self._diff_contents(before, after)

            return result

        except json.JSONDecodeError:
            pass

        # JSON was truncated — extract content arrays directly with regex
        print(f"[Analyzer] Truncated JSON — extracting content lists directly")

        def extract_array(key: str) -> list[str]:
            pattern = rf'"{key}"\s*:\s*\['
            m = re.search(pattern, text)
            if not m:
                return []
            array_start = m.end() - 1
            items = []
            for match in re.finditer(r'"([^"]+)"', text[array_start:]):
                val = match.group(1)
                # Stop if we've hit a key name rather than a value
                if val in ("before_contents", "after_contents", "changes",
                           "item", "action", "added", "removed"):
                    break
                items.append(val)
            return items

        before = extract_array("before_contents")
        after  = extract_array("after_contents")
        print(f"[Analyzer] Recovered — before: {len(before)}, after: {len(after)}")

        return {
            "before_contents": before,
            "after_contents":  after,
            "changes":         self._diff_contents(before, after),
        }

    def _diff_contents(self, before: list[str], after: list[str]) -> list[dict]:
        """
        Derive the changes array by diffing before/after content lists.
        Uses a counter-based approach so duplicate items are handled correctly.
        This replaces reliance on the model's own changes array, which is
        frequently wrong or truncated even when the content lists are correct.
        """
        from collections import Counter

        def normalize(desc: str) -> str:
            # Strip stopwords so minor description variations still match
            stopwords = {
                "and", "or", "a", "an", "the", "with", "of", "in", "on",
                "small", "large", "big", "tall", "short", "dark", "light",
                "bright", "clear", "red", "orange", "yellow", "green", "blue",
                "purple", "pink", "black", "white", "grey", "gray", "silver",
                "gold", "brown", "transparent",
            }
            words = [w for w in desc.lower().split() if w not in stopwords]
            return " ".join(sorted(words))

        before_counts = Counter(normalize(d) for d in before)
        after_counts  = Counter(normalize(d) for d in after)

        changes = []

        # Items that decreased in count -> removed
        for norm, count in before_counts.items():
            delta = count - after_counts.get(norm, 0)
            orig = next((d for d in before if normalize(d) == norm), norm)
            for _ in range(delta):
                changes.append({"item": orig, "action": "removed"})

        # Items that increased in count -> added
        for norm, count in after_counts.items():
            delta = count - before_counts.get(norm, 0)
            orig = next((d for d in after if normalize(d) == norm), norm)
            for _ in range(delta):
                changes.append({"item": orig, "action": "added"})

        return changes

    def analyze(self, before: np.ndarray, after: np.ndarray,
                inventory: list[dict] = None) -> dict:
        client     = self._get_client()
        before_uri = self._encode(before)
        after_uri  = self._encode(after)
        prompt     = self._build_prompt(inventory or [])

        for attempt in range(4):
            try:
                print(f"[Analyzer] Sending to {self.model} (attempt {attempt + 1})...")
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": before_uri}},
                                {"type": "image_url", "image_url": {"url": after_uri}},
                            ],
                        }
                    ],
                    temperature=0.1,
                    max_tokens=4096,
                )
                result = self._parse(response.choices[0].message.content)
                print(f"[Analyzer] Response: "
                      f"{response.choices[0].message.content[:500]}")
                return result

            except Exception as e:
                err = str(e)
                print(f"[Analyzer] {type(e).__name__}: {err[:150]}")
                if any(x in err for x in ("429", "rate_limit")):
                    wait = 10.0 * (2 ** attempt)
                    print(f"[Analyzer] Rate limited — waiting {wait:.0f}s...")
                    time.sleep(wait)
                elif any(x in err for x in ("503", "502", "unavailable", "overloaded")):
                    wait = 5.0 * (2 ** attempt)
                    print(f"[Analyzer] Overloaded — waiting {wait:.0f}s...")
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError("Groq API failed after all retries.")

    def analyze_mock(self) -> dict:
        return {
            "before_contents": ["small cylindrical can, blue and silver",
                                 "tall dark bottle lying on its side"],
            "after_contents":  ["tall dark bottle lying on its side"],
            "changes": [
                {"item": "small cylindrical can, blue and silver",
                 "action": "removed"},
            ],
        }