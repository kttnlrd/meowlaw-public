import json
import os
from collections import defaultdict
import re
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CASE_DIR = os.path.expanduser(os.environ.get("ASIC_CASES_DIR", "~/projects/asic-scraper/cases"))
RULING_DIR = os.path.join(DATA_DIR, "rulings")
OUTPUT_FILE = os.path.join(DATA_DIR, "smartlink_index.json")

CITATION_INDEX_PATH = os.path.join(DATA_DIR, "citation_index.json")
RULING_SECTION_INDEX_PATH = os.path.join(DATA_DIR, "ruling_section_index.json")


def load_json(filepath):
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r') as f:
        return json.load(f)


def save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


def extract_case_id(filename):
    return os.path.splitext(filename)[0].replace('_', ' ').strip()


def extract_ruling_id(filename):
    base = os.path.splitext(filename)[0]
    # TR_2023_2 -> TR 2023/2
    parts = base.split('_')
    if len(parts) >= 3 and parts[0] in ('TR', 'TD', 'PCG', 'ATO', 'CR', 'PR'):
        return f"{parts[0]} {parts[1]}/{parts[2]}"
    return base


def classify_item_id(item_id):
    if item_id.startswith('['):
        return "case"
    if item_id.startswith(('TR ', 'TD ', 'PCG ', 'ATO ', 'CR ', 'PR ')):
        return "ruling"
    if ' part ' in item_id or ' division ' in item_id:
        return "part"
    return "section"


def parse_section_key(key):
    """Parse 'itaa-1997#6-5' into ('itaa-1997', '6-5')"""
    if '#' in key:
        return key.split('#', 1)
    return None, None


