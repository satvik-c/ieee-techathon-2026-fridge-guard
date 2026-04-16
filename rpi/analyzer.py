"""
FridgeGuard — Analyzer module (Groq vision, single pass).

Injects current inventory into prompt so Groq uses exact stored
descriptions for removals, and includes item count so Groq doesn't
hallucinate duplicates.
"""

import base64
import io
import json
import time

import numpy as np
from PIL import Image


SEND_WIDTH = 1024

BASE_PROMPT = """\
You are a visual change detector for a refrigerator.
Photo 1 is BEFORE. Photo 2 is AFTER.

CONTEXT:
- Blue LED lighting makes both photos look dark and blue-tinted. This is normal — do not treat it as a difference.
- There is a bright light at the back of the fridge. Objects near it may look washed out but are still real solid objects.
- The camera is wide-angle. Objects at the edges and corners of the frame are real — do not ignore them.
- If one object is partially behind another, they are TWO separate objects. If both disappear, that is TWO removals.
- BACKGROUND LIGHT does not move or change between photos — if something near it disappeared, it was a real object.
- Objects may shift slightly in position between photos — this is irrelevant. Only care about objects that completely disappear or newly appear.
- PARTIALLY OBSCURED OBJECTS: count every distinct object even if only partially visible.

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
            text = text[start:end + 1]

        try:
            result = json.loads(text)
            result.setdefault("before_contents", [])
            result.setdefault("after_contents",  [])
            result.setdefault("changes", [])
            return result
        except json.JSONDecodeError as e:
            print(f"[Analyzer] JSON parse failed: {e}")
            print(f"[Analyzer] Raw: {text[:300]}")
            raise

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
                                {"type": "text",      "text": prompt},
                                {"type": "image_url", "image_url": {"url": before_uri}},
                                {"type": "image_url", "image_url": {"url": after_uri}},
                            ],
                        }
                    ],
                    temperature=0.1,
                    max_tokens=1024,
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