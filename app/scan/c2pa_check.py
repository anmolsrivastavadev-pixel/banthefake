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
# Actions that do not by themselves mean the content was changed. `c2pa.placed` (another asset was placed into
# this one) and `c2pa.unknown` (an unspecified change) are deliberately NOT here: both can change the content.
NEUTRAL_ACTIONS = {
    "c2pa.created", "c2pa.opened", "c2pa.published", "c2pa.transcoded",
    "c2pa.repackaged", "c2pa.converted", "c2pa.watermarked",
}
UNTRUSTED_CODES = ("signingCredential.untrusted",)
TRUST_DIR = Path(__file__).resolve().parent.parent / "trust"
TRUST_LIST_VERSION = "2026-10-23"
PEM_CERT = "-----BEGIN CERTIFICATE-----"
MAX_INGREDIENT_DEPTH = 8
_context = None
_context_key = None
_custom_label = ""  # trust_list_version for results trusted via a loaded BTF_C2PA_TRUST_ANCHORS file


@dataclass
class C2PAResult:
    present: bool
    valid: bool = False
    trusted: bool = False
    generator: str = ""
    issuer: str = ""
    signer: str = ""
    signed_at: str = ""
    capture: bool = False  # camera capture declared by a valid, trusted manifest (never set for unverified claims)
    claimed_capture: bool = False  # the manifest declares a camera capture, whether or not it validates or is trusted
    ai_generated: bool = False  # the manifest, or the manifest of one of its ingredients, declares generative AI
    ingredient_ai: bool = False  # an ingredient's own manifest declares generative AI
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
            "claimed_capture": self.claimed_capture,
            "ai_generated": self.ai_generated,
            "ingredient_ai": self.ingredient_ai,
            "actions": self.actions,
            "edits": self.edit_actions,
            "ingredients": self.ingredients,
            "trust_list_version": self.trust_list_version,
        }


def _get_context():
    global _context, _context_key, _custom_label
    mode = "off" if os.environ.get("BTF_C2PA_TRUST", "official").strip().lower() == "off" else "official"
    override = os.environ.get("BTF_C2PA_TRUST_ANCHORS", "")
    append = os.environ.get("BTF_C2PA_TRUST_ANCHORS_MODE", "replace").strip().lower() == "append"
    key = (mode, override, append, str(TRUST_DIR))
    if _context is None or _context_key != key:
        settings, label = _build_settings(mode, override, append)
        _context = Context.from_dict(settings)
        _context_key, _custom_label = key, label
    return _context


