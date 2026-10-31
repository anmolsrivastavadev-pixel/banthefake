"""Regenerate the throwaway probe C2PA material from the repository root with:

.venv/bin/python tests/fixtures/probe_c2pa/make_probe_c2pa.py

It creates a new test root CA ("BTF Probe Root", valid for 100 years) and an end-entity signing certificate in a
temporary directory, writes `ca.pem`, and signs small JPEGs with c2pa-python. The private keys are discarded.
These are generated test certificates, not real ones: they are on no trust list.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from c2pa import Builder, C2paSignerInfo, Signer

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
IPTC = "http://cv.iptc.org/newscodes/digitalsourcetype/"
CNF = """[req]
distinguished_name=dn
prompt=no
[dn]
CN=BTF Probe Root
O=Probe
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
[v3_ee]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=emailProtection
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
"""


def _pki(d: Path) -> None:
    def ssl(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, cwd=d, capture_output=True)

    (d / "ca.cnf").write_text(CNF)
    ssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key")
    ssl("req", "-x509", "-new", "-key", "ca.key", "-days", "36500", "-config", "ca.cnf", "-extensions", "v3_ca",
        "-out", "ca.pem")
    ssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ee.key")
    ssl("req", "-new", "-key", "ee.key", "-subj", "/CN=Probe Camera/O=Probe Org", "-out", "ee.csr")
    ssl("x509", "-req", "-in", "ee.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "36000",
        "-extfile", "ca.cnf", "-extensions", "v3_ee", "-out", "ee.pem")
    ssl("pkcs8", "-topk8", "-nocrypt", "-in", "ee.key", "-out", "ee.p8")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _pki(d)
        info = C2paSignerInfo(alg=b"es256", sign_cert=(d / "ee.pem").read_bytes(),
                              private_key=(d / "ee.p8").read_bytes(), ta_url=None)

        def make(name: str, src: Path, actions: list[dict], ingredient: Path | None = None) -> Path:
            manifest = {"claim_generator_info": [{"name": "ProbeCam", "version": "1"}], "title": name,
                        "assertions": [{"label": "c2pa.actions", "data": {"actions": actions}}]}
            builder = Builder(manifest)
            if ingredient is not None:
                with open(ingredient, "rb") as f:
                    builder.add_ingredient({"title": ingredient.name, "relationship": "componentOf",
                                            "label": "ing1"}, "image/jpeg", f)
            out = HERE / name
            with open(src, "rb") as s, open(out, "wb") as dst:
                builder.sign(Signer.from_info(info), "image/jpeg", s, dst)
            return out

        real, ai = FIXTURES / "real_cat_q70_640.jpg", FIXTURES / "ai_dalle_cat_q70_640.jpg"
        capture = {"action": "c2pa.created", "digitalSourceType": IPTC + "digitalCapture"}
        placed = {"action": "c2pa.placed", "parameters": {"ingredientIds": ["ing1"]}}
        make("s_capture.jpg", real, [capture])
        make("s_capture_unknown.jpg", real, [capture, {"action": "c2pa.unknown"}])
        make("s_capture_placed.jpg", real, [capture, placed], ingredient=real)
        signed_ai = make("s_ai.jpg", ai, [{"action": "c2pa.created", "digitalSourceType": IPTC + "trainedAlgorithmicMedia"}])
        make("s_capture_placed_ai.jpg", real, [capture, placed], ingredient=signed_ai)
        os.replace(d / "ca.pem", HERE / "ca.pem")


if __name__ == "__main__":
    main()
