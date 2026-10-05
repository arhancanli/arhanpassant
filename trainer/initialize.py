"""Restore a training checkpoint only when it reproduces the supplied NNUE."""

import hashlib
from pathlib import Path
import tempfile


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def verify_sources(provenance):
    for path, expected in provenance["inputs_sha256"].items():
        if digest(path) != expected:
            raise ValueError(f"initialization input changed: {path}")


def protect_inputs(checkpoint, network, output):
    inputs = [Path(checkpoint).resolve(), Path(network).resolve()]
    for target in [Path(output), Path(str(output) + ".pt"), Path(str(output) + ".init.json")]:
        for source in inputs:
            if target.resolve() == source or (target.exists() and target.samefile(source)):
                raise ValueError("training output would overwrite an initialization input")
    return inputs


def restore(net, checkpoint, network, output, exporter):
    import torch

    inputs = protect_inputs(checkpoint, network, output)
    provenance = {"inputs_sha256": {str(path): digest(path) for path in inputs}}
    weights = torch.load(inputs[0], map_location="cpu", weights_only=True)
    expected = net.state_dict()
    if not isinstance(weights, dict) or set(weights) != set(expected):
        raise ValueError("checkpoint keys do not match the configured network")
    for name, value in weights.items():
        if (not isinstance(value, torch.Tensor) or value.shape != expected[name].shape
                or value.dtype != expected[name].dtype):
            raise ValueError(f"checkpoint layout or dtype mismatch: {name}")
        if not bool(torch.isfinite(value).all()):
            raise ValueError(f"nonfinite initialization weight: {name}")
    net.load_state_dict(weights, strict=True)
    with tempfile.TemporaryDirectory(prefix="ap-initialization-", dir=Path(output).parent) as folder:
        exported = Path(folder) / "initial.nnue"
        exporter(net, exported)
        if digest(exported) != provenance["inputs_sha256"][str(inputs[1])]:
            raise ValueError("checkpoint does not reproduce the supplied NNUE")
    verify_sources(provenance)
    provenance["initial_export_matches_network"] = True
    return provenance
