#!/usr/bin/env python3
"""Generate small ONNX test models for llvm-pipa ML benchmark smoke tests.

Usage:
    python3 scripts/make_test_onnx.py [--output-dir benchmarks/ml]

Requires: pip install onnx
"""
import argparse
from pathlib import Path


def make_matmul_4x4(output_path: Path) -> None:
    """X: [1,4] @ W: [4,4] -> Y: [1,4]  (single MatMul, constant weight)."""
    import onnx
    from onnx import helper, TensorProto, numpy_helper
    import numpy as np

    X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 4])
    Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 4])
    W_data = np.eye(4, dtype=np.float32).flatten().tolist()
    W = numpy_helper.from_array(np.array(W_data, dtype=np.float32).reshape(4, 4), name="W")
    node = helper.make_node("MatMul", inputs=["X", "W"], outputs=["Y"])
    graph = helper.make_graph([node], "matmul_4x4", [X], [Y], initializer=[W])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    onnx.checker.check_model(model)
    onnx.save(model, str(output_path))
    print(f"Saved: {output_path}")


def make_mlp_16(output_path: Path) -> None:
    """Two-layer MLP: [1,16] -> [1,16] -> [1,16]  (Linear + ReLU + Linear)."""
    import onnx
    from onnx import helper, TensorProto, numpy_helper
    import numpy as np

    X  = helper.make_tensor_value_info("X",  TensorProto.FLOAT, [1, 16])
    Y  = helper.make_tensor_value_info("Y",  TensorProto.FLOAT, [1, 16])
    W1 = numpy_helper.from_array(np.random.randn(16, 16).astype(np.float32), name="W1")
    b1 = numpy_helper.from_array(np.zeros(16, dtype=np.float32), name="b1")
    W2 = numpy_helper.from_array(np.random.randn(16, 16).astype(np.float32), name="W2")
    b2 = numpy_helper.from_array(np.zeros(16, dtype=np.float32), name="b2")

    nodes = [
        helper.make_node("Gemm", inputs=["X", "W1", "b1"],  outputs=["h1"]),
        helper.make_node("Relu", inputs=["h1"],              outputs=["h1_relu"]),
        helper.make_node("Gemm", inputs=["h1_relu", "W2", "b2"], outputs=["Y"]),
    ]
    graph = helper.make_graph(nodes, "mlp_16", [X], [Y], initializer=[W1, b1, W2, b2])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    onnx.checker.check_model(model)
    onnx.save(model, str(output_path))
    print(f"Saved: {output_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", default="benchmarks/ml", help="Output directory")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import onnx  # noqa: F401
    except ImportError:
        print("Error: 'onnx' package not installed. Run: pip install onnx")
        raise SystemExit(1)

    make_matmul_4x4(out_dir / "matmul_4x4.onnx")
    make_mlp_16(out_dir / "mlp_16.onnx")
    print("Done. Add these to configs/ml_benchmarks.toml to use them.")


if __name__ == "__main__":
    main()
