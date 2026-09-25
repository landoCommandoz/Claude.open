"""
deskconfig.py | Loads and validates config/risk.json and config/tools.json.

Nothing here decides money. It only answers: are all the numbers present and sane,
and which Robinhood tool plays which role. Any problem raises ConfigError (fail closed).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MODES = ("DRY", "LIVE")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
TOOL_CLASSES = ("read", "preview", "place", "cancel", "forbidden")
ORDER_TYPES = ("limit", "market", "stop_market", "stop_limit")

# Role names seen in setup/tools.template.json and BUILD.md section 3, folded to one set.
ROLE_ALIASES = {"accounts": "accounts", "account_value": "account", "account": "account",
                "positions": "positions", "orders": "orders", "open_orders": "orders",
                "order_history": "orders", "order_status": "orders", "quotes": "quotes",
                "quote": "quotes", "pairs": "pairs", "crypto_order_preview": "preview",
                "crypto_order": "place", "cancel_order": "cancel"}
READ_ROLES = ("accounts", "account", "positions", "orders", "quotes")


class ConfigError(Exception):
    pass


def _frac(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1


def _pos(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _hhmm(v):
    return isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) is not None


def _date(v):
    return isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None


SPEC = {
    "account": {"starting_capital": _pos, "floor_equity": _pos, "floor_gap_allowance_frac": _frac,
                "stop_slip_pause_frac": _frac, "daily_loss_limit_frac": _frac,
                "weekly_loss_limit_frac": _frac},
    "strategy": {"entry_lookback": _pos_int, "exit_lookback": _pos_int, "trend_sma": _pos_int,
                 "atr_period": _pos_int, "stop_atr_mult": _pos, "risk_per_trade": _frac,
                 "max_position_frac": _frac, "max_positions": _pos_int, "max_open_risk_frac": _frac,
                 "min_stop_frac": _frac, "min_notional": _pos, "cash_buffer_frac": _frac,
                 "dd_step": _frac, "dd_cut": _frac},
    "execution": {"cost_per_side_assumed": _frac, "cost_per_side_stress": _frac,
                  "max_spread_frac": _frac, "entry_limit_slippage_frac": _frac,
                  "entry_fill_timeout_sec": _pos_int, "chase_limit_n": _pos,
                  "price_check_tolerance_frac": _frac, "min_ratchet_n": lambda v: _pos(v) or v == 0,
                  "stop_time_in_force": lambda v: v == "gtc", "stop_refresh_days": _pos_int,
                  "stop_retry_limit": _pos_int, "exit_reprice_attempts": _pos_int,
                  "exit_reprice_step_frac": _frac, "approval_ttl_sec": _pos_int,
                  "tape_max_age_sec": _pos_int, "dust_qty_frac": _frac},
    "schedule": {"daily_run_utc": _hhmm, "watcher_interval_min": _pos_int,
                 "missed_daily_alert_utc": _hhmm, "stale_run_alert_hours": _pos_int,
                 "weekly_review_day": lambda v: v in DAYS},
    "alerts": {"ntfy_topic": lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{0,64}", v),
               "daily_summary": lambda v: isinstance(v, bool)},
    "gate": {"full_start": _date, "recent_start": _date, "min_trades": _pos_int,
             "min_profit_factor": _pos, "min_expectancy_r": _pos,
             "max_drawdown_pct": lambda v: isinstance(v, (int, float)) and -100 < v < 0,
             "recent_min_profit_factor": _pos, "stress_min_profit_factor": _pos,
             "rolling_max_floor_hits": lambda v: isinstance(v, int) and v >= 0},
}


def validate_config(cfg: dict) -> list[str]:
    errors = []
    if not isinstance(cfg, dict):
        return ["config is not an object"]
    if cfg.get("mode") not in MODES:
        errors.append(f"mode must be one of {MODES}")
    uni = cfg.get("universe")
    if not isinstance(uni, list) or not uni or not all(isinstance(c, str) and c.isalnum()
                                                       and c.isupper() for c in uni):
        errors.append("universe must be a non-empty list of uppercase coin symbols")
    for section, fields in SPEC.items():
        block = cfg.get(section)
        if not isinstance(block, dict):
            errors.append(f"missing section {section}")
            continue
        for key, ok in fields.items():
            if key not in block:
                errors.append(f"missing {section}.{key}")
            elif not ok(block[key]):
                errors.append(f"out of range {section}.{key}={block[key]!r}")
    if not errors:
        acct = cfg["account"]
        if acct["floor_equity"] >= acct["starting_capital"]:
            errors.append("floor_equity must be below starting_capital")
        if cfg["execution"]["cost_per_side_stress"] < cfg["execution"]["cost_per_side_assumed"]:
            errors.append("stress cost must be at least the assumed cost")
    return errors


def load_config(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {Path(path).name}: {exc}") from exc
    errors = validate_config(cfg)
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg


# ---------- tools.json ----------

def short_name(tool_name: str) -> str:
    return str(tool_name).rsplit("__", 1)[-1]


def role_of(info: dict) -> str | None:
    return ROLE_ALIASES.get(info.get("role", ""))


def validate_tools(tools: dict, universe: list[str]) -> list[str]:
    errors = []
    if not isinstance(tools, dict) or not isinstance(tools.get("tools"), dict) or not tools["tools"]:
        return ["tools.json has no tools"]
    if "SERVER" in json.dumps(tools):
        errors.append("tools.json still contains the SERVER placeholder")
    for name, info in tools["tools"].items():
        if not name.startswith("mcp__") or info.get("class") not in TOOL_CLASSES:
            errors.append(f"tool {name}: bad name or class")
        elif info["class"] != "forbidden" and role_of(info) is None:
            errors.append(f"tool {name}: unknown role {info.get('role')!r}")
    have = {role_of(i) for i in tools["tools"].values() if i.get("class") != "forbidden"}
    for role in READ_ROLES + ("preview", "place", "cancel"):
        if role not in have:
            errors.append(f"no tool for role {role}")
    if tools.get("stop_order_type") not in ("stop_market", "stop_limit"):
        errors.append("stop_order_type must be stop_market or stop_limit")
    for coin in universe:
        inc = tools.get("increments", {}).get(coin, {})
        if not all(isinstance(inc.get(k), (int, float)) and inc.get(k) > 0 for k in ("qty", "price")):
            errors.append(f"increments for {coin} missing or not numbers")
        mins = [tools.get(k, {}).get(coin) for k in ("min_order_usd", "min_order_size")]
        if not any(isinstance(m, (int, float)) and m > 0 for m in mins):
            errors.append(f"minimum order for {coin} missing")
    return errors


def load_tools(path: Path, universe: list[str] | None = None) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            tools = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read tools.json: {exc}") from exc
    errors = validate_tools(tools, universe or [])
    if errors:
        raise ConfigError("; ".join(errors))
    return tools


def tools_for(tools: dict, role: str, klass: str | None = None) -> list[str]:
    return [n for n, i in tools["tools"].items()
            if role_of(i) == role and i.get("class") != "forbidden"
            and (klass is None or i.get("class") == klass)]


def tool_for(tools: dict, role: str) -> str:
    names = tools_for(tools, role)
    if not names:
        raise ConfigError(f"no tool for role {role}")
    return names[0]


def order_type_values(tools: dict, tool_name: str) -> dict:
    return {**tools.get("order_type_values", {}),
            **tools["tools"].get(tool_name, {}).get("order_type_values", {})}


def raw_order_type(tools: dict, tool_name: str, canonical: str) -> str:
    return order_type_values(tools, tool_name).get(canonical, canonical)


def canonical_order_type(tools: dict, tool_name: str, raw) -> str | None:
    for canon, value in order_type_values(tools, tool_name).items():
        if value == raw:
            return canon
    return raw if raw in ORDER_TYPES else None


def symbol_for(tools: dict, coin: str) -> str:
    return tools.get("symbol_format", "{coin}-USD").format(coin=coin)


def coin_from_symbol(raw) -> str:
    return str(raw).upper().split("-")[0].split("/")[0]


def args_map(tools: dict, tool_name: str) -> dict:
    return tools["tools"].get(tool_name, {}).get("args_map", {})


def args_raw(tools: dict, tool_name: str, canonical: dict) -> dict:
    """Canonical order fields -> the exact argument names this tool takes. Unmapped keys are dropped."""
    amap = args_map(tools, tool_name)
    out = {}
    for key, value in canonical.items():
        if key not in amap or value is None:
            continue
        if key == "coin":
            value = symbol_for(tools, value)
        elif key == "order_type":
            value = raw_order_type(tools, tool_name, value)
        out[amap[key]] = value
    return out


def args_canonical(tools: dict, tool_name: str, tool_input: dict) -> dict:
    """The reverse of args_raw: what a tool call actually asks for, in canonical names."""
    out = {}
    for key, raw_name in args_map(tools, tool_name).items():
        if raw_name not in tool_input:
            continue
        value = tool_input[raw_name]
        if key == "coin":
            value = coin_from_symbol(value)
        elif key == "order_type":
            value = canonical_order_type(tools, tool_name, value)
        out[key] = value
    return out


def increments(tools: dict, coin: str) -> tuple[float, float]:
    inc = tools["increments"][coin]
    return float(inc["qty"]), float(inc["price"])


def min_order_ok(tools: dict, coin: str, qty: float, price: float) -> bool:
    usd = tools.get("min_order_usd", {}).get(coin)
    size = tools.get("min_order_size", {}).get(coin)
    if isinstance(usd, (int, float)) and qty * price < usd:
        return False
    if isinstance(size, (int, float)) and qty < size:
        return False
    return True
