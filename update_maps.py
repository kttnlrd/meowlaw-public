#!/usr/bin/env python3
"""Update MAP A and MAP B for subsection completeness gate standard.
Idempotent: reads fresh from file each time, detects if already updated.
"""

import json
import re
import sys

def strip_subsection_ref(sec):
    """Strip trailing parenthesized subsection suffixes like (1), (1A), (2)(a)."""
    m = re.match(r'^(\d+-\d+(?:[A-Z])?)(?:\([^)]*\))*$', sec)
    if m:
        return m.group(1)
    return sec

def clean_statute_refs(nodes):
    """Clean all statute references to use base section numbers only."""
    changed = False
    for node in nodes:
        for stat in node.get("statute", []):
            original = stat["section"]
            cleaned = strip_subsection_ref(original)
            if cleaned != original:
                stat["section"] = cleaned
                changed = True
    return nodes, changed

def has_node(nodes, nid):
    return any(n["id"] == nid for n in nodes)

def has_edge(edges, frm, to):
    return any(e["from"] == frm and e["to"] == to for e in edges)

def validate_graph(nodes, edges):
    """Validate the graph structure."""
    errors = []
    node_ids = {n["id"] for n in nodes}
    
    for e in edges:
        if e["from"] not in node_ids:
            errors.append(f"Edge from '{e['from']}' references non-existent node")
        if e["to"] not in node_ids:
            errors.append(f"Edge to '{e['to']}' references non-existent node")
    
    # Check all nodes are reachable from 'start'
    reachable = set()
    stack = ["start"]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        for e in edges:
            if e["from"] == node and e["to"] not in reachable:
                stack.append(e["to"])
    
    for nid in node_ids:
        if nid not in reachable:
            errors.append(f"Node '{nid}' is not reachable from 'start'")
    
    # Check for non-terminal dead ends
    for n in nodes:
        nid = n["id"]
        if n["type"] in ("end", "outcome"):
            continue
        has_out = any(e["from"] == nid for e in edges)
        if not has_out:
            errors.append(f"Node '{nid}' (type={n['type']}) has no outgoing edges")
    
    return errors

