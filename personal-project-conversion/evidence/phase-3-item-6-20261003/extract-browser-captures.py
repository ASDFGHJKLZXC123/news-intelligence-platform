"""Retain every labeled native CUA output; exclude call arguments and credentials.

Images belong to their native function-call output, not automatically to every
embedded CAPTURE label. Sequential DOM observations may precede the image moment.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

# A label ends before an adjacent '- complementary' tree, uppercase innerText,
# punctuation, or whitespace. A trailing hyphen cannot be part of the label.
CAPTURE = re.compile(r"CAPTURE ([a-z0-9]+(?:-[a-z0-9]+)*)")


def verify_secret_safe(value: str) -> None:
    """Reject explicit same-line or indented multiline access-key textbox values."""
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r'textbox[^\n]*API access key', line, re.I):
            continue
        indentation = len(line) - len(line.lstrip())
        block = [line]
        for child in lines[index + 1:]:
            if child.strip() and len(child) - len(child.lstrip()) <= indentation:
                break
            block.append(child)
        for element in block:
            match = re.search(r'\btext:\s*(.*)$', element, re.I)
            if match:
                text_value = match.group(1).strip().strip('"').strip("'")
                if text_value and text_value != "<redacted>":
                    raise RuntimeError("native capture contains an unredacted access-key value")
    if re.search(r'(?i)\bBearer\s+[A-Za-z0-9_.-]{16,}', value):
        raise RuntimeError("native capture contains an unredacted bearer credential")


def main() -> None:
    session = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    out.mkdir(exist_ok=True)
    captures, function_calls, images = [], [], []
    for line_number, line in enumerate(session.read_text().splitlines(), 1):
        row = json.loads(line)
        payload = row.get("payload", {})
        if row.get("type") != "response_item" or payload.get("type") != "function_call_output":
            continue
        content = payload.get("output")
        if not isinstance(content, list):
            continue
        text_parts = [part.get("text", "") for part in content
                      if isinstance(part, dict) and part.get("type") == "input_text"]
        combined = "\n".join(text_parts)
        matches = list(CAPTURE.finditer(combined))
        if not matches:
            continue
        call_id = payload.get("call_id")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", call_id or f"line-{line_number}")
        labels = [match.group(1) for match in matches]
        verify_secret_safe(combined)
        call_image_files = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_image":
                continue
            data = part.get("image_url")
            if isinstance(data, dict):
                data = data.get("url")
            if not isinstance(data, str) or not data.startswith("data:image/"):
                continue
            header, encoded = data.split(",", 1)
            extension = ".jpg" if "jpeg" in header else ".png"
            filename = f"{safe_id}-image-{len(call_image_files) + 1}{extension}"
            raw = base64.b64decode(encoded, validate=True)
            (out / filename).write_bytes(raw)
            call_image_files.append(filename)
            images.append({
                "file": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                "call_id": call_id, "session_line": line_number,
                "observed_at": row.get("timestamp"), "embedded_labels": labels,
                "scope": "native_function_call_output", "label_attribution": "unassigned",
                "snapshot_moment_qualification": (
                    "The image is retained from this function-call output. It is not a separate "
                    "screenshot for each embedded label, and a preceding DOM label may represent "
                    "an earlier/loading state. Verify image content before attributing a completed UI state."
                ),
            })
        call_record = {
            "call_id": call_id, "session_line": line_number,
            "observed_at": row.get("timestamp"), "embedded_labels": labels,
            "images": call_image_files,
        }
        if matches[0].start():
            prefix = combined[:matches[0].start()].strip()
            if prefix:
                filename = f"{safe_id}-unlabeled-prefix.txt"
                (out / filename).write_text(prefix + "\n")
                call_record["unlabeled_prefix"] = filename
        function_calls.append(call_record)
        for index, match in enumerate(matches):
            label = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(combined)
            # Preserve all native text, including innerText and inspected JS objects.
            segment = combined[match.start():end].rstrip() + "\n"
            occurrence = sum(capture["label"] == label for capture in captures)
            name = label if not occurrence else f"{label}-occurrence-{occurrence + 1}"
            filename = name + ".dom.txt"
            (out / filename).write_text(segment)
            captures.append({
                "label": label, "session_line": line_number,
                "observed_at": row.get("timestamp"), "call_id": call_id,
                "label_ordinal_within_call": index + 1, "dom": filename,
                "dom_sha256": hashlib.sha256(segment.encode()).hexdigest(),
                "screenshots": [], "function_call_image_refs": call_image_files,
                "snapshot_state": "requires_content_inspection",
                "screenshot_label_attribution": "unassigned_function_call_scope",
            })
    session_rows = [json.loads(line) for line in session.read_text().splitlines()]
    session_id = next((row.get("payload", {}).get("id") for row in session_rows
                       if row.get("type") == "session_meta"), None)
    index = {"schema": "item6-native-browser-captures.v2", "session_id": session_id,
             "session_sha256": hashlib.sha256(session.read_bytes()).hexdigest(),
             "source": "native function_call_output only; no function call arguments",
             "captures": captures, "function_calls": function_calls, "images": images,
             "previous_extraction_files": (
                 "Older extraction files may remain on disk. Only files referenced by this v2 index "
                 "describe the complete current extraction; call-scope images replace prior label-attributed image names."
             )}
    (out / "capture-index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(json.dumps({"captured_labels": len(captures), "function_calls": len(function_calls),
                      "images": len(images)}))


if __name__ == "__main__":
    main()
