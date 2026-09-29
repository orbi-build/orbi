# np.linspace returns NaN when start and stop are the same infinity

## Problem

When both endpoints of `np.linspace` are the same infinite value, every sample except the
last one comes back as `nan`, and NumPy emits an `invalid value encountered` RuntimeWarning:

```python
>>> import numpy as np
>>> np.linspace(np.inf, np.inf, 5)
RuntimeWarning: invalid value encountered in subtract
array([nan, nan, nan, nan, inf])
```

A range whose start equals its stop is a constant sequence, so the expected result is
`array([inf, inf, inf, inf, inf])`. The same problem shows up with `-np.inf`, with
`endpoint=False`, with `retstep=True` (the returned step should be `0.0`), with complex
infinities such as `complex(np.inf, np.inf)`, and element-wise when array endpoints contain
an equal infinity in some positions:

```python
>>> np.linspace(np.array([np.inf, 1.0]), np.array([np.inf, 3.0]), 3)
# expected [[inf, 1.], [inf, 2.], [inf, 3.]]
```

## Expected behavior

- Equal endpoints (including equal infinities, real or complex, scalar or per element) produce a
  constant sequence and a zero step.
- No spurious `RuntimeWarning` is emitted in these cases (do not simply silence warnings
  globally).
- Genuinely invalid inputs keep their current behavior: `np.linspace(np.inf, -np.inf, 5)` still
  yields `nan` interior points and still warns about the invalid value, and `nan` endpoints still
  propagate `nan`.

## Acceptance

- Fix the bug.
- Add tests covering the cases above.
- The repository's tests for this area pass, e.g. after building NumPy in development mode
  (`pip install --no-build-isolation -e .` or `spin build`):
  `python -m pytest numpy/_core/tests/test_function_base.py numpy/_core/tests/test_numeric.py numpy/lib/tests/test_function_base.py`
