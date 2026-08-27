# -*- coding: utf-8 -*-
"""拾光 SeeGlow · 公共网站一键部署到 Modal（免费额度 $30/月，无需绑卡）

前置：pip install modal 且已登录（~/.modal.toml 存在）
部署：在本目录执行  python -m modal deploy deploy_modal.py
得到：https://<workspace>--seeglow-web-api.modal.run

要点：
- SEELOW_PUBLIC=1：BYOK 模式，访客自带 API Key（存各自浏览器），服务端零凭据
- 结果与笔记落在 Volume（seeglow-data），容器重启不丢
- 单容器承载全部请求（任务状态在内存里，多容器会导致轮询打到别台）；并发靠 allow_concurrent_inputs
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.100", "uvicorn>=0.20", "pydantic>=2.0",
                 "requests>=2.28", "av>=12.0", "numpy>=1.24",
                 "python-multipart>=0.0.9", "PyMuPDF>=1.24")
    .env({
        "SEELOW_PUBLIC": "1",
        # 网站付费模式：服务器内置官方 API（Key 走 Modal Secret），赞助/激活码解锁
        "SEELOW_SITE_PAID": "1",
        "SEELOW_OUTPUT_DIR": "/data/shiguang",
        "PYTHONUNBUFFERED": "1",
    })
    # 源码打进镜像（含前端 static）
    .add_local_dir("seeglow", "/root/app/seeglow", copy=True)
)

# 敏感凭证走 Modal Secret（命令行注入，不落盘）：
#   modal secret create seeglow-auth SEEGLOW_AUTH_SECRET=xxx SEELOW_API_BASE=... SEELOW_API_KEY=... \
#     AFDIAN_USER_ID=... AFDIAN_TOKEN=...
site_secrets = modal.Secret.from_name("seeglow-auth")

volume = modal.Volume.from_name("seeglow-data", create_if_missing=True)

app = modal.App(
    "seeglow-web",
    image=image,
    volumes={"/data": volume},
)


@app.function(
    cpu=2,
    memory=4096,
    timeout=1800,            # 长任务保护：单次流水线最长 30 分钟
    scaledown_window=300,    # 空闲 5 分钟后休眠（免费额度内尽量省）
    max_containers=1,        # 内存任务表必须单实例；流量大时调高需改用共享存储
    secrets=[site_secrets],
)
@modal.concurrent(max_inputs=32)   # 一个容器同时接 32 路请求
@modal.asgi_app()
def api():
    import sys

    sys.path.insert(0, "/root/app")
    from seeglow.web import app as fastapi_app

    return fastapi_app
