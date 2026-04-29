"""Session 14.5 Part 1 — chain spot-check of getKami(...).state.

For a small set of kami_ids drawn from recent harvest_* / liquidate
rows, query the live chain via GetterSystem.getKami and report the
``state`` string ("RESTING" / "HARVESTING" / "DEAD"). Used to verify
which action types end a harvest and which do not.

Read-only; writes nothing to DuckDB or chain. Single-shot.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingester.chain_client import ChainClient  # noqa: E402
from ingester.config import configure_logging, load_config  # noqa: E402
from ingester.kami_static import KamiStaticReader  # noqa: E402
from ingester.system_registry import resolve_systems  # noqa: E402


SAMPLES = [
    # label, kami_id, expected_state_for_hypothesis, action_context
    (
        "recent harvest_collect actor",
        "90744872039007326162727949324238766027944830217727694520394257942237350478642",
        "HARVESTING",
        "If collect doesn't end harvest, this kami should still be HARVESTING.",
    ),
    (
        "recent harvest_stop actor",
        "95572451966947218860805346501587441910047595000743899472846868271815482636361",
        "RESTING",
        "stop ends harvest → expect RESTING.",
    ),
    (
        "recent liquidate VICTIM (resolved via harvest_id self-join)",
        "5570467898776264922572070991504340585288418025255076400873058461102659589731",
        "RESTING_or_DEAD",
        "liquidate ends victim's harvest. Victim might also be DEAD if HP hit 0.",
    ),
    (
        "recent liquidate KILLER",
        "58264419171348297404332385261669086893213926612285789848147261828216750355574",
        "any",
        "killer's own state is independent of this row.",
    ),
]


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)
    client = ChainClient(cfg.rpc_url)
    reg = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    reader = KamiStaticReader(client, reg, cfg.abi_dir)

    print("kami_id, kami_index, name, level, state, room, hypothesis_label")
    for label, kami_id, expected, note in SAMPLES:
        try:
            shape = client.call_contract_fn(reader.contract, "getKami", int(kami_id))
            (_id, kami_index, name, _media, _stats, _traits, _aff,
             _account, level, _xp, room, state) = shape
            print(
                f"{kami_id[:12]}...  idx={kami_index}  name={name!r}  "
                f"level={level}  state={state!r}  room={room}  | {label}"
            )
            print(f"    expected: {expected}  — {note}")
        except Exception as e:
            print(f"{kami_id[:12]}...  ERROR  | {label}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