def build_smartlink_index():
    print("Loading data...")
    citation_index = load_json(CITATION_INDEX_PATH)
    ruling_section_index = load_json(RULING_SECTION_INDEX_PATH)

    # Load cases
    case_files = [f for f in os.listdir(CASE_DIR) if f.endswith('.json')]
    all_cases = {}
    for fname in case_files:
        case_id = extract_case_id(fname)
        all_cases[case_id] = load_json(os.path.join(CASE_DIR, fname))
    print(f"Loaded {len(all_cases)} cases.")

    # Load rulings
    ruling_files = [f for f in os.listdir(RULING_DIR) if f.endswith('.txt')]
    all_rulings = {}
    for fname in ruling_files:
        ruling_id = extract_ruling_id(fname)
        all_rulings[ruling_id] = {"id": ruling_id, "file": fname}
    print(f"Loaded {len(all_rulings)} rulings.")

    # Load sections and parts from tree.json files
    all_sections = {}  # key -> {"act": ..., "section": ..., "title": ...}
    all_parts = {}     # key -> {"act": ..., "id": ..., "title": ..., "type": "part|division"}
    section_to_part = {}  # section_key -> part_key

    for act_folder in os.listdir(DATA_DIR):
        act_path = os.path.join(DATA_DIR, act_folder)
        tree_path = os.path.join(act_path, "tree.json")
        if not os.path.isdir(act_path) or not os.path.exists(tree_path):
            continue

        tree = load_json(tree_path)
        parts = tree.get("parts", [])

        for part in parts:
            part_id = part.get("id", "")
            part_key = f"{act_folder}#part#{part_id}"
            all_parts[part_key] = {"act": act_folder, "id": part_id, "title": part.get("title", ""), "type": "part"}

            # Sections directly under part
            for sec in part.get("sections", []):
                sec_id = sec.get("id", "")
                sec_key = f"{act_folder}#{sec_id}"
                all_sections[sec_key] = {"act": act_folder, "section": sec_id, "title": sec.get("title", "")}
                section_to_part[sec_key] = part_key

            # Divisions under part
            for div in part.get("divisions", []):
                div_id = div.get("id", "")
                div_key = f"{act_folder}#division#{div_id}"
                all_parts[div_key] = {"act": act_folder, "id": div_id, "title": div.get("title", ""), "type": "division"}

                for sec in div.get("sections", []):
                    sec_id = sec.get("id", "")
                    sec_key = f"{act_folder}#{sec_id}"
                    all_sections[sec_key] = {"act": act_folder, "section": sec_id, "title": sec.get("title", "")}
                    section_to_part[sec_key] = div_key

                for sub in div.get("subdivisions", []):
                    for sec in sub.get("sections", []):
                        sec_id = sec.get("id", "")
                        sec_key = f"{act_folder}#{sec_id}"
                        all_sections[sec_key] = {"act": act_folder, "section": sec_id, "title": sec.get("title", "")}
                        section_to_part[sec_key] = div_key

    print(f"Loaded {len(all_sections)} sections and {len(all_parts)} parts/divisions.")

    # --- Build citation maps ---
    item_citations = defaultdict(set)  # item -> set of items it cites
    item_cited_by = defaultdict(set)   # item -> set of items that cite it

    # From citation_index: sections cite cases and rulings
    for act_id, sections_data in citation_index.items():
        for section_id, citations_list in sections_data.items():
            source_key = f"{act_id}#{section_id}"
            for c in citations_list:
                ctype = c.get("type")
                target = c.get("citation")
                if ctype in ("case", "ruling") and target:
                    item_citations[source_key].add(target)
                    item_cited_by[target].add(source_key)

    # From ruling_section_index: rulings cite sections
    for ruling_id, sections_list in ruling_section_index.items():
        for s in sections_list:
            act_id = s.get("act", "")
            sec_id = s.get("section", "")
            if act_id and sec_id:
                target_key = f"{act_id}#{sec_id}"
                item_citations[ruling_id].add(target_key)
                item_cited_by[target_key].add(ruling_id)

    print(f"Citation map: {len(item_citations)} items cite others, {len(item_cited_by)} items are cited.")

    # --- Compute smart links ---
    print("Computing smart links...")

    def compute_links(target_id, target_info):
        scores = defaultdict(float)
        reasons = defaultdict(list)
        target_type = target_info.get("type", "unknown")

        for cand_id, cand_info in all_candidates.items():
            if cand_id == target_id:
                continue
            cand_type = cand_info.get("type", "unknown")
            score = 0.0
            reason_list = []

            # 1. Direct citation (candidate cites target)
            if target_id in item_citations.get(cand_id, set()):
                score += 1.0
                reason_list.append("cited by this item")

            # 2. Back-reference (target cites candidate)
            if cand_id in item_citations.get(target_id, set()):
                score += 0.6
                reason_list.append("cites this item")

            # 3. Shared citations (both cite >=2 same items)
            common = item_citations.get(target_id, set()) & item_citations.get(cand_id, set())
            if len(common) >= 2:
                score += 0.7
                reason_list.append(f"shares {len(common)} citations")

            # 4. Structural sibling
            if target_type == "section" and cand_type == "section":
                if target_info.get("act") == cand_info.get("act"):
                    tp = section_to_part.get(target_id)
                    cp = section_to_part.get(cand_id)
                    if tp and cp and tp == cp:
                        score += 0.4
                        reason_list.append("same part/division")
            elif target_type == "case" and cand_type == "case":
                ty = re.search(r'\[(\d{4})\]', target_id)
                cy = re.search(r'\[(\d{4})\]', cand_id)
                if ty and cy and ty.group(1) == cy.group(1):
                    score += 0.4
                    reason_list.append("same year")
            elif target_type == "ruling" and cand_type == "ruling":
                ty = re.search(r'(\d{4})', target_id)
                cy = re.search(r'(\d{4})', cand_id)
                if ty and cy and ty.group(1) == cy.group(1):
                    score += 0.4
                    reason_list.append("same year")

            # 5. Cross-type bonus
            if target_type != cand_type:
                score += 0.2
                reason_list.append("related type")

            if score > 0:
                scores[cand_id] = min(score, 1.5)
                reasons[cand_id] = reason_list

        # Sort and cap
        sorted_links = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        final = []
        type_counts = defaultdict(int)

        for link_id, link_score in sorted_links:
            lt = all_candidates[link_id]["type"]
            if type_counts[lt] < 3:
                final.append({
                    "id": link_id,
                    "type": lt,
                    "score": round(link_score, 2),
                    "reason": ", ".join(reasons[link_id])
                })
                type_counts[lt] += 1
            if len(final) >= 10:
                break
        return final

    # Build candidate pool
    all_candidates = {}
    for k, v in all_sections.items():
        all_candidates[k] = {**v, "type": "section"}
    for k, v in all_cases.items():
        all_candidates[k] = {"type": "case", "title": v.get("case_name", "")}
    for k, v in all_rulings.items():
        all_candidates[k] = {"type": "ruling", "title": k}
    for k, v in all_parts.items():
        all_candidates[k] = {**v, "type": "part"}

    smartlink_index = {"sections": {}, "cases": {}, "rulings": {}, "parts": {}}

    # Sections
    for sec_key, sec_info in all_sections.items():
        act_id, sec_id = sec_key.split('#', 1)
        links = compute_links(sec_key, all_candidates[sec_key])
        if links:
            if act_id not in smartlink_index["sections"]:
                smartlink_index["sections"][act_id] = {}
            smartlink_index["sections"][act_id][sec_id] = links

    # Cases
    for case_id in all_cases:
        links = compute_links(case_id, all_candidates[case_id])
        if links:
            smartlink_index["cases"][case_id] = links

    # Rulings
    for ruling_id in all_rulings:
        links = compute_links(ruling_id, all_candidates[ruling_id])
        if links:
            smartlink_index["rulings"][ruling_id] = links

    # Parts
    for part_key, part_info in all_parts.items():
        act_id = part_info["act"]
        part_id = part_info["id"]
        links = compute_links(part_key, all_candidates[part_key])
        if links:
            if act_id not in smartlink_index["parts"]:
                smartlink_index["parts"][act_id] = {}
            smartlink_index["parts"][act_id][part_id] = links

    save_json(OUTPUT_FILE, smartlink_index)

    sec_total = sum(len(v) for v in smartlink_index["sections"].values())
    case_total = len(smartlink_index["cases"])
    ruling_total = len(smartlink_index["rulings"])
    part_total = sum(len(v) for v in smartlink_index["parts"].values())
    print(f"Done. Sections: {sec_total}, Cases: {case_total}, Rulings: {ruling_total}, Parts: {part_total}")


if __name__ == "__main__":
    build_smartlink_index()
