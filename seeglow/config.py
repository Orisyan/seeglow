"""配置读写：config.json + 环境变量覆盖（SEELOW_ 前缀）。"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_OUTPUT = ROOT / "拾光"

DEFAULTS = {
    "api_base": "https://api.siliconflow.cn/v1",
    "api_key": "",
    "model": "Qwen/Qwen3-Omni-30B-A3B-Instruct",
    "temperature": 0.3,
    "sessdata": "",
    "output_dir": str(DEFAULT_OUTPUT),
}

# 服务商预设：一个 Key、一个模型，既能听音频又能写总结
PRESETS = {
    "siliconflow": {
        "label": "硅基流动 · 单模型直听音频+总结（国内快，推荐）",
        "api_base": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-Omni-30B-A3B-Instruct",
    },
    "openai": {
        "label": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-audio-preview",
    },
    "custom": {
        "label": "自定义",
        "api_base": "",
        "model": "",
    },
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    for key in DEFAULTS:
        env = os.getenv(f"SEELOW_{key.upper()}")
        if env:
            cfg[key] = env
    cfg["output_dir"] = str(cfg.get("output_dir") or DEFAULT_OUTPUT)
    return cfg


def save_config(update: dict) -> dict:
    cfg = load_config()
    for k, v in (update or {}).items():
        if k in DEFAULTS and v is not None:
            cfg[k] = v
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg
