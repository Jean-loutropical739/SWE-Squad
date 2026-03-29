FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://localhost:8888/health || exit 1
CMD ["python3", "scripts/ops/dashboard_server.py", "--port", "8888", "--host", "0.0.0.0"]
