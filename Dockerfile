FROM python:3.11-slim

# 可用 --build-arg APT_MIRROR=mirrors.aliyun.com 等镜像源加速/绕过网络限制
ARG APT_MIRROR=deb.debian.org

WORKDIR /app

# 安装系统依赖
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources || true; \
    fi && \
    apt-get update && apt-get install -y \
    git \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据目录与非 root 用户
RUN mkdir -p data \
    && groupadd --system bilibot \
    && useradd --system --gid bilibot --home-dir /app --shell /usr/sbin/nologin bilibot \
    && chown -R bilibot:bilibot /app

# 暴露端口
EXPOSE 8080
ENV BILIBOT_WEB_PORT=8080

# 健康检查（公开状态端点，无需鉴权；BILIBOT_WEB_PORT 需与 config web.port 一致，
# 且 web.enabled=true，否则该 HTTP 探针无法通过）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${BILIBOT_WEB_PORT}/api/status/public || exit 1

USER bilibot

# 默认命令
CMD ["python", "-m", "bilibot"]
