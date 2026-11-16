"""诊断: tdx 内置源是否被能力层补授 full_minute, 以及 minute_refresh 是否真的会跑。

只读, 不改任何状态。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data_providers import custom  # noqa: E402
from app.services import preferences  # noqa: E402
from app.tickflow import policy  # noqa: E402
from app.tickflow.capabilities import Cap  # noqa: E402


def main() -> None:
    print("registered:", sorted(custom.names()))
    print("tdx is_custom:", custom.is_custom_provider("tdx"))
    print("has full_minute:", custom.provider_has_dataset("tdx", "full_minute"))
    print("has minute:", custom.provider_has_dataset("tdx", "minute"))
    try:
        p = custom.get_provider("tdx")
        print("datasets:", p.config.datasets)
    except Exception as e:  # noqa: BLE001
        print("get_provider err:", e)

    print("\nprefs: full_minute =", preferences.get_full_minute_data_provider())
    print("prefs: minute      =", preferences.get_minute_data_provider())

    capset = policy.detect_capabilities()
    for cap in (Cap.INTRADAY_UNIVERSE, Cap.INTRADAY_BATCH, Cap.KLINE_MINUTE_BATCH):
        print(f"cap {cap.value:20s} -> {capset.has(cap)}")


if __name__ == "__main__":
    main()
