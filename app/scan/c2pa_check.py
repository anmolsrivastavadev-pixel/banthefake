"""C2PA (Content Credentials) reading and validation via c2pa-python. Degrades gracefully if missing."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("banthefake.c2pa")

try:
    from c2pa import C2paError, Context, Reader

    AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    AVAILABLE = False

AI_SOURCE_TYPES = ("trainedAlgorithmicMedia", "compositeWithTrainedAlgorithmicMedia")
CAPTURE_SOURCE_TYPES = ("digitalCapture", "computationalCapture")
# Actions that do not by themselves mean the content was changed.
NEUTRAL_ACTIONS = {
    "c2pa.created", "c2pa.opened", "c2pa.placed", "c2pa.published", "c2pa.transcoded",
    "c2pa.repackaged", "c2pa.converted", "c2pa.watermarked", "c2pa.unknown",
}
UNTRUSTED_CODES = ("signingCredential.untrusted",)
TRUST_DIR = Path(__file__).resolve().parent.parent / "trust"
TRUST_LIST_VERSION = "2026-10-23"
_context = None
_context_key = None


@dataclass
class C2PAResult:
    present: bool
    valid: bool = False
    trusted: bool = False
    generator: str = ""
    issuer: str = ""
    signer: str = ""
    signed_at: str = ""
    capture: bool = False
    ai_generated: bool = False
    actions: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    software_agents: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # validation failure codes (excluding untrusted signer)
    ingredients: list[dict[str, str]] = field(default_factory=list)
    trust_list_version: str = ""
    untrusted: bool = False
    readable: bool = True

    @property
    def edit_actions(self) -> list[str]:
        return [a for a in self.actions if a not in NEUTRAL_ACTIONS]

    def to_report(self) -> dict:
        return {
            "present": self.present,
            "valid": self.valid,
            "trusted": self.trusted,
            "generator": self.generator,
            "issuer": self.issuer,
            "signer": self.signer,
            "signed_at": self.signed_at,
            "capture": self.capture,
            "ai_generated": self.ai_generated,
            "actions": self.actions,
            "edits": self.edit_actions,
            "ingredients": self.ingredients,
            "trust_list_version": self.trust_list_version,
        }


def _get_context():
    global _context, _context_key
    mode = "off" if os.environ.get("BTF_C2PA_TRUST", "official").strip().lower() == "off" else "official"
    override = os.environ.get("BTF_C2PA_TRUST_ANCHORS", "")
    key = (mode, override)
    if _context is None or _context_key != key:
        settings: dict = {"verify": {"verify_trust": mode != "off", "verify_timestamp_trust": mode != "off",
                                     "remote_manifest_fetch": False, "ocsp_fetch": False}}
        if override and Path(override).is_file():
            anchors = Path(override).read_text()
            config = (TRUST_DIR / "store.cfg").read_text()
            settings["trust"] = {"trust_anchors": anchors, "trust_config": config}
        elif mode == "official":
            anchors = (TRUST_DIR / "C2PA-TRUST-LIST.pem").read_text()
            anchors += (TRUST_DIR / "C2PA-TSA-TRUST-LIST.pem").read_text()
            settings["trust"] = {"trust_anchors": anchors,
                                 "trust_config": (TRUST_DIR / "store.cfg").read_text()}
        _context = Context.from_dict(settings)
        _context_key = key
    return _context


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, (C2paError.ManifestNotFound, C2paError.NotSupported))


def read(path: str | Path, mime: str) -> C2PAResult | None:
    """Return the active manifest summary, None if there is no manifest (or C2PA is unavailable)."""
    if not AVAILABLE:
        return None
    try:
        with open(path, "rb") as f, Reader(mime, f, context=_get_context()) as reader:
            store = json.loads(reader.json())
            state = reader.get_validation_state()
    except Exception as exc:
        if AVAILABLE and _is_not_found(exc):
            return None
        log.info("c2pa read failed: %s", type(exc).__name__)
        return C2PAResult(present=False, readable=False)
    result = summarize(store, state)
    if result is not None and result.trusted and os.environ.get("BTF_C2PA_TRUST_ANCHORS"):
        result.trust_list_version = "custom"
    return result


def summarize(store: dict, state: str | None) -> C2PAResult | None:
    manifests = store.get("manifests") or {}
    m = manifests.get(store.get("active_manifest") or "")
    if not isinstance(m, dict):
        return None
    actions = [
        a
        for x in m.get("assertions") or []
        if str(x.get("label", "")).startswith("c2pa.actions")
        for a in (x.get("data") or {}).get("actions") or []
        if isinstance(a, dict)
    ]
    source_types = [str(a["digitalSourceType"]) for a in actions if a.get("digitalSourceType")]
    agents = []
    for a in actions:
        agent = a.get("softwareAgent")
        name = agent.get("name") if isinstance(agent, dict) else agent
        if isinstance(name, str) and name and name not in agents:
            agents.append(name)
    statuses = store.get("validation_status") or []
    codes = [str(s.get("code", "")) for s in statuses if isinstance(s, dict)]
    untrusted = any(c in UNTRUSTED_CODES for c in codes)
    failures = [c for c in codes if c not in UNTRUSTED_CODES]
    generator = _generator(m)
    sig = m.get("signature_info") or {}
    common_name = str(sig.get("common_name") or "")
    organisation = str(sig.get("issuer") or "")
    signer = common_name
    if organisation and organisation != common_name:
        signer = f"{common_name} ({organisation})" if common_name else organisation
    capture = any(
        a.get("action") == "c2pa.created"
        and str(a.get("digitalSourceType", "")).rsplit("/", 1)[-1] in CAPTURE_SOURCE_TYPES
        for a in actions
    )
    ingredients = []
    for ingredient in m.get("ingredients") or []:
        if not isinstance(ingredient, dict):
            continue
        summary = {k: str(ingredient[k]) for k in ("title", "format", "relationship") if ingredient.get(k)}
        if summary:
            ingredients.append(summary)
    return C2PAResult(
        present=True,
        valid=state in ("Valid", "Trusted") and not failures,
        trusted=state == "Trusted",
        generator=generator,
        issuer=organisation or common_name,
        signer=signer,
        signed_at=str(sig.get("time") or ""),
        capture=capture and not any(t.rsplit("/", 1)[-1] in AI_SOURCE_TYPES for t in source_types),
        ai_generated=any(t.rsplit("/", 1)[-1] in AI_SOURCE_TYPES for t in source_types),
        actions=list(dict.fromkeys(str(a.get("action", "")) for a in actions if a.get("action"))),
        source_types=source_types,
        software_agents=agents,
        failures=failures,
        ingredients=ingredients,
        trust_list_version=TRUST_LIST_VERSION if state == "Trusted" else "",
        untrusted=untrusted,
    )


def _generator(m: dict) -> str:
    info = m.get("claim_generator_info")
    if isinstance(info, list) and info and isinstance(info[0], dict) and info[0].get("name"):
        first = info[0]
        return f"{first['name']} {first.get('version', '')}".strip()
    return str(m.get("claim_generator") or "")
