FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir 'httpx>=0.28,<1'
COPY agent agent
USER 10001:10001
CMD ["python", "-m", "agent.app.main"]
