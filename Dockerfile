# 拾光 SeeGlow · 公共部署镜像
# 构建并运行（默认私有配置）：
#   docker build -t seeglow .
#   docker run -p 8765:8765 -e SEELOW_PUBLIC=1 -v seeglow_data:/data seeglow
FROM python:3.12-slim

WORKDIR /app

# PyAV 需要的运行库（ffmpeg 编解码器已内置在 av wheels 里，无需装 ffmpeg）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY seeglow ./seeglow
COPY run_web.py .

# 公共模式：输出集中到可挂载的卷；监听 0.0.0.0 由 web.main 自动处理
ENV SEELOW_PUBLIC=1 \
    SEELOW_OUTPUT_DIR=/data/shiguang \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "run_web.py"]