def _load_override(path: str) -> str | None:
    """PEM text of the BTF_C2PA_TRUST_ANCHORS file, or None (logged as an error) if it cannot be used."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        log.error("BTF_C2PA_TRUST_ANCHORS=%s cannot be read (%s): no signer is trusted", path, type(exc).__name__)
        return None
    if PEM_CERT not in text:
        log.error("BTF_C2PA_TRUST_ANCHORS=%s contains no PEM certificate: no signer is trusted", path)
        return None
    return text


def _context_settings(mode: str, override: str = "", append: bool = False) -> dict:
    return _build_settings(mode, override, append)[0]


def _build_settings(mode: str, override: str = "", append: bool = False) -> tuple[dict, str]:
    """c2pa-rs settings, plus the trust_list_version label for signers trusted through a custom anchor file.

    Only signer roots are passed as trust anchors. c2pa-python 0.38 pools every configured anchor, whatever its
    `trust_kind`, when it checks the signing certificate, so loading the C2PA TSA list would let any TSA root make
    a manifest signer "trusted". The TSA list is therefore not loaded. Timestamps are still validated, and their
    trust is still checked (`verify_timestamp_trust`) against the signer roots; an untrusted timestamp is only
    informational and never changes the validation state.

    If BTF_C2PA_TRUST_ANCHORS is set but unusable (missing, unreadable, no certificate), nothing is trusted: the
    operator asked to replace the official list, so silently falling back to it would trust unintended signers.
    """
    enabled = mode != "off"
    settings: dict = {
        "verify": {
            "verify_trust": enabled,
            "verify_timestamp_trust": enabled,
            "remote_manifest_fetch": False,
            "ocsp_fetch": False,
        }
    }
    if not enabled:
        return settings, ""
    official = {
        "trust_anchors": (TRUST_DIR / "C2PA-TRUST-LIST.pem").read_text(),
        "trust_kind": "manifest",
        "trust_uri": "https://c2pa.org/trustlist",
    }
    if not override:
        settings["trust"] = {"anchors": [official]}
        return settings, ""
    custom = _load_override(override)
    if custom is None:
        settings["verify"]["verify_trust"] = settings["verify"]["verify_timestamp_trust"] = False
        return settings, ""
    anchors = [{"trust_anchors": custom, "trust_kind": "manifest", "trust_uri": "custom"}]
    if append:
        anchors.insert(0, official)
    settings["trust"] = {"anchors": anchors}
    return settings, f"{TRUST_LIST_VERSION}+custom" if append else "custom"


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, (C2paError.ManifestNotFound, C2paError.NotSupported))


def read(path: str | Path, mime: str) -> C2PAResult | None:
    """Return the active manifest summary, None if there is no manifest (or C2PA is unavailable)."""
    if not AVAILABLE:
        return None
    try:
        context = _get_context()
        with open(path, "rb") as f, Reader(mime, f, context=context) as reader:
            store = json.loads(reader.json())
            state = reader.get_validation_state()
    except Exception as exc:
        if AVAILABLE and _is_not_found(exc):
            return None
        log.info("c2pa read failed: %s", type(exc).__name__)
        return C2PAResult(present=False, readable=False)
    try:
        result = summarize(store, state)
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        log.info("c2pa manifest summary failed: %s", type(exc).__name__)
        return C2PAResult(present=True, readable=False)
    if result is not None and result.trusted and os.environ.get("BTF_C2PA_TRUST_ANCHORS"):
        # a loaded custom anchor file replaces (or extends) the official list; never label an unloaded one
        result.trust_list_version = _custom_label
    return result


def summarize(store: dict, state: str | None) -> C2PAResult | None:
    if not isinstance(store, dict):
        return None
    manifests = store.get("manifests") or {}
    if not isinstance(manifests, dict):
        return None
    m = manifests.get(store.get("active_manifest") or "")
    if not isinstance(m, dict):
        return None
    assertions = m.get("assertions") or []
    if not isinstance(assertions, list):
        assertions = []
    actions = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        if not str(assertion.get("label", "")).startswith("c2pa.actions"):
            continue
        data = assertion.get("data") or {}
        if not isinstance(data, dict):
            continue
        listed = data.get("actions") or []
        if isinstance(listed, list):
            actions.extend(action for action in listed if isinstance(action, dict))
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
    if not isinstance(sig, dict):
        sig = {}
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
    ingredient_ai = False
    for ingredient in _list(m.get("ingredients")):
        if not isinstance(ingredient, dict):
            continue
        summary = {k: str(ingredient[k]) for k in ("title", "format", "relationship") if ingredient.get(k)}
        declares_ai = _declares_ai(manifests, ingredient.get("active_manifest"), set())
        if declares_ai:
            summary["ai_generated"] = "true"
            ingredient_ai = True
        if summary:
            ingredients.append(summary)
    declared_ai = any(_is_ai(t) for t in source_types)
    ai_generated = declared_ai or ingredient_ai
    valid = state in ("Valid", "Trusted") and not failures
    trusted = state == "Trusted" and valid
    claimed_capture = capture and not ai_generated
    return C2PAResult(
        present=True,
        valid=valid,
        trusted=trusted,
        generator=generator,
        issuer=organisation or common_name,
        signer=signer,
        signed_at=str(sig.get("time") or ""),
        # only a valid manifest from a trusted signer is capture evidence; anything else is just a claim
        capture=claimed_capture and trusted,
        claimed_capture=claimed_capture,
        ai_generated=ai_generated,
        ingredient_ai=ingredient_ai,
        actions=list(dict.fromkeys(str(a.get("action", "")) for a in actions if a.get("action"))),
        source_types=source_types,
        software_agents=agents,
        failures=failures,
        ingredients=ingredients,
        trust_list_version=TRUST_LIST_VERSION if trusted else "",
        untrusted=untrusted,
    )


def _generator(m: dict) -> str:
    info = m.get("claim_generator_info")
    if isinstance(info, list) and info and isinstance(info[0], dict) and info[0].get("name"):
        first = info[0]
        return f"{first['name']} {first.get('version', '')}".strip()
    return str(m.get("claim_generator") or "")


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _is_ai(source_type: str) -> bool:
    return source_type.rsplit("/", 1)[-1] in AI_SOURCE_TYPES


def _manifest_source_types(manifest: dict) -> list[str]:
    types = []
    for assertion in _list(manifest.get("assertions")):
        if not isinstance(assertion, dict) or not str(assertion.get("label", "")).startswith("c2pa.actions"):
            continue
        data = assertion.get("data")
        for action in _list(data.get("actions") if isinstance(data, dict) else None):
            if isinstance(action, dict) and action.get("digitalSourceType"):
                types.append(str(action["digitalSourceType"]))
    return types


def _declares_ai(manifests: dict, label, seen: set, depth: int = 0) -> bool:
    """Whether the ingredient manifest `label`, or any of its own ingredients, declares generative AI."""
    if not isinstance(label, str) or label in seen or depth >= MAX_INGREDIENT_DEPTH:
        return False
    seen.add(label)
    manifest = manifests.get(label)
    if not isinstance(manifest, dict):
        return False
    if any(_is_ai(t) for t in _manifest_source_types(manifest)):
        return True
    return any(
        _declares_ai(manifests, ingredient.get("active_manifest"), seen, depth + 1)
        for ingredient in _list(manifest.get("ingredients"))
        if isinstance(ingredient, dict)
    )