def update_map_a():
    with open("data/maps/itaa-1997-subdiv-122a.json") as f:
        ma = json.load(f)
    
    nodes = ma["nodes"]
    edges = ma["edges"]
    changed = False
    
    # 1. Clean statute refs (strip subsection suffixes)
    nodes, refs_changed = clean_statute_refs(nodes)
    if refs_changed:
        changed = True
    
    # 2. Add early decision node: entity type boundary assertion (s 122-15)
    if not has_node(nodes, "dec-entity-type"):
        dec_entity = {
            "id": "dec-entity-type",
            "type": "decision",
            "label": "Are you an individual or a trustee? (Not a company, partnership or super fund)",
            "body": "The roll-over is only available to an individual or a trustee. Companies, partnerships and superannuation funds cannot use this roll-over (s 122-15).",
            "statute": [
                {"act": "itaa-1997", "section": "122-15", "title": "Disposal or creation of assets—wholly-owned company"}
            ],
            "commentary": ["when-cgt-roll-over-is-available"],
            "cases": [],
            "definitions": []
        }
        # Insert after start node
        start_idx = next(i for i, n in enumerate(nodes) if n["id"] == "start")
        nodes.insert(start_idx + 1, dec_entity)
        
        # Rewire: start → dec-entity-type, dec-entity-type → trigger-event / no-rollover
        new_edges = []
        for e in edges:
            if e["from"] == "start" and e["to"] == "trigger-event":
                new_edges.append({"from": "start", "to": "dec-entity-type", "label": "transfer to company"})
                if not has_edge(edges, "dec-entity-type", "trigger-event"):
                    new_edges.append({"from": "dec-entity-type", "to": "trigger-event", "label": "Yes — individual or trustee"})
                if not has_edge(edges, "dec-entity-type", "no-rollover"):
                    new_edges.append({"from": "dec-entity-type", "to": "no-rollover", "label": "No — entity not eligible"})
            else:
                new_edges.append(e)
        edges = new_edges
        changed = True
    
    # Also rewire if edges from start already point to dec-entity-type but missing the no-rollover or trigger-event edges
    if has_node(nodes, "dec-entity-type"):
        if not has_edge(edges, "dec-entity-type", "trigger-event"):
            edges.append({"from": "dec-entity-type", "to": "trigger-event", "label": "Yes — individual or trustee"})
            changed = True
        if not has_edge(edges, "dec-entity-type", "no-rollover"):
            edges.append({"from": "dec-entity-type", "to": "no-rollover", "label": "No — entity not eligible"})
            changed = True
    
    # 3. Add recapture events node
    if not has_node(nodes, "recapture-events"):
        recapture = {
            "id": "recapture-events",
            "type": "outcome",
            "label": "Recapture events: CGT events J2, J5, J6 may later bring deferred gain to account",
            "body": "CGT event J2 (s 104-130): if a non-redeemable share received under the roll-over is converted into a redeemable share, a capital gain arises equal to the deferred amount. CGT event J5 (s 104-175): failure to execute a roll-over agreement. CGT event J6 (s 104-180): reversal of a roll-over. These events ensure the deferred gain is ultimately brought to account.",
            "statute": [
                {"act": "itaa-1997", "section": "104-130", "title": "CGT event J2"},
                {"act": "itaa-1997", "section": "104-175", "title": "CGT event J5"},
                {"act": "itaa-1997", "section": "104-180", "title": "CGT event J6"}
            ],
            "commentary": [],
            "cases": [],
            "definitions": ["capital gain", "redeemable shares"]
        }
        nodes.append(recapture)
        # Rewire: outcome→company nodes now go to recapture-events instead of end
        new_edges = []
        for e in edges:
            if e["from"] in ("outcome-company-disposal", "outcome-company-creation") and e["to"] == "end":
                new_edges.append({"from": e["from"], "to": "recapture-events", "label": "company consequences complete"})
            else:
                new_edges.append(e)
        if not has_edge(edges, "recapture-events", "end"):
            new_edges.append({"from": "recapture-events", "to": "end", "label": ""})
        edges = new_edges
        changed = True
    
    # Ensure recapture-events → end edge exists
    if has_node(nodes, "recapture-events") and not has_edge(edges, "recapture-events", "end"):
        edges.append({"from": "recapture-events", "to": "end", "label": ""})
        changed = True
    
    # 4. Add Subdivision 328-G interplay node
    if not has_node(nodes, "subdiv-328g-interplay"):
        subdiv_328g = {
            "id": "subdiv-328g-interplay",
            "type": "outcome",
            "label": "Alternative: Subdivision 328-G small business restructure roll-over",
            "body": "If the Subdivision 122-A roll-over is not available (e.g. entity not eligible, assets excluded), consider Subdivision 328-G (Restructures of small businesses). It provides a broader roll-over for CGT assets, trading stock, revenue assets and depreciating assets when a small business restructures, without the 100% ownership requirement. The choice under s 103-25 applies separately.",
            "statute": [
                {"act": "itaa-1997", "section": "328-G", "title": "Restructures of small businesses"}
            ],
            "commentary": ["cgt-roll-over-for-business-restructures"],
            "cases": [],
            "definitions": ["small business entity"]
        }
        nodes.append(subdiv_328g)
        if not has_edge(edges, "no-rollover", "subdiv-328g-interplay"):
            edges.append({"from": "no-rollover", "to": "subdiv-328g-interplay", "label": "consider small business restructure"})
        changed = True
    
    # Ensure no-rollover → subdiv-328g-interplay edge exists
    if has_node(nodes, "subdiv-328g-interplay") and not has_edge(edges, "no-rollover", "subdiv-328g-interplay"):
        edges.append({"from": "no-rollover", "to": "subdiv-328g-interplay", "label": "consider small business restructure"})
        changed = True
    
    # 5. Update summary
    new_summary = ("An individual or trustee can defer CGT on transferring a CGT asset, or all the assets of a business, to a company they wholly own (or on creating a CGT asset in that company), by choosing a roll-over. The gain/loss is disregarded and the CGT cost base moves into the shares received (and into the assets in the company's hands). The map below walks the eligibility conditions in order, then the consequences. Subsection coverage: operative sections 122-15, 122-20, 122-25, 122-35, 122-37, 122-40, 122-50, 122-55, 122-60, 122-65, 122-70, 122-75, 103-25 mapped to nodes; 103-25 (machinery); 328-G (transitional).")
    if ma["summary"] != new_summary:
        ma["summary"] = new_summary
        changed = True
    
    ma["nodes"] = nodes
    ma["edges"] = edges
    return ma, changed


