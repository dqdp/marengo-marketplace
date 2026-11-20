#!/usr/bin/env python3
"""Emit a host-safe offer card for a deeper-analysis capability.

    python3 offer.py "<command>" "<label>" [--note "..."]
    e.g. python3 offer.py "/equity-research:earnings ORCL Q4 FY25" "Deeper earnings report →"

Claude builds preserve their slash-command behavior. OpenAI builds never consult Claude's plugin
registry: stdout is only a non-clickable compatibility fallback. The installed companion MCP App
owns the actual native click-to-continue card.
Pure stdlib."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard  # noqa: E402  (offer_widget lives with the other agent-surface helpers)


# The OpenAI companion card has carried a report-language choice since the port; the Claude card
# had no way to express one, so the SAME offer produced a Russian deliverable on one host and an
# English one on the other. These strings are copied VERBATIM from the companion server's
# LANGUAGE_INSTRUCTIONS (packaging/openai/financial-deep-analysis/mcp/offer_server.py) so both
# hosts hand the document skill the same instruction; test_openai_port.py fails if they drift.
LANGUAGE_INSTRUCTIONS = {
    "en": (
        " Write the complete deliverable in English, including the narrative, tables, "
        "chart labels, captions, and any PDF. Keep company names, tickers, and source "
        "titles in their original form."
    ),
    "ru": (
        " Write the complete deliverable in Russian, including the narrative, tables, "
        "chart labels, captions, and any PDF. Keep company names, tickers, and source "
        "titles in their original form."
    ),
}


def in_language(prompt, lang):
    """Append the report-language instruction. No language asked for -> untouched (opt-in)."""
    return prompt + LANGUAGE_INSTRUCTIONS[lang] if lang else prompt


def packaged_host(_root=None):
    """Return the build-selected host. No runtime surface auto-detection."""
    root = _root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "market-data-host.json")
    try:
        with open(path, encoding="utf-8") as f:
            host = json.load(f).get("host")
        return "openai" if host == "openai" else "claude"
    except Exception:
        return "claude"


def plugin_installed(namespace, _path=None):
    """True if a plugin whose name matches `namespace` is in ~/.claude/plugins/installed_plugins.json.
    Robust to a missing / malformed manifest (returns False). `_path` is a test seam. In cloud Cowork the
    manifest is not mounted -> False -> the honest desktop-fallback card."""
    path = _path or os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(path, encoding="utf-8") as f:
            for key in (json.load(f).get("plugins") or {}):
                if isinstance(key, str) and key.split("@", 1)[0] == namespace:
                    return True
    except Exception:
        pass
    return False


def derive_namespace(command):
    """Plugin namespace from the COMMAND token only — '/equity-research:earnings ORCL 10:30' is
    'equity-research'; a colon in the ARGS must not garble it (v1.125). '' when not namespaced."""
    head = (command.split() or [""])[0]
    return head.lstrip("/").split(":", 1)[0] if (head.startswith("/") and ":" in head) else ""


# "namespace:command" -> the backing SKILL dir the Cowork mirror ships (skills, not commands);
# ns-keyed (re-review P4) so a wrong-namespace typo never maps to the other plugin's skill
_CMD_SKILL = {"equity-research:earnings": "earnings-analysis",
              "equity-research:earnings-preview": "earnings-preview",
              "equity-research:sector": "sector-overview",
              "equity-research:initiate": "initiating-coverage",
              "equity-research:thesis": "thesis-tracker",
              "equity-research:morning-note": "morning-note",
              "equity-research:catalysts": "catalyst-calendar",
              "equity-research:model-update": "model-update",
              "equity-research:screen": "idea-generation",
              "financial-analysis:comps": "comps-analysis",
              "financial-analysis:dcf": "dcf-model",
              "financial-analysis:lbo": "lbo-model",
              "financial-analysis:competitive-analysis": "competitive-analysis"}


def mapped_skill(command):
    head = (command.split() or [""])[0]
    if not (head.startswith("/") and ":" in head):
        return None
    return _CMD_SKILL.get(head.lstrip("/"))


def openai_skill_prompt(command, available_skill=None):
    """Translate a Claude capability token into the companion OpenAI skill prompt."""
    mapped = mapped_skill(command)
    if not mapped:
        return None
    if available_skill:
        available_base = available_skill.rsplit(":", 1)[-1]
        if available_base != mapped:
            return None
        full_name = (available_skill if ":" in available_skill
                     else f"financial-deep-analysis:{mapped}")
    else:
        full_name = f"financial-deep-analysis:{mapped}"
    args = command.split(None, 1)[1] if len(command.split(None, 1)) > 1 else ""
    return f"Use ${full_name}" + (f" for: {args}" if args else "")


def mirrored_skill(command, _skills_dir=None):
    """v1.126 (the local-Cowork offer): the plugin manifest is NOT mounted in Cowork, but
    prewarm_sync's mirror_plugins ships the plugins' SKILLS beside this one — so the offer can
    still be ONE-CLICK there. Returns the mirrored skill name backing `command` when its
    SKILL.md exists in the sibling skills dir, else None. `_skills_dir` is the test seam."""
    head = (command.split() or [""])[0]
    if not (head.startswith("/") and ":" in head):
        return None
    name = _CMD_SKILL.get(head.lstrip("/"))
    if not name:
        return None
    skills = _skills_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return name if os.path.isfile(os.path.join(skills, name, "SKILL.md")) else None


def main():
    ap = argparse.ArgumentParser(description="Emit a show_widget offer-card for a deeper-analysis command.")
    ap.add_argument("command", help="the exact slash command, e.g. '/equity-research:earnings ORCL Q4 FY25'")
    ap.add_argument("label", help="the button text, e.g. 'Deeper earnings report →'")
    ap.add_argument("--note", default=None, help="an optional one-line note under the card")
    ap.add_argument("--skill-available", default=None, metavar="NAME",
                    help="the matching document SKILL is visible in THIS session's own skill list; "
                         "on OpenAI this selects the native-prompt compatibility fallback")
    ap.add_argument("--host", choices=("auto", "claude", "openai"), default="auto",
                    help="host contract; auto reads the static market-data-host.json build marker")
    ap.add_argument("--lang", choices=tuple(sorted(LANGUAGE_INSTRUCTIONS)), default=None,
                    help="report language for the deliverable the button asks for; appends the same "
                         "instruction the OpenAI companion card sends. Omitted -> nothing is added")
    a = ap.parse_args()
    host = packaged_host() if a.host == "auto" else a.host
    if host == "openai":
        prompt = openai_skill_prompt(a.command, a.skill_available)
        mapped_prompt = openai_skill_prompt(a.command)
        if prompt and a.skill_available:
            print(dashboard.offer_widget(in_language(prompt, a.lang), a.label, True,
                                         note=a.note, host="openai"))
        else:
            print(dashboard.offer_widget(in_language(mapped_prompt, a.lang) if mapped_prompt
                                         else "Install financial-deep-analysis",
                                         a.label, False, note=a.note, host="openai"))
        return

    ns = derive_namespace(a.command)
    ok = plugin_installed(ns)
    if not ok:
        # the AGENT's own skill list is the highest-truth signal (the MA session 2026-07-23:
        # earnings-preview was invocable via the Skill tool while the manifest/mirror probes
        # both missed it and the card wrongly said "run on desktop")
        mk = a.skill_available or mirrored_skill(a.command)
        if mk:
            # local Cowork: no plugin manifest, but the backing SKILL is mirrored beside us —
            # a REAL one-click: the button sendPrompts an instruction to run the mirrored skill
            # (the /plugin:command form would not resolve here). Cloud Cowork has no mirror, so
            # it still falls through to the honest desktop card below.
            args = a.command.split(None, 1)[1] if len(a.command.split(None, 1)) > 1 else ""
            prompt = (f"Use the '{mk}' skill (the mirrored financial-services document skill)"
                      + (f": {args}" if args else ""))
            print(dashboard.offer_widget(in_language(prompt, a.lang), a.label, True,
                                         note=(a.note or f"runs the mirrored '{mk}' skill here in Cowork")))
            return
    print(dashboard.offer_widget(in_language(a.command, a.lang), a.label, ok, note=a.note))


if __name__ == "__main__":
    main()
