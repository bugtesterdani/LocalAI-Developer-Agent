"""Erzeugt contracts/testvectors/transfer.json und transfer-qemu.vhdx.gz.

Der Container wird mit der Python-Implementierung gebaut; die VHDX erzeugt qemu-img (unabhängige
Implementierung des VHDX-Formats) – so prüft der C#-Leser gegen ein fremdes Abbild.
"""

import gzip
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "python"))

from localai.worker.transfer import TransferKind, build_container  # noqa: E402

OUT = ROOT / "contracts" / "testvectors"
PAYLOAD = '{"jobId":"j-1","sequence":42,"text":"Grüße aus der VM"}'.encode()
DISK = 4 * 1024 * 1024


def main() -> None:
    image = build_container(TransferKind.RESULT, 42, PAYLOAD)
    vector = {
        "kind": 1,
        "sequence": 42,
        "payloadUtf8": PAYLOAD.decode(),
        "headerHex": image[:64].hex(),
        "imageLength": len(image),
        "imageSha256": hashlib.sha256(image).hexdigest(),
        "qemuVhdx": {"file": "transfer-qemu.vhdx.gz", "virtualSize": DISK},
    }
    (OUT / "transfer.json").write_text(json.dumps(vector, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if shutil.which("qemu-img") is None:
        print("qemu-img fehlt – VHDX-Vektor unverändert")
        return
    with tempfile.TemporaryDirectory() as tmp:
        raw = pathlib.Path(tmp) / "disk.raw"
        raw.write_bytes(image.ljust(DISK, b"\0"))
        vhdx = pathlib.Path(tmp) / "disk.vhdx"
        subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "vhdx", "-o", "subformat=dynamic,block_size=1M",
                        str(raw), str(vhdx)], check=True)
        (OUT / "transfer-qemu.vhdx.gz").write_bytes(gzip.compress(vhdx.read_bytes(), mtime=0))
    print("Vektoren geschrieben")


if __name__ == "__main__":
    main()