def update_map_b():
    with open("data/maps/itaa-1997-subdiv-124b.json") as f:
        mb = json.load(f)
    
    nodes = mb["nodes"]
    edges = mb["edges"]
    changed = False
    
    # 1. Clean statute refs
    nodes, refs_changed = clean_statute_refs(nodes)
    if refs_changed:
        changed = True
    
    # 2. Add recapture events node
    if not has_node(nodes, "recapture-events"):
        recapture = {
            "id": "recapture-events",
            "type": "outcome",
            "label": "Recapture events: CGT events J2, J5, J6 may later bring deferred gain to account",
            "body": "If shares or interests received as replacement assets under a Subdivision 124-B roll-over are later disposed of, CGT events J2 (s 104-130 — share becomes redeemable), J5 (s 104-175 — failure to execute agreement) or J6 (s 104-180 — reversal of roll-over) may bring the deferred gain to account. Also, CGT event A1 (s 104-10) on the eventual disposal of the replacement asset will account for the deferred amount through the reduced cost base.",
            "statute": [
                {"act": "itaa-1997", "section": "104-130", "title": "CGT event J2"},
                {"act": "itaa-1997", "section": "104-175", "title": "CGT event J5"},
                {"act": "itaa-1997", "section": "104-180", "title": "CGT event J6"}
            ],
            "commentary": [],
            "cases": [],
            "definitions": ["capital gain"]
        }
        nodes.append(recapture)
        # Rewire outcome → end to outcome → recapture → end
        new_edges = []
        for e in edges:
            if e["to"] == "end" and e["from"] in ("outcome-money", "outcome-asset", "outcome-both"):
                new_edges.append({"from": e["from"], "to": "recapture-events", "label": e["label"]})
            else:
                new_edges.append(e)
        new_edges.append({"from": "recapture-events", "to": "end", "label": ""})
        edges = new_edges
        changed = True
    
    # Ensure recapture-events → end edge exists
    if has_node(nodes, "recapture-events") and not has_edge(edges, "recapture-events", "end"):
        edges.append({"from": "recapture-events", "to": "end", "label": ""})
        changed = True
    
    # 3. Update summary
    new_summary = ("Replacement-asset roll-over where a CGT asset is compulsorily acquired by a government agency (or under a covered law), lost or destroyed, or disposed of after an acquisition notice. You must receive money or another CGT asset as compensation and, for money, acquire a replacement asset or repair the original within the time limits (1 year before / 1 year after the income year). The gain is reduced or disregarded and the replacement asset inherits the original's cost base. Subsection coverage: operative sections 124-70, 124-75, 124-80, 124-85, 124-90, 124-95, 103-25 mapped to nodes; 103-25 (machinery).")
    if mb["summary"] != new_summary:
        mb["summary"] = new_summary
        changed = True
    
    mb["nodes"] = nodes
    mb["edges"] = edges
    return mb, changed


if __name__ == "__main__":
    print("Updating MAP A (Subdiv 122-A)...")
    ma, changed_a = update_map_a()
    errs = validate_graph(ma["nodes"], ma["edges"])
    if errs:
        print(f"MAP A validation errors: {errs}")
        sys.exit(1)
    print(f"MAP A: {len(ma['nodes'])} nodes, {len(ma['edges'])} edges — {'changed' if changed_a else 'no changes needed'}, valid")
    
    print("Updating MAP B (Subdiv 124-B)...")
    mb, changed_b = update_map_b()
    errs = validate_graph(mb["nodes"], mb["edges"])
    if errs:
        print(f"MAP B validation errors: {errs}")
        sys.exit(1)
    print(f"MAP B: {len(mb['nodes'])} nodes, {len(mb['edges'])} edges — {'changed' if changed_b else 'no changes needed'}, valid")
    
    # Re-read original files to make sure we're writing fresh
    with open("data/maps/itaa-1997-subdiv-122a.json") as f:
        original_a = json.load(f)
    with open("data/maps/itaa-1997-subdiv-124b.json") as f:
        original_b = json.load(f)
    
    # Only write if changed
    if changed_a:
        with open("data/maps/itaa-1997-subdiv-122a.json", "w") as f:
            json.dump(ma, f, indent=2)
            f.write("\n")
        print("  -> Wrote MAP A")
    else:
        print("  -> MAP A already up to date")
    
    if changed_b:
        with open("data/maps/itaa-1997-subdiv-124b.json", "w") as f:
            json.dump(mb, f, indent=2)
            f.write("\n")
        print("  -> Wrote MAP B")
    else:
        print("  -> MAP B already up to date")
    
    # Verify the written files parse correctly
    print("\nVerifying written files...")
    for fn in ["data/maps/itaa-1997-subdiv-122a.json", "data/maps/itaa-1997-subdiv-124b.json"]:
        with open(fn) as f:
            d = json.load(f)
        errs = validate_graph(d["nodes"], d["edges"])
        if errs:
            print(f"  {fn}: VALIDATION FAILED: {errs}")
        else:
            print(f"  {fn}: {len(d['nodes'])} nodes, {len(d['edges'])} edges — valid")
    
    print("\nDone.")